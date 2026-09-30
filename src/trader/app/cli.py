"""Trader command-line interface (Typer).

``status`` loads and reports the validated configuration plus Schwab auth/token
age (and backs the Docker HEALTHCHECK via ``--healthcheck``). ``reauth`` runs the
interactive Schwab OAuth flow (M1). The remaining commands are skeletons fleshed
out by later milestones: ``backtest`` (M2), ``run`` (M3/M4), ``reconcile`` (M4),
``kill`` (M5).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from trader.broker import FeesModel, SchwabBroker
from trader.clock import RealClock
from trader.config import DEFAULT_CONFIG_PATH, AppConfig, load_config
from trader.execution.idempotency import OrderRepository
from trader.execution.schwab_reconciler import SchwabOrderReconciler
from trader.observability.logging import configure_logging
from trader.schwab.config import SchwabClientConfig, schwab_config_from_env
from trader.schwab.errors import SchwabAuthError, SchwabError
from trader.schwab.http import SchwabHttp

# Default backtest starting capital until a config-driven account balance exists.
_BACKTEST_STARTING_CASH = "100000"


app = typer.Typer(
    help="Automated equity trader.",
    no_args_is_help=True,
    add_completion=False,
)

# OFFLINE research tools (parameter sweeps). Strictly read-only / no broker — see
# trader.research (structurally cannot trade; enforced by tests/unit/test_param_sweep.py).
research_app = typer.Typer(
    help="OFFLINE research tools — reads cached data only; never trades.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(research_app, name="research")

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to the YAML config file.")]


@app.callback()
def _configure() -> None:
    # Install the scrubbed logging pipeline before any command runs (never structlog's
    # defaults, which render tracebacks with frame locals and skip secret scrubbing).
    configure_logging(os.environ.get("TRADER_LOG_LEVEL", "INFO"))


def _load(config: Path) -> AppConfig:
    """Load + validate config, exiting non-zero with a clean message on error."""
    try:
        cfg = load_config(config)
    except Exception as exc:  # surface config errors as a clean CLI failure
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(1) from exc
    configure_logging(
        os.environ.get("TRADER_LOG_LEVEL", "INFO"),
        json_output=cfg.observability.log_format == "json",
    )
    return cfg


def _token_valid(cfg: AppConfig) -> bool:
    """True iff a non-expired Schwab refresh token is on disk (no network)."""
    from trader.auth.token_store import TokenStore

    schwab_cfg = _schwab_config(cfg)
    if not schwab_cfg.token_store_path.exists():
        return False
    tok = TokenStore(schwab_cfg.token_store_path).load()
    if tok is None:
        return False
    return schwab_cfg.refresh_token_max_age_days - tok.refresh_age_days(RealClock()) > 0


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


@dataclass(frozen=True)
class _LiveAccount:
    """The live Schwab account's order path: broker adapter + order reconciler."""

    broker: SchwabBroker
    reconciler: SchwabOrderReconciler
    account_hash: str


