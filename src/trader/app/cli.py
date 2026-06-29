"""Trader command-line interface (Typer).

``status`` loads and reports the validated configuration plus Schwab auth/token
age (and backs the Docker HEALTHCHECK via ``--healthcheck``). ``reauth`` runs the
interactive Schwab OAuth flow (M1). The remaining commands are skeletons fleshed
out by later milestones: ``backtest`` (M2), ``run`` (M3/M4), ``reconcile`` (M4),
``kill`` (M5).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from trader.config import DEFAULT_CONFIG_PATH, AppConfig, load_config
from trader.schwab.config import SchwabClientConfig, schwab_config_from_env
from trader.schwab.errors import SchwabAuthError, SchwabError

app = typer.Typer(
    help="Automated equity trader.",
    no_args_is_help=True,
    add_completion=False,
)

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to the YAML config file.")]


class _RealClock:
    """Minimal wall-clock for CLI use (the full RealClock arrives in M2.1)."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def is_market_open(self, at: datetime | None = None) -> bool:
        return True


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
    remaining = schwab_cfg.refresh_token_max_age_days - tok.refresh_age_days(_RealClock())
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
            auth = Authenticator(schwab_cfg, client, store, clock=_RealClock())
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
