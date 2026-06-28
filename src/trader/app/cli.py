"""Trader command-line interface (Typer).

``status`` loads and reports the validated configuration (and backs the Docker
HEALTHCHECK via ``--healthcheck``). The other commands are skeletons that load
config and are fleshed out by later milestones: ``backtest`` (M2), ``run`` (M3/M4),
``reconcile`` (M4), ``reauth`` (M1), ``kill`` (M5).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from trader.config import DEFAULT_CONFIG_PATH, AppConfig, load_config

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
    typer.echo("auth: not authenticated (Schwab client arrives in M1)")


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
    """Re-authenticate with Schwab (implemented in M1)."""
    typer.echo("reauth: not implemented yet (OAuth arrives in M1)")


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