def _live_account(
    cfg: AppConfig,
    http: SchwabHttp,
    repo: OrderRepository,
    *,
    clock: RealClock,
    fees: FeesModel | None,
    command: str,
) -> _LiveAccount:
    """Resolve the (single) hashed account and build its broker + reconciler. Refuses on
    ambiguity rather than act on the wrong account with real money."""
    from trader.schwab.endpoints import SchwabClient
    from trader.schwab.orders import SchwabTradingClient

    # The raw account number is PII and never used directly; only its hash.
    mappings = SchwabClient(http).get_account_numbers()
    if len(mappings) != 1:
        typer.echo(
            f"{command} error: expected exactly 1 Schwab account, found {len(mappings)}; "
            "explicit multi-account selection is required before live",
            err=True,
        )
        raise typer.Exit(1)
    trading = SchwabTradingClient(http)
    account_hash = mappings[0].hash_value
    broker = SchwabBroker(
        trading, account_hash, clock=clock, fees=fees, client_id_for=repo.client_id_for
    )
    reconciler = SchwabOrderReconciler(
        trading,
        account_hash,
        clock=clock,
        bound_broker_ids=repo.bound_broker_ids,
        bound_orders_created_between=repo.bound_orders_created_between,
        awaiting_resolution=repo.awaiting_resolution,
        consistency_window=timedelta(seconds=cfg.execution.reconcile_window_seconds),
    )
    return _LiveAccount(broker, reconciler, account_hash)


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
    confirm_live: Annotated[
        bool,
        typer.Option(
            "--confirm-live",
            help="Second go-live signal (or set TRADER_CONFIRM_LIVE=I_UNDERSTAND). REAL MONEY.",
        ),
    ] = False,
) -> None:
    """Run the trading daemon. PAPER (default) uses SimBroker against live quotes (no real
    orders). LIVE places REAL orders and requires mode=live PLUS a second confirmation."""
    import time as _time
    import uuid
    from zoneinfo import ZoneInfo

    from trader.app.live_guard import announce_live, live_confirmed, live_preflight
    from trader.broker import FeesModel, SimBroker
    from trader.broker.schwab_broker import TRANSIENT_READ_ERRORS
    from trader.core.enums import Mode
    from trader.core.protocols import Broker
    from trader.execution.executor import DurableOrderExecutor, in_memory_reconciler
    from trader.execution.idempotency import OrderRepository, Reconciler
    from trader.execution.poller import DEFAULT_RETRYABLE, PollPolicy
    from trader.observability.alerting import build_alerter
    from trader.observability.heartbeat import Heartbeat
    from trader.observability.logging import get_logger
    from trader.orchestrator.cycle import Orchestrator, SqliteAuditSink
    from trader.orchestrator.lock import GlobalCycleLock
    from trader.risk.gate import RiskManager
    from trader.risk.kill_switch import KillSwitch, tripping_day_state
    from trader.scheduler.calendar import TradingCalendar
    from trader.scheduler.daemon import SchedulerDaemon
    from trader.sizing.sizer import size_decision
    from trader.state.attribution import AttributionLedger
    from trader.state.daily import DailyCounters
    from trader.state.db import connect
    from trader.state.lease import TradingLease
    from trader.state.ledger import FiredSlotLedger
    from trader.state.migrate import run_migrations
    from trader.strategy import load_bindings

    cfg = _load(config)
    if cfg.mode not in (Mode.PAPER, Mode.LIVE):
        typer.echo(
            f"run error: `run` requires mode=paper or mode=live, got {cfg.mode.value}", err=True
        )
        raise typer.Exit(1)
    is_live = cfg.mode is Mode.LIVE
    # FIRST signal is mode: live in config; SECOND is this out-of-band confirmation.
    if is_live and not live_confirmed(confirm_flag=confirm_live, environ=dict(os.environ)):
        typer.echo(
            "run error: live mode requires a SECOND confirmation (REAL MONEY): set "
            "TRADER_CONFIRM_LIVE=I_UNDERSTAND or pass --confirm-live",
            err=True,
        )
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
    # One sender per state DB: the daemon holds the trading lease for its whole life (the
    # kernel frees it if the process dies), and `trader reconcile` refuses while it's held.
    lease = TradingLease(Path(cfg.observability.db_path))
    if not lease.try_acquire():
        typer.echo(
            "run error: another trader process holds the trading lease for this database",
            err=True,
        )
        raise typer.Exit(3)
    state = connect(Path(cfg.observability.db_path))
    run_migrations(state)
    cash = Decimal(_BACKTEST_STARTING_CASH)

    # Redundant alerting + per-strategy risk overrides assembled once for the run.
    alerter = build_alerter(cfg.alerting.channels, environ=os.environ)
    overrides = {b.strategy_id: b.risk_overrides for b in bindings if b.risk_overrides}

    if is_live:
        # Conservative go-live preflight: refuse to start a REAL-MONEY run unless the rollout
        # is safe. Validates EFFECTIVE per-strategy caps (overrides can't exceed the ceiling),
        # an alert channel (never silent), default-deny allowlist, kill switch off, valid
        # token -- and, until M5.7 wires the idempotent submit path, refuses live entirely.
        problems = live_preflight(
            cfg,
            bindings,
            kill_switch_engaged=KillSwitch(connect(Path(cfg.observability.db_path))).is_engaged(),
            token_valid=_token_valid(cfg),
            alert_channel_count=len(alerter._channels),
        )
        if problems:
            for p in problems:
                typer.echo(f"live preflight FAILED [{p.check}]: {p.detail}", err=True)
            raise typer.Exit(1)
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
        fees = FeesModel.from_config(cfg.execution.fees_model)  # same estimate paper + live
        repo = OrderRepository(state)
        attribution = AttributionLedger(state)  # same connection: atomic completion
        broker: Broker
        if is_live:
            live = _live_account(cfg, http, repo, clock=clock, fees=fees, command="run")
            # Live counters persist across restarts; the live state database serves one account.
            counters_scope = "live"
            broker = live.broker
            reconciler: Reconciler = live.reconciler
            poll_policy = PollPolicy(timeout_seconds=cfg.execution.poll_timeout_seconds)
            retryable = TRANSIENT_READ_ERRORS
            # Live state is NEVER silent: log it loud and alert at startup (design §10).
            get_logger("cli").warning("STARTING IN LIVE MODE — REAL ORDERS ENABLED")
            announce_live(alerter)
        else:
            # PAPER: SimBroker, never real. It can't change state while we poll, so read once
            # and cancel any remainder (a resting limit order is never left WORKING).
            # A paper process starts from a fresh, in-memory SimBroker while the order rows and
            # counters are durable, so both are namespaced per process: a broker id may belong
            # to only one order row, and a persisted paper start-of-day equity would read a
            # restart as a loss.
            run_id = uuid.uuid4().hex[:8]
            sim = SimBroker(data, clock, starting_cash=cash, fees=fees, id_prefix=f"SIM-{run_id}")
            counters_scope = f"paper:{run_id}"
            broker = sim
            reconciler = in_memory_reconciler(sim.find_by_client_id)
            poll_policy = PollPolicy(timeout_seconds=0)
            retryable = DEFAULT_RETRYABLE
        # Read the persisted kill switch fresh each cycle: an engage (CLI or auto-trip) halts
        # the daemon at the next cycle start AND pre-submit (gate). Its own connection so the
        # worker thread never shares one cross-thread.
        kill_switch = KillSwitch(connect(Path(cfg.observability.db_path)), alerter=alerter)
        # Durable execution for paper AND live: write-ahead order rows, at most one send per
        # client_order_id, bounded polling, and atomic completion (orders + fills +
        # attribution) — so the paper soak exercises the live order path.
        executor = DurableOrderExecutor(
            broker=broker,
            repo=repo,
            attribution=attribution,
            reconcile=reconciler,
            poll_policy=poll_policy,
            retryable=retryable,
            # An order of unknown/unresolved fate halts all trading until reconciled.
            on_uncertain=lambda reason: kill_switch.engage(reason, source="auto"),
        )
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
            executor=executor,
            # Real daily rails: persisted start-of-day equity + today's orders (exchange tz).
            # A daily-loss breach auto-engages the kill switch as soon as a cycle sees it.
            day_state_provider=tripping_day_state(
                DailyCounters(
                    state,
                    tz=ZoneInfo(schedule.timezone),
                    scope=counters_scope,
                    sessions=calendar.sessions,  # PDT: rolling window of exchange sessions
                    pdt_window_days=cfg.risk.pdt_window_days,
                ).day_state,
                kill_switch,
                cfg.risk,
            ),
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
        mode_label = "LIVE" if is_live else "paper"
        if once:
            for binding in bindings:
                for slot in binding.slots if binding.enabled else ():
                    daemon.fire(binding.strategy_id, slot.slot_id)  # callbacks built at init
            typer.echo(f"run: one {mode_label} tick complete (--once)")
            return
        daemon.start()
        typer.echo(
            f"run: {mode_label} daemon started ({len(daemon.scheduler.get_jobs())} jobs); "
            "Ctrl-C to stop"
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
    out: Annotated[
        str, typer.Option("--out-dir", "--out", help="Output directory for reports.")
    ] = "reports",
    report_json: Annotated[
        bool, typer.Option("--report-json/--no-report-json", help="Write report.json.")
    ] = True,
    report_html: Annotated[
        bool, typer.Option("--report-html/--no-report-html", help="Write report.html.")
    ] = True,
) -> None:
    """Run a multi-strategy backtest over CACHED data and write a per-strategy + combined
    report (JSON/HTML + manifest). This path is fully OFFLINE and deterministic — it never
    touches the broker or the network (no real-money path; design safety gate)."""
    from trader.backtest import write_manifest
    from trader.backtest.runner import run_backtest_report

    cfg = _load(config)
    start_d = _parse_day(start, "--start", context="backtest").date()
    end_d = _parse_day(end, "--end", context="backtest").date()
    if end_d < start_d:
        typer.echo("backtest error: --end must be on or after --start", err=True)
        raise typer.Exit(1)

    try:
        run = run_backtest_report(cfg, start_d, end_d)
    except ValueError as exc:
        typer.echo(f"backtest error: {exc}", err=True)
        raise typer.Exit(1) from exc

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")  # microseconds avoid collisions
    out_dir = Path(out) / f"{start_d}-{end_d}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if report_json:
        run.doc.to_json(out_dir / "report.json")
    if report_html:
        run.doc.to_html(out_dir / "report.html")
    write_manifest(run.manifest, out_dir / "manifest.json")

    if run.num_fills == 0:
        typer.echo(
            "backtest warning: no fills produced — check that data is cached over "
            f"{start_d}..{end_d}",
            err=True,
        )
    _print_backtest_summary(run.doc.data)
    typer.echo(f"backtest: {run.num_fills} fills; report written to {out_dir}")


