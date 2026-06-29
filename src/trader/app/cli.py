"""Trader command-line interface (Typer).

``status`` loads and reports the validated configuration plus Schwab auth/token
age (and backs the Docker HEALTHCHECK via ``--healthcheck``). ``reauth`` runs the
interactive Schwab OAuth flow (M1). The remaining commands are skeletons fleshed
out by later milestones: ``backtest`` (M2), ``run`` (M3/M4), ``reconcile`` (M4),
``kill`` (M5).
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from trader.clock import RealClock
from trader.config import DEFAULT_CONFIG_PATH, AppConfig, load_config
from trader.schwab.config import SchwabClientConfig, schwab_config_from_env
from trader.schwab.errors import SchwabAuthError, SchwabError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from trader.core import Account, Decision, MarketSnapshot, Position
    from trader.core.protocols import Clock, MarketDataProvider

# Default backtest starting capital until a config-driven account balance exists.
_BACKTEST_STARTING_CASH = "100000"


class _BuyAndHoldStrategy:
    """Placeholder strategy for the M2.11 wiring: buy each universe symbol once and
    hold. Replaced by the StrategyRegistry + real strategies in M3.6/M6."""

    def __init__(self, quantity: int = 10) -> None:
        self._quantity = quantity

    def decide(
        self,
        snapshot: MarketSnapshot,
        positions: Sequence[Position],
        account: Account,
        data: MarketDataProvider,
        clock: Clock,
    ) -> Sequence[Decision]:
        from trader.core import Decision
        from trader.core.enums import Action

        held = {p.symbol for p in positions if p.quantity != 0}
        return [
            Decision(action=Action.BUY, symbol=symbol, quantity=self._quantity)
            for symbol in snapshot.quotes
            if symbol not in held
        ]


app = typer.Typer(
    help="Automated equity trader.",
    no_args_is_help=True,
    add_completion=False,
)

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to the YAML config file.")]


def _load(config: Path) -> AppConfig:
    """Load + validate config, exiting non-zero with a clean message on error."""
    try:
        return load_config(config)
    except Exception as exc:  # surface config errors as a clean CLI failure
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(1) from exc


def _heartbeat_fresh(cfg: AppConfig) -> bool:
    """True iff the daemon's heartbeat exists and is fresh (backs ``--healthcheck``).

    Stale if older than two heartbeat intervals (tolerates one missed beat). Reads
    defensively: a missing DB / unmigrated state / unreadable row is "not alive" rather
    than an error, so the probe never crashes the container."""
    from trader.observability.heartbeat import Heartbeat
    from trader.state.db import read_only_connect

    db_path = Path(cfg.observability.db_path)
    if not db_path.exists():
        return False
    # The daemon must touch the heartbeat at least every heartbeat_minutes (wired in
    # M4.7); 2x tolerates a single missed beat.
    max_age = cfg.alerting.heartbeat_minutes * 60 * 2
    try:
        conn = read_only_connect(db_path)
    except Exception:
        return False
    try:
        return Heartbeat(conn, clock=RealClock(), max_age_seconds=max_age).is_alive()
    except Exception:
        return False  # any unexpected read error => unhealthy, never a crashing probe
    finally:
        conn.close()


def _schwab_config(cfg: AppConfig, *, require_credentials: bool = False) -> SchwabClientConfig:
    """Build the Schwab client config from env + a couple of AppConfig settings."""
    default_token_store = Path(cfg.observability.db_path).parent / "schwab_token.sqlite"
    return schwab_config_from_env(
        default_token_store=default_token_store,
        rate_limit_per_min=cfg.execution.rate_limit_per_min,
        require_credentials=require_credentials,
    )


def _auth_status_line(cfg: AppConfig) -> str:
    """One-line Schwab auth/token-age summary for ``status`` (no network)."""
    from trader.auth.token_store import TokenStore

    schwab_cfg = _schwab_config(cfg)
    # Read-only: never create the token store just to report status.
    if not schwab_cfg.token_store_path.exists():
        return "auth: not authenticated (run `trader reauth`)"
    tok = TokenStore(schwab_cfg.token_store_path).load()
    if tok is None:
        return "auth: not authenticated (run `trader reauth`)"
    remaining = schwab_cfg.refresh_token_max_age_days - tok.refresh_age_days(RealClock())
    if remaining <= 0:
        return "auth: refresh token EXPIRED — run `trader reauth`"
    return f"auth: authenticated; refresh token expires in ~{remaining:.1f} day(s)"


@app.command()
def status(
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    healthcheck: Annotated[
        bool, typer.Option("--healthcheck", help="Exit 0 if healthy (for the Docker HEALTHCHECK).")
    ] = False,
) -> None:
    """Show mode, strategies, and auth status (or a healthcheck exit code)."""
    cfg = _load(config)
    if healthcheck:
        # Docker HEALTHCHECK (§16.1): fresh daemon heartbeat => exit 0, stale/missing =>
        # non-zero so the container is marked unhealthy and restarted.
        raise typer.Exit(0 if _heartbeat_fresh(cfg) else 1)
    typer.echo(f"mode: {cfg.mode.value}")
    typer.echo(f"strategies: {', '.join(s.id for s in cfg.strategies)}")
    typer.echo(_auth_status_line(cfg))


@app.command()
def run(
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    once: Annotated[
        bool, typer.Option("--once", help="Fire each slot once and exit (no blocking loop).")
    ] = False,
) -> None:
    """Run the PAPER trading daemon (SimBroker against live quotes; no real orders)."""
    import os
    import time as _time

    from trader.broker import SimBroker
    from trader.core.enums import Mode
    from trader.observability.alerting import build_alerter
    from trader.observability.heartbeat import Heartbeat
    from trader.orchestrator.cycle import Orchestrator, SqliteAuditSink
    from trader.orchestrator.lock import GlobalCycleLock
    from trader.risk.gate import RiskManager
    from trader.risk.kill_switch import KillSwitch
    from trader.scheduler.calendar import TradingCalendar
    from trader.scheduler.daemon import SchedulerDaemon
    from trader.sizing.sizer import size_decision
    from trader.state.attribution import AttributionLedger
    from trader.state.db import connect
    from trader.state.ledger import FiredSlotLedger
    from trader.state.migrate import run_migrations
    from trader.strategy import load_bindings

    cfg = _load(config)
    # SAFETY GATE (pre-M5): the daemon never places real orders. Refuse live.
    if cfg.mode is Mode.LIVE:
        typer.echo(
            "run error: live mode is refused until M5 (no real orders); set mode=paper", err=True
        )
        raise typer.Exit(1)
    if cfg.mode is not Mode.PAPER:
        typer.echo(f"run error: `run` requires mode=paper, got {cfg.mode.value}", err=True)
        raise typer.Exit(1)

    schedule, bindings = load_bindings(cfg)
    if not any(b.enabled for b in bindings):
        typer.echo("run error: no enabled strategy in config", err=True)
        raise typer.Exit(1)

    # Paper quotes come from the read-only live Schwab feed -> needs credentials.
    try:
        schwab_cfg = _schwab_config(cfg, require_credentials=True)
    except SchwabAuthError as exc:
        typer.echo(f"run error: {exc}", err=True)
        raise typer.Exit(1) from exc

    import httpx

    from trader.auth.token_store import TokenStore
    from trader.data.schwab_market_data import SchwabMarketData
    from trader.schwab.endpoints import SchwabClient
    from trader.schwab.http import SchwabHttp

    clock = RealClock()
    calendar = TradingCalendar(code=schedule.market_calendar, tz=schedule.timezone)
    state = connect(Path(cfg.observability.db_path))
    run_migrations(state)
    cash = Decimal(_BACKTEST_STARTING_CASH)

    # Redundant alerting + per-strategy risk overrides assembled once for the run.
    alerter = build_alerter(cfg.alerting.channels, environ=os.environ)
    overrides = {b.strategy_id: b.risk_overrides for b in bindings if b.risk_overrides}
    risk = RiskManager(
        account_config=cfg.risk,
        clock=clock,
        overrides_by_strategy=overrides,
        default_policy=cfg.risk.conflict_policy,
    )
    # The heartbeat gets its OWN connection so its dedicated executor thread never shares
    # a sqlite3.Connection with the cycle worker (cross-thread concurrent use is unsafe).
    heartbeat = Heartbeat(
        connect(Path(cfg.observability.db_path)),
        clock=clock,
        max_age_seconds=cfg.alerting.heartbeat_minutes * 60 * 2,
        alerter=alerter,
    )

    with httpx.Client(timeout=schwab_cfg.request_timeout_seconds) as client:
        http = SchwabHttp(schwab_cfg, client, TokenStore(schwab_cfg.token_store_path), clock=clock)
        data = SchwabMarketData(SchwabClient(http), clock)
        broker = SimBroker(data, clock, starting_cash=cash)  # PAPER: SimBroker only, never real
        attribution = AttributionLedger(state)
        # Read the persisted kill switch fresh each cycle: an engage (CLI or auto-trip) halts
        # the daemon at the next cycle start AND pre-submit (gate). Its own connection so the
        # worker thread never shares one cross-thread.
        kill_switch = KillSwitch(connect(Path(cfg.observability.db_path)), alerter=alerter)
        orchestrator = Orchestrator(
            broker=broker,
            data=data,
            clock=clock,
            cycle_lock=GlobalCycleLock(),
            attribution=attribution,
            sizer=lambda d, sid: size_decision(d, sid, cfg.execution),
            risk=risk,  # the real fail-closed gate is the single chokepoint
            audit=SqliteAuditSink(state),  # durable audit chain
            kill_switch=kill_switch.is_engaged,
        )
        # NOTE: reconcile-against-broker-truth on startup is wired in M5. It is meaningful
        # only for a broker whose positions survive a restart; SimBroker is in-memory (always
        # flat on restart), so trueing the durable attribution ledger up to it would corrupt
        # intent and fire a spurious mismatch alert every restart. In-session reconcile lands
        # with the durable SchwabBroker (M5).
        daemon = SchedulerDaemon(
            bindings=bindings,
            schedule=schedule,
            calendar=calendar,
            ledger=FiredSlotLedger(state),
            orchestrator=orchestrator,
            clock=clock,
            alerter=alerter,
            heartbeat=heartbeat,
        )
        if once:
            for binding in bindings:
                for slot in binding.slots if binding.enabled else ():
                    daemon.fire(binding.strategy_id, slot.slot_id)  # callbacks built at init
            typer.echo("run: one tick complete (--once)")
            return
        daemon.start()
        typer.echo(
            f"run: paper daemon started ({len(daemon.scheduler.get_jobs())} jobs); Ctrl-C to stop"
        )
        try:
            while True:
                _time.sleep(1)
        except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
            typer.echo("run: stopping…")
        finally:
            daemon.stop()


@app.command()
def backtest(
    start: Annotated[str, typer.Option("--start", help="Inclusive start date (YYYY-MM-DD).")],
    end: Annotated[str, typer.Option("--end", help="Inclusive end date (YYYY-MM-DD).")],
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    out: Annotated[str, typer.Option("--out", help="Output directory for reports.")] = "reports",
) -> None:
    """Run a single-strategy backtest over cached data; write report + manifest."""
    import json

    from trader.backtest import BacktestEngine, Portfolio, build_manifest, write_manifest
    from trader.backtest.report import BacktestReport
    from trader.broker import SimBroker
    from trader.clock import VirtualClock
    from trader.data.cache import ParquetCache
    from trader.data.historical import HistoricalDataProvider

    cfg = _load(config)
    start_d = _parse_day(start, "--start", context="backtest").date()
    end_d = _parse_day(end, "--end", context="backtest").date()
    if end_d < start_d:
        typer.echo("backtest error: --end must be on or after --start", err=True)
        raise typer.Exit(1)

    enabled = [b for b in cfg.strategies if b.enabled]
    if not enabled:
        typer.echo("backtest error: no enabled strategy in config", err=True)
        raise typer.Exit(1)
    binding = enabled[0]
    universe = list(binding.universe)
    # M2 simplification: slot "HH:MM" is treated as UTC (engine combines with UTC).
    # DST-aware localization to the config timezone arrives in M3.3/M3.4 (design §7.1).
    slots = [datetime.strptime(s.time, "%H:%M").time() for s in binding.slots]
    seed = cfg.schedule.base_seed or 0
    cash = Decimal(_BACKTEST_STARTING_CASH)

    clock = VirtualClock(datetime.combine(start_d, time.min, tzinfo=UTC))
    cache = ParquetCache(cfg.observability.data_cache)
    data = HistoricalDataProvider(cache, clock)
    broker = SimBroker(data, clock, starting_cash=cash)
    portfolio = Portfolio(cash)
    engine = BacktestEngine(clock=clock, data=data, broker=broker, portfolio=portfolio)
    result = engine.run(
        _BuyAndHoldStrategy(),
        universe=universe,
        slots=slots,
        start=start_d,
        end=end_d,
        strategy_id=binding.id,
        seed=seed,
    )

    data_hashes = {symbol: cache.content_hash(symbol) for symbol in universe}
    manifest = build_manifest(cfg, data_hashes, seed)
    report = BacktestReport.build(result.fills, result.equity_curve, manifest)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")  # microseconds avoid collisions
    out_dir = Path(out) / f"{binding.id}-{start_d}-{end_d}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_manifest(manifest, out_dir / "manifest.json")
    if not result.fills:
        typer.echo(
            "backtest warning: no fills produced — check that data is cached for "
            f"{universe} over {start_d}..{end_d}",
            err=True,
        )
    typer.echo(f"backtest: {len(result.fills)} fills; report written to {out_dir}")


@app.command()
def reauth(config: ConfigOpt = DEFAULT_CONFIG_PATH) -> None:
    """Re-authenticate with Schwab via the interactive browser OAuth flow."""
    import httpx

    from trader.auth.authenticator import Authenticator
    from trader.auth.token_store import TokenStore

    cfg = _load(config)
    try:
        schwab_cfg = _schwab_config(cfg, require_credentials=True)
    except SchwabAuthError as exc:
        typer.echo(f"reauth error: {exc}", err=True)
        raise typer.Exit(1) from exc

    store = TokenStore(schwab_cfg.token_store_path)
    typer.echo("Opening browser for Schwab authorization…")
    try:
        with httpx.Client(timeout=schwab_cfg.request_timeout_seconds) as client:
            auth = Authenticator(schwab_cfg, client, store, clock=RealClock())
            auth.interactive_authorize()
    except SchwabError as exc:
        typer.echo(f"reauth failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo("Authenticated; tokens saved.")


@app.command()
def kill(
    on: Annotated[
        bool, typer.Option("--on/--off", help="Engage or release the kill switch.")
    ] = False,
    reason: Annotated[
        str, typer.Option("--reason", help="Why the switch is being engaged (for the audit/log).")
    ] = "manual kill via CLI",
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
) -> None:
    """Engage/release the persisted kill switch (halts all new orders; survives restarts)."""
    from trader.risk.kill_switch import KillSwitch
    from trader.state.db import connect
    from trader.state.migrate import run_migrations

    cfg = _load(config)
    conn = connect(Path(cfg.observability.db_path))
    run_migrations(conn)
    switch = KillSwitch(conn)
    if on:
        newly = switch.engage(reason, source="cli")
        typer.echo(f"kill switch ENGAGED ({reason})" if newly else "kill switch already engaged")
    else:
        switch.disengage(source="cli")
        typer.echo("kill switch released")


@app.command()
def reconcile(config: ConfigOpt = DEFAULT_CONFIG_PATH) -> None:
    """Reconcile local state with the broker (implemented in M4)."""
    _load(config)
    typer.echo("reconcile: not implemented yet (reconciliation arrives in M4)")


data_app = typer.Typer(help="Historical data cache management.", no_args_is_help=True)
app.add_typer(data_app, name="data")


def _parse_day(value: str, name: str, *, context: str = "data fetch") -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        typer.echo(f"{context} error: {name} must be YYYY-MM-DD, got {value!r}", err=True)
        raise typer.Exit(1) from exc


@data_app.command("fetch")
def data_fetch(
    symbols: Annotated[str, typer.Option("--symbols", help="Comma-separated tickers.")],
    start: Annotated[str, typer.Option("--start", help="Inclusive start date (YYYY-MM-DD).")],
    end: Annotated[str, typer.Option("--end", help="Inclusive end date (YYYY-MM-DD).")],
    freq: Annotated[str, typer.Option("--freq", help="Bar frequency.")] = "daily",
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
) -> None:
    """Fetch daily candles from Schwab into the Parquet cache (read-only, missing-only)."""
    import httpx

    from trader.auth.token_store import TokenStore
    from trader.data.cache import ParquetCache
    from trader.data.ingest import ingest_daily
    from trader.data.schwab_market_data import SchwabMarketData
    from trader.schwab.endpoints import SchwabClient
    from trader.schwab.http import SchwabHttp

    cfg = _load(config)
    if freq != "daily":
        typer.echo(f"data fetch error: only --freq daily is supported, got {freq!r}", err=True)
        raise typer.Exit(1)
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not syms:
        typer.echo("data fetch error: no symbols given", err=True)
        raise typer.Exit(1)
    start_dt = _parse_day(start, "--start")
    end_day = _parse_day(end, "--end")
    if end_day < start_dt:
        typer.echo("data fetch error: --end must be on or after --start", err=True)
        raise typer.Exit(1)
    # --start/--end are inclusive day boundaries; extend end to end-of-day so the end
    # day's (midnight-stamped) daily bar is fetched and a single-day window is non-empty.
    end_dt = end_day + timedelta(days=1) - timedelta(seconds=1)

    # Resolve credentials first so a missing-creds run fails before touching the cache.
    try:
        schwab_cfg = _schwab_config(cfg, require_credentials=True)
    except SchwabAuthError as exc:
        typer.echo(f"data fetch error: {exc}", err=True)
        raise typer.Exit(1) from exc

    store = TokenStore(schwab_cfg.token_store_path)
    cache = ParquetCache(cfg.observability.data_cache)
    clock = RealClock()
    try:
        with httpx.Client(timeout=schwab_cfg.request_timeout_seconds) as client:
            http = SchwabHttp(schwab_cfg, client, store, clock=clock)
            provider = SchwabMarketData(SchwabClient(http), clock)
            results = ingest_daily(provider, cache, syms, start_dt, end_dt, clock=clock)
    except SchwabError as exc:
        typer.echo(f"data fetch failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    for r in results:
        typer.echo(f"{r.symbol}: {r.bars_written} bars across {r.ranges_fetched} range(s)")
