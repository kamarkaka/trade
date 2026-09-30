# trade

Automated equity trading program — scheduled, multi-strategy, backtestable, and traded via the Charles Schwab API.

> **Status:** Milestones M0–M7 are implemented: first-party Schwab client, event-driven backtester, multi-strategy scheduler, paper trading, Docker deployment, and a read-only monitoring web UI. **Live trading is locked** — `trader run` refuses `mode: live` until the guarded first-real-order step (M5.7) is completed by a human. See [`plan/milestones.md`](plan/milestones.md).

## What this is

A single-user service that runs continuously and triggers a configurable number of times per trading day (each fire offset by a bounded random drift). On each trigger it fetches quotes for a configurable set of tickers, runs one or more pluggable strategies to produce buy/sell/hold decisions, passes them through a non-bypassable risk gate, and (in live mode) executes through Charles Schwab. The **same** strategy/decision code runs unchanged in an event-driven **backtest** over historical data.

## Documents

- [`plan/design.md`](plan/design.md) — full system design: architecture, core interfaces, scheduler + jitter, first-party Schwab integration, backtesting, risk controls, deployment, and the read-only web UI.
- [`plan/milestones.md`](plan/milestones.md) — implementation plan: 8 milestones, 80 baby-step sub-steps with files, libraries, and validation each (starts with a quick-reference table).
- [`docs/strategy_guide.md`](docs/strategy_guide.md) — how to write and register a new strategy.
- [`docs/runbooks/`](docs/runbooks/) — operational runbooks (paper soak, weekly Schwab re-auth).
- [`docs/security/m1-credential-review.md`](docs/security/m1-credential-review.md) — credential-handling security review.

## Key properties

- **Live/backtest parity** — the same decision code runs both live and in backtest (data, clock, and broker are injected).
- **Default-safe** — paper mode by default; live requires `mode: live` plus a second confirmation and a conservative preflight, and stays locked until M5.7.
- **First-party Schwab client** — no third-party broker SDK handles credentials or orders.
- **Multi-strategy** — multiple strategies, each on its own schedule, dispatched by the orchestrator.
- **Deployment** — Docker image via docker compose, with an optional read-only, password-gated monitoring web UI.

## Usage

Configuration is a YAML file ([`config/default.yaml`](config/default.yaml) is a complete, paper-mode example); every command takes `--config/-c`. Schwab credentials come only from the environment (`SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`), never from the config file.

```bash
trader status                                   # mode, strategies, Schwab token age
trader reauth                                   # interactive Schwab OAuth (repeat weekly)
trader data fetch --symbols AAPL,MSFT --start 2024-01-01 --end 2024-12-31   # cache daily bars
trader backtest --start 2024-01-01 --end 2024-12-31                         # offline report
trader research sweep --strategy zscore_revert --grid lookback=10,20 --data /data/   # offline
trader run                                      # paper daemon: live quotes, simulated fills
trader run --once                               # fire each slot once and exit
trader kill --on --reason "why"                 # halt all new orders (persisted); --off releases
```

`backtest` and `research` are fully offline and read only the local data cache. `run` in paper mode uses live Schwab quotes with simulated fills — no real orders.

### Deployment

The trader, the read-only web UI, and a Caddy TLS proxy run under docker compose ([`deploy/`](deploy/)):

```bash
cp deploy/secrets/.env.example deploy/secrets/.env   # fill in credentials + alert channels
cd deploy && docker compose up -d --build
```

Follow [`docs/runbooks/paper-soak.md`](docs/runbooks/paper-soak.md) for the multi-day paper rehearsal and [`docs/runbooks/weekly-reauth.md`](docs/runbooks/weekly-reauth.md) for the mandatory weekly Schwab re-authentication.

## Development

Requires **Python 3.11+**.

### Setup

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"              # editable package + dev toolchain
pre-commit install                   # optional: run the gate automatically on each git commit
```

### Build, lint & test

Run the same gate CI enforces (lint, format, strict type-check, file hygiene) plus the tests:

```bash
pre-commit run --all-files           # ruff (lint + format), mypy --strict (src), file hygiene
pytest -q --cov                      # unit tests
```

Or run the tools individually:

```bash
ruff check .            # lint
ruff format --check .   # formatting
mypy src                # strict type-check
pytest -q               # tests
```

> Tip: `pre-commit` only inspects files git tracks. If you just created files and haven't staged them, run `git add -A` first — otherwise the hooks report "no files to check".

### Continuous integration

Every push and pull request runs the pre-commit gate and the test suite via GitHub Actions ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)). Action and dependency updates are proposed automatically by Dependabot ([`.github/dependabot.yml`](.github/dependabot.yml)).

## License

See [LICENSE](LICENSE).