def _print_backtest_summary(data: dict) -> None:  # type: ignore[type-arg]
    """Compact per-strategy + combined table to stdout (no files)."""

    def _row(name: str, trades: object, hit: object, ret: object, dd: object) -> str:
        # Fixed widths with explicit gaps so signed 8dp values (e.g. -0.00300000) never
        # collide with the next column.
        return f"{name:<16}{trades!s:>8}  {hit!s:>14}  {ret!s:>14}  {dd!s:>12}"

    combined = data["combined"]
    typer.echo("")
    typer.echo(_row("strategy", "trades", "hit_rate", "total_ret", "max_dd"))
    typer.echo("-" * 70)
    typer.echo(
        _row(
            "COMBINED",
            combined["num_trades"],
            combined["hit_rate"] or "—",
            combined["total_return"],
            combined["max_drawdown_pct"],
        )
    )
    for sid, block in data["per_strategy"].items():
        em = block.get("equity_metrics")
        typer.echo(
            _row(
                sid,
                block["num_trades"],
                block["hit_rate"] or "—",
                em["total_return"] if em else "—",
                em["max_drawdown_pct"] if em else "—",
            )
        )


def _parse_grid(grid: list[str]) -> dict[str, list[object]]:
    """Parse ``key=v1,v2`` options into ``{key: [values]}`` (int when integral, else float)."""

    def _coerce(token: str) -> object:
        try:
            return int(token)
        except ValueError:
            return float(token)

    out: dict[str, list[object]] = {}
    for spec in grid:
        if "=" not in spec:
            raise ValueError(f"bad --grid {spec!r}; expected key=v1,v2")
        key, _, values = spec.partition("=")
        key = key.strip()
        if key in out:
            raise ValueError(f"duplicate --grid key {key!r} (pass all values in one key=...)")
        try:
            out[key] = [_coerce(v.strip()) for v in values.split(",") if v.strip()]
        except ValueError as exc:
            raise ValueError(f"bad --grid value in {spec!r}: {exc}") from exc
        if not out[key]:
            raise ValueError(f"--grid {spec!r} has no values")
    return out


