"""Trader command-line interface (Typer).

``status`` loads and reports the validated configuration plus Schwab auth/token
age (and backs the Docker HEALTHCHECK via ``--healthcheck``). ``reauth`` runs the
interactive Schwab OAuth flow (M1). The remaining commands are skeletons fleshed
out by later milestones: ``backtest`` (M2), ``run`` (M3/M4), ``reconcile`` (M4),
``kill`` (M5).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from trader.clock import RealClock
from trader.config import DEFAULT_CONFIG_PATH, AppConfig, load_config
from trader.schwab.config import SchwabClientConfig, schwab_config_from_env
from trader.schwab.errors import SchwabAuthError, SchwabError

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
        # M0.9: config loads => the binary is healthy. The real heartbeat-based
        # liveness check is wired in M4.
        raise typer.Exit(0)
    typer.echo(f"mode: {cfg.mode.value}")
    typer.echo(f"strategies: {', '.join(s.id for s in cfg.strategies)}")
    typer.echo(_auth_status_line(cfg))


@app.command()
def run(config: ConfigOpt = DEFAULT_CONFIG_PATH) -> None:
    """Run the trading daemon (implemented in M3/M4)."""
    _load(config)
    typer.echo("run: not implemented yet (daemon arrives in M3/M4)")


@app.command()
def backtest(config: ConfigOpt = DEFAULT_CONFIG_PATH) -> None:
    """Run a historical backtest (implemented in M2)."""
    _load(config)
    typer.echo("backtest: not implemented yet (engine arrives in M2)")


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
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
) -> None:
    """Engage/release the kill switch (implemented in M5)."""
    state = "on" if on else "off"
    typer.echo(f"kill: not implemented yet (kill switch arrives in M5); requested {state}")


@app.command()
def reconcile(config: ConfigOpt = DEFAULT_CONFIG_PATH) -> None:
    """Reconcile local state with the broker (implemented in M4)."""
    _load(config)
    typer.echo("reconcile: not implemented yet (reconciliation arrives in M4)")


data_app = typer.Typer(help="Historical data cache management.", no_args_is_help=True)
app.add_typer(data_app, name="data")


def _parse_day(value: str, name: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        typer.echo(f"data fetch error: {name} must be YYYY-MM-DD, got {value!r}", err=True)
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
