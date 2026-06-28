# trade

Automated equity trading program — scheduled, multi-strategy, backtestable, and traded via the Charles Schwab API.

> **Status:** Design & planning stage. This repository currently contains the design and implementation plan only — **no application code yet.**

## What this is

A single-user service that runs continuously and triggers a configurable number of times per trading day (each fire offset by a bounded random drift). On each trigger it fetches quotes for a configurable set of tickers, runs one or more pluggable strategies to produce buy/sell/hold decisions, passes them through a non-bypassable risk gate, and (in live mode) executes through Charles Schwab. The **same** strategy/decision code runs unchanged in an event-driven **backtest** over historical data.

## Documents

- [`plan/design.md`](plan/design.md) — full system design: architecture, core interfaces, scheduler + jitter, first-party Schwab integration, backtesting, risk controls, deployment, and the read-only web UI.
- [`plan/milestones.md`](plan/milestones.md) — implementation plan: 8 milestones, 80 baby-step sub-steps with files, libraries, and validation each (starts with a quick-reference table).

## Key properties

- **Live/backtest parity** — the same decision code runs both live and in backtest (data, clock, and broker are injected).
- **Default-safe** — paper/dry-run by default; no real orders until the guarded live milestone (M5).
- **First-party Schwab client** — no third-party broker SDK handles credentials or orders.
- **Multi-strategy** — multiple strategies, each on its own schedule, dispatched by the orchestrator.
- **Deployment** — Docker image via docker compose, with an optional read-only, password-gated monitoring web UI.

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