@research_app.command("sweep")
def research_sweep(
    strategy: Annotated[str, typer.Option("--strategy", help="Strategy family to sweep.")],
    grid: Annotated[
        list[str],
        typer.Option("--grid", help="Param grid, repeatable: key=v1,v2 (e.g. lookback=10,20)."),
    ],
    data: Annotated[str, typer.Option("--data", help="Path to the read-only Parquet data cache.")],
    symbols: Annotated[
        str, typer.Option("--symbols", help="Comma-separated symbols (default: all in cache).")
    ] = "",
    start: Annotated[str, typer.Option("--start", help="Inclusive start date (YYYY-MM-DD).")] = "",
    end: Annotated[str, typer.Option("--end", help="Inclusive end date (YYYY-MM-DD).")] = "",
    out: Annotated[
        str, typer.Option("--out", help="Output directory for the results CSV.")
    ] = "research_results",
) -> None:
    """RESEARCH ONLY — no orders, no broker, offline. Vectorized parameter sweep over CACHED
    bars to SHORTLIST params; re-validate winners with `trader backtest` before any live use."""
    # Banner first: make it unmistakable this path cannot trade.
    typer.echo("=== RESEARCH ONLY — no orders, no broker, offline ===")
    from trader.research import param_sweep  # imports ONLY pandas/numpy/stdlib (no broker)

    try:
        param_grid = _parse_grid(grid)
    except ValueError as exc:
        typer.echo(f"research error: {exc}", err=True)
        raise typer.Exit(1) from exc

    start_d = _parse_day(start, "--start", context="research").date() if start else None
    end_d = _parse_day(end, "--end", context="research").date() if end else None

    syms = [s.strip() for s in symbols.split(",") if s.strip()] or param_sweep.available_symbols(
        data
    )
    if not syms:
        typer.echo(f"research error: no symbols given and none cached under {data}", err=True)
        raise typer.Exit(1)

    bars, missing = param_sweep.load_bars_for_symbols(data, syms, start_d, end_d)
    for sym in missing:
        typer.echo(
            f"research warning: no cached data for {sym} — skipped (never fetched)", err=True
        )
    if not bars:
        typer.echo("research error: no cached data for any requested symbol", err=True)
        raise typer.Exit(1)

    try:
        results = param_sweep.sweep(strategy, param_grid, bars)
    except ValueError as exc:
        typer.echo(f"research error: {exc}", err=True)
        raise typer.Exit(1) from exc

    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    grid_tag = "_".join(sorted(param_grid))
    out_path = out_dir / f"sweep-{strategy}-{grid_tag}.csv"
    results.to_csv(out_path, index=False)
    typer.echo(f"research: {len(results)} param combo(s) over {len(bars)} symbol(s)")
    typer.echo(results.to_string(index=False) if not results.empty else "(no results)")
    typer.echo(f"research: results written to {out_path}")


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
def reconcile(
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    mark_not_placed: Annotated[
        list[str] | None,
        typer.Option(
            "--mark-not-placed",
            help="Operator override: CLIENT_ORDER_ID verified (in Schwab's records) NOT placed.",
        ),
    ] = None,
    adopt: Annotated[
        list[str] | None,
        typer.Option(
            "--adopt",
            help="Operator override: CLIENT_ORDER_ID=BROKER_ORDER_ID verified at Schwab.",
        ),
    ] = None,
) -> None:
    """Settle open orders against the LIVE Schwab account, then true positions.

    Never places an order (an unfinished order's remainder may be cancelled). Requires the
    trading lease, so the daemon must be stopped. Exit codes: 0 clean, 2 unresolved or
    divergent, 3 another trader process holds the lease."""
    import httpx

    from trader.auth.token_store import TokenStore
    from trader.broker.schwab_broker import TRANSIENT_READ_ERRORS
    from trader.core.enums import Mode
    from trader.execution.account_reconcile import reconcile_account, summary_lines
    from trader.execution.poller import PollPolicy
    from trader.schwab.http import SchwabHttp
    from trader.state.attribution import AttributionLedger
    from trader.state.db import connect
    from trader.state.lease import TradingLease
    from trader.state.migrate import run_migrations

    cfg = _load(config)
    if cfg.mode is not Mode.LIVE:
        typer.echo(
            "reconcile: paper mode has no broker account to reconcile (SimBroker is in-memory)"
        )
        return
    state_path = Path(cfg.observability.db_path)
    lease = TradingLease(state_path)
    if not lease.try_acquire():
        typer.echo(
            "reconcile error: another trader process (the daemon?) holds the trading lease "
            "for this database; stop it first",
            err=True,
        )
        raise typer.Exit(3)
    try:
        conn = connect(state_path)
        run_migrations(conn)
        repo = OrderRepository(conn)
        _apply_overrides(repo, mark_not_placed or [], adopt or [])
        try:
            schwab_cfg = _schwab_config(cfg, require_credentials=True)
        except SchwabAuthError as exc:
            typer.echo(f"reconcile error: {exc}", err=True)
            raise typer.Exit(1) from exc
        clock = RealClock()
        with httpx.Client(timeout=schwab_cfg.request_timeout_seconds) as client:
            http = SchwabHttp(
                schwab_cfg, client, TokenStore(schwab_cfg.token_store_path), clock=clock
            )
            live = _live_account(cfg, http, repo, clock=clock, fees=None, command="reconcile")
            report = reconcile_account(
                broker=live.broker,
                repo=repo,
                attribution=AttributionLedger(conn),
                reconcile_order=live.reconciler,
                poll_policy=PollPolicy(timeout_seconds=cfg.execution.poll_timeout_seconds),
                retryable=TRANSIENT_READ_ERRORS,
            )
        for line in summary_lines(report):
            typer.echo(line)
        if not report.is_clean:
            raise typer.Exit(2)
    finally:
        lease.release()


