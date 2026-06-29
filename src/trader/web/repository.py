"""Read-only monitoring repository (design §12/§19, M7.5).

The SINGLE place monitoring SQL lives, so route handlers stay thin and the no-write / no-leak
guarantees are easy to audit. Every method:
  * selects EXPLICIT columns (never ``SELECT *``) so secret columns are never even read;
  * returns plain ``dict``s (JSON-safe: ISO timestamps, Decimal strings) run through ``scrub``
    which redacts any stray secret-ish key as ``***`` (defense in depth);
  * is parameterized.

``token_status`` reads the SEPARATE Schwab token store DB read-only and selects ONLY the
issue/expiry timestamp columns — it NEVER touches access_token/refresh_token. The whole module
imports only stdlib + ``trader.web.db`` (no broker/schwab/auth), so it cannot leak via a code
path either.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from trader.web.db import ReadOnlyStateDB

# Substrings that mark a dict key as secret-bearing -> redact its value.
SECRET_KEYS = (
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "password",
    "hash",
    "app_key",
)

# The Schwab token store lives next to the state DB (cli `_schwab_config` default).
_TOKEN_DB_NAME = "schwab_token.sqlite"


def _is_secret_key(key: str) -> bool:
    low = key.lower()
    return any(s in low for s in SECRET_KEYS)


def scrub(value: Any) -> Any:
    """Recursively redact secret-ish dict keys (defense in depth). Lists/scalars pass through;
    only dict VALUES under a secret-ish KEY are replaced with ``'***'``."""
    if isinstance(value, Mapping):
        return {k: ("***" if _is_secret_key(str(k)) else scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def _rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [scrub(dict(r)) for r in rows]


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return scrub(dict(row)) if row is not None else None


class MonitoringRepo:
    """Supplies all monitoring data as safe dicts from the read-only state DB."""

    def __init__(
        self,
        db: ReadOnlyStateDB,
        config_loader: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self._db = db
        self._config_loader = config_loader
        self._token_db = ReadOnlyStateDB(db.path.parent / _TOKEN_DB_NAME)

    # --- system / schedule ------------------------------------------------- #

    def system_status(self) -> dict[str, Any]:
        heartbeat = self._db.query_one(
            "SELECT last_alive_at, scheduler_state, detail FROM heartbeat WHERE id = 1"
        )
        kill = self._db.query_one(
            "SELECT engaged, reason, source, updated_at FROM kill_switch WHERE id = 1"
        )
        kill_dict = _row(kill)
        if kill_dict is not None:
            kill_dict["engaged"] = bool(kill_dict["engaged"])
        return {"heartbeat": _row(heartbeat), "kill_switch": kill_dict}

    def schedule_status(self, limit: int = 50) -> list[dict[str, Any]]:
        return _rows(
            self._db.query(
                "SELECT slot_date, strategy_id, slot_id, status, planned_fire_ts, "
                "drift_seconds, seed, claimed_at, finished_at, error "
                "FROM fired_slot ORDER BY slot_date DESC, planned_fire_ts DESC LIMIT ?",
                (limit,),
            )
        )

    # --- strategies -------------------------------------------------------- #

    def strategy_list(self) -> list[str]:
        rows = self._db.query(
            "SELECT DISTINCT strategy_id FROM ("
            "  SELECT strategy_id FROM orders "
            "  UNION SELECT strategy_id FROM attributed_position "
            "  UNION SELECT strategy_id FROM fired_slot"
            ") WHERE strategy_id IS NOT NULL ORDER BY strategy_id"
        )
        return [str(r["strategy_id"]) for r in rows]

    def strategy_detail(self, strategy_id: str, *, decisions_limit: int = 25) -> dict[str, Any]:
        positions = _rows(
            self._db.query(
                "SELECT strategy_id, symbol, quantity, avg_price FROM attributed_position "
                "WHERE strategy_id = ? ORDER BY symbol",
                (strategy_id,),
            )
        )
        return {
            "strategy_id": strategy_id,
            "attributed_positions": positions,
            "recent_decisions": self.recent_decisions(strategy_id, limit=decisions_limit),
        }

    def recent_decisions(
        self, strategy_id: str | None = None, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        if strategy_id is None:
            rows = self._db.query(
                "SELECT id, ts, cycle_id, strategy_id, kind, payload FROM audit_log "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        else:
            rows = self._db.query(
                "SELECT id, ts, cycle_id, strategy_id, kind, payload FROM audit_log "
                "WHERE strategy_id = ? ORDER BY id DESC LIMIT ?",
                (strategy_id, limit),
            )
        return [self._decision_row(r) for r in rows]

    @staticmethod
    def _decision_row(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        raw = out.pop("payload", None)
        try:
            payload = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            payload = {"raw": raw}
        out["payload"] = scrub(payload)
        scrubbed: dict[str, Any] = scrub(out)
        return scrubbed

    # --- positions / account / pnl ---------------------------------------- #

    def positions_account(self) -> list[dict[str, Any]]:
        return _rows(
            self._db.query(
                "SELECT symbol, quantity, avg_price, market_value, updated_at "
                "FROM positions ORDER BY symbol"
            )
        )

    def positions_attributed(self) -> list[dict[str, Any]]:
        return _rows(
            self._db.query(
                "SELECT strategy_id, symbol, quantity, avg_price FROM attributed_position "
                "ORDER BY strategy_id, symbol"
            )
        )

    def account_summary(self) -> dict[str, Any]:
        snapshot = self._db.query_one(
            "SELECT ts, equity, cash, realized_pnl, unrealized_pnl "
            "FROM equity_snapshots ORDER BY id DESC LIMIT 1"
        )
        counters = self._db.query_one(
            "SELECT trading_date, trades_today, loss_today, start_of_day_equity, updated_at "
            "FROM daily_counters ORDER BY trading_date DESC LIMIT 1"
        )
        return {"latest_equity": _row(snapshot), "today": _row(counters)}

    def pnl_summary(self) -> dict[str, Any]:
        # Account-level P&L from the latest equity snapshot (per-strategy P&L attribution is a
        # future enhancement; positions are attributed, realized P&L per strategy is not yet
        # persisted as its own table — §12).
        return {"account": self.account_summary()["latest_equity"]}

    # --- orders / fills ---------------------------------------------------- #

    def recent_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        return _rows(
            self._db.query(
                "SELECT client_order_id, strategy_id, symbol, side, quantity, order_type, "
                "limit_price, tif, status, broker_order_id, created_at, updated_at "
                "FROM orders ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        )

    def order_fills(self, client_order_id: str) -> list[dict[str, Any]]:
        return _rows(
            self._db.query(
                "SELECT id, client_order_id, broker_order_id, symbol, quantity, price, fees, "
                "ts, status FROM fills WHERE client_order_id = ? ORDER BY id",
                (client_order_id,),
            )
        )

    def recent_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        # No dedicated alerts table (alerts are pushed to Telegram/email); surface the
        # alert-worthy audit events instead.
        rows = self._db.query(
            "SELECT id, ts, cycle_id, strategy_id, kind, payload FROM audit_log "
            "WHERE kind IN ('cycle_error', 'rejected', 'kill_switch_halt') "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [self._decision_row(r) for r in rows]

    # --- token age / config ----------------------------------------------- #

    def token_status(self, now: datetime) -> dict[str, Any]:
        """Refresh-token age + access-token expiry countdown — reading ONLY the timestamp
        columns of the token store (never the token values). Unauthenticated / missing store
        -> ``{authenticated: False}`` (never raises)."""
        try:
            row = self._token_db.query_one(
                "SELECT access_expires_at, refresh_issued_at FROM tokens WHERE id = 1"
            )
        except (FileNotFoundError, sqlite3.Error):
            return {"authenticated": False}
        if row is None:
            return {"authenticated": False}
        try:
            access_expires = datetime.fromisoformat(row["access_expires_at"])
            refresh_issued = datetime.fromisoformat(row["refresh_issued_at"])
        except (TypeError, ValueError):
            return {"authenticated": False}
        return {
            "authenticated": True,
            "access_token_expires_at": access_expires.isoformat(),
            "access_token_seconds_remaining": (access_expires - now).total_seconds(),
            "refresh_token_issued_at": refresh_issued.isoformat(),
            "refresh_token_age_days": (now - refresh_issued).total_seconds() / 86400.0,
        }

    def config_view(self) -> dict[str, Any]:
        """The resolved config with all secret-ish values scrubbed. ``{}`` if unavailable."""
        if self._config_loader is None:
            return {}
        try:
            scrubbed: dict[str, Any] = scrub(dict(self._config_loader()))
            return scrubbed
        except Exception:  # config read/parse errors must not break the monitoring page
            return {}


__all__ = ["SECRET_KEYS", "MonitoringRepo", "scrub"]
