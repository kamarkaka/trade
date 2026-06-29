"""Typed models + parsers for Schwab read responses (design §8.4/§8.5).

Schwab JSON shapes are the [VERIFY] parity checklist. Prices are parsed to Decimal
and epoch-millisecond timestamps to tz-aware UTC. Parsing failures raise
SchwabBadResponseError.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import SchwabBadResponseError


def _dec(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SchwabBadResponseError(f"bad decimal for {field}: {value!r}") from exc


def _opt_dec(value: Any, field: str) -> Decimal | None:
    return None if value is None else _dec(value, field)


def _int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SchwabBadResponseError(f"bad int for {field}: {value!r}") from exc


def _epoch_ms_to_utc(value: Any, field: str) -> datetime:
    return datetime.fromtimestamp(_int(value, field) / 1000.0, tz=UTC)


def _require(mapping: Any, key: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise SchwabBadResponseError(f"missing field {key!r}")
    return mapping[key]


@dataclass(frozen=True)
class SchwabQuote:
    symbol: str
    last: Decimal
    bid: Decimal
    ask: Decimal
    volume: int
    quote_time: datetime
    prev_close: Decimal | None = None


@dataclass(frozen=True)
class SchwabCandle:
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class SchwabPriceHistory:
    symbol: str
    candles: tuple[SchwabCandle, ...]


@dataclass(frozen=True, repr=False)
class AccountNumberMapping:
    account_number: str  # raw (PII — never log; resolve to hash_value for use)
    hash_value: str

    def __repr__(self) -> str:
        # Mask the raw account number so it can't leak via repr/log of the object.
        return f"AccountNumberMapping(account_number='***', hash_value={self.hash_value!r})"


def parse_quote(symbol: str, entry: Any) -> SchwabQuote:
    """Parse one symbol's quote entry (``{"quote": {...}}`` or a flat quote dict)."""
    if not isinstance(entry, dict):
        raise SchwabBadResponseError(f"quote entry for {symbol!r} is not an object")
    q = entry.get("quote", entry)
    return SchwabQuote(
        symbol=symbol,
        last=_dec(_require(q, "lastPrice"), "lastPrice"),
        bid=_dec(_require(q, "bidPrice"), "bidPrice"),
        ask=_dec(_require(q, "askPrice"), "askPrice"),
        volume=_int(q.get("totalVolume", 0), "totalVolume"),
        # [VERIFY] quoteTime is the chosen staleness timestamp (vs tradeTime / etc.).
        quote_time=_epoch_ms_to_utc(_require(q, "quoteTime"), "quoteTime"),
        prev_close=_opt_dec(q.get("closePrice"), "closePrice"),
    )


def parse_price_history(data: Any) -> SchwabPriceHistory:
    symbol = _require(data, "symbol")
    raw_candles = _require(data, "candles")
    if not isinstance(raw_candles, list):
        raise SchwabBadResponseError("candles is not a list")
    candles = tuple(
        SchwabCandle(
            ts=_epoch_ms_to_utc(_require(c, "datetime"), "datetime"),
            open=_dec(_require(c, "open"), "open"),
            high=_dec(_require(c, "high"), "high"),
            low=_dec(_require(c, "low"), "low"),
            close=_dec(_require(c, "close"), "close"),
            volume=_int(c.get("volume", 0), "volume"),
        )
        for c in raw_candles
    )
    return SchwabPriceHistory(symbol=str(symbol), candles=candles)


def parse_account_numbers(data: Any) -> list[AccountNumberMapping]:
    if not isinstance(data, list):
        raise SchwabBadResponseError("accountNumbers response is not a list")
    return [
        AccountNumberMapping(
            account_number=str(_require(item, "accountNumber")),
            hash_value=str(_require(item, "hashValue")),
        )
        for item in data
    ]