def _apply_overrides(repo: OrderRepository, not_placed: list[str], adopt: list[str]) -> None:
    """Operator overrides for rows reconciliation cannot settle on its own (e.g. beyond the
    listing look-back, or ambiguous with a manual trade). Verify against Schwab first."""
    import sqlite3

    for cid in not_placed:
        if not repo.force_not_placed(cid):
            typer.echo(f"reconcile error: {cid} is not an unsettled order without an id", err=True)
            raise typer.Exit(2)
        typer.echo(f"override: {cid} marked not placed")
    for spec in adopt:
        cid, sep, broker_order_id = spec.partition("=")
        if not sep or not cid or not broker_order_id.strip():
            typer.echo(
                f"reconcile error: --adopt expects CID=BROKER_ORDER_ID, got {spec!r}", err=True
            )
            raise typer.Exit(2)
        try:
            adopted = repo.force_adopt(cid, broker_order_id.strip())
        except sqlite3.IntegrityError as exc:
            typer.echo(f"reconcile error: {broker_order_id} is bound to another order", err=True)
            raise typer.Exit(2) from exc
        if not adopted:
            typer.echo(f"reconcile error: {cid} is not an unsettled order without an id", err=True)
            raise typer.Exit(2)
        typer.echo(f"override: {cid} adopted as {broker_order_id.strip()}")


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
