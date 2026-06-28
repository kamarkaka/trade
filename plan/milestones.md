# Implementation Plan — Milestones & Execution Steps

- **Status:** Draft for review (no execution yet)
- **Companion to:** [`design.md`](./design.md) — this document operationalizes design §17 into fine-grained, individually-validatable steps.
- **Milestones:** 8 (M0–M7), **80 sub-steps total.**

## Quick reference — milestones & sub-steps

| Step | Title | Deliverable | Depends |
|------|-------|-------------|---------|
| **M0 — Skeleton & foundations (9 steps)** | | | |
| M0.1 | Project scaffolding, tooling & CI | A reproducible, linted, type-checked, CI-backed empty project that builds and runs an (empty) test suite | — |
| M0.2 | Core domain types & enums | A typed, immutable domain vocabulary (Quote/Bar/Order/Fill/Decision/…) with money-as-Decimal and tz-aware… | M0.1 |
| M0.3 | Core interfaces / Protocols | The complete set of injected interfaces (Clock/MarketDataProvider/Broker/Strategy/RiskManager/Scheduler) that… | M0.2 |
| M0.4 | Configuration models (pydantic) | A fully-typed, validated configuration schema covering schedule, strategies, risk, execution, backtest,… | M0.2 |
| M0.5 | Layered config loader | A layered config loader producing one validated AppConfig from defaults+file+env+CLI, never touching secrets | M0.4 |
| M0.6 | Structured logging + secret scrubbing | A structured JSON logger with guaranteed secret scrubbing and a correlation id, ready for the audit trail | M0.1 |
| M0.7 | SQLite state layer + migration runner | A WAL SQLite connection helper + transactional migration runner with the initial durable schema | M0.1 |
| M0.8 | Shared test doubles (FakeClock, FakeBroker, FakeMarketData) | Deterministic, Protocol-conforming FakeClock/FakeBroker/FakeMarketData used by every later test | M0.3 |
| M0.9 | CLI skeleton | A working CLI skeleton (all commands present as stubs) that loads validated config and runs `status` | M0.5, M0.6, M0.7 |
| **M1 — First-party Schwab client (read-only) (10 steps)** | | | |
| M1.1 | Schwab client config, constants, and typed error taxonomy | Importable trader.schwab package with config model, endpoint constants, and a typed error taxonomy that… | — |
| M1.2 | Token model + SQLite token store with 7-day age tracking | A persistable TokenSet + SQLite TokenStore that tracks refresh-token age and answers expiry/alert questions… | M1.1 |
| M1.3 | Token-bucket rate limiter (injected clock, no wall-clock) | A deterministic token-bucket rate limiter unit usable by the HTTP transport, fully covered by clock-injected… | M1.1 |
| M1.4 | OAuth token-exchange + refresh primitives (httpx,… | OAuth code-exchange and refresh functions returning TokenSet, contract-tested against recorded Schwab… | M1.1, M1.2 |
| M1.5 | Local HTTPS loopback callback server for the authorization-code… | An HTTPS loopback callback capture server that yields the OAuth code to the exchange step, ready for the… | M1.1 |
| M1.6 `⚙split` | Resilient HTTP transport: rate limit + tenacity retry +… | A resilient, auth-aware HTTP transport with rate limiting, retry/backoff, transparent 401-refresh-retry, and… | M1.2, M1.3, M1.4 |
| M1.7 | OAuth orchestration: authenticate + auto-refresh + token-age… | An Authenticator that performs interactive first-auth, persists tokens, and fires the 7-day re-auth alert… | M1.2, M1.4, M1.5, M1.6 |
| M1.8 | Typed endpoint models + read endpoints: quotes, pricehistory,… | A read-only SchwabClient exposing quotes, daily pricehistory, and hashed account-number resolution, with… | M1.6 |
| M1.9 | SchwabMarketData provider adapter (implements core… | A SchwabMarketData adapter conforming to MarketDataProvider plus working `reauth`/`status` CLI hooks,… | M1.7, M1.8 |
| M1.10 | Credential-handling security review + log-scrubbing assertions… | A completed credential-handling security review, cross-cutting no-secret-leakage and file-permission tests,… | M1.9 |
| **M2 — Backtest engine (11 steps)** | | | |
| M2.1 | Clock implementations (Real + Virtual) | RealClock + VirtualClock conforming to Clock, with monotonic forward-only virtual time | M0.3 |
| M2.2 | Historical data cache (Parquet + catalog) | A content-hashed Parquet bar cache with range tracking for fast, reproducible offline backtests | M0.2 |
| M2.3 | HistoricalDataProvider (asof-bound, no-lookahead) | An asof-bound HistoricalDataProvider that structurally prevents lookahead, reusable unchanged by the engine | M2.1, M2.2, M0.3 |
| M2.4 | Data ingestion CLI (Schwab pricehistory → cache) | An offline-first daily-candle ingestion path populating the Parquet cache from Schwab | M2.2 |
| M2.5 | SimBroker core (fills, slippage, fees) | A deterministic SimBroker for market orders with realistic slippage and fees | M0.3, M0.8 |
| M2.6 | SimBroker advanced fills (limit + partial) | Limit + partial-fill modeling in SimBroker with working-order remainders and DAY expiry | M2.5 |
| M2.7 | Backtest portfolio & P&L | A portfolio/P&L tracker producing an equity curve and realized/unrealized P&L from fills | M2.5 |
| M2.8 | Event-driven backtest engine (single strategy) | A working single-strategy event-driven backtest engine producing trades + an equity curve | M2.3, M2.6, M2.7 |
| M2.9 | Run manifest + seeded determinism | A portable, content-addressed run manifest + seeded RNG making every backtest exactly reproducible | M2.2 |
| M2.10 | Backtest report (creates report.py) + golden run | A JSON backtest report module (owned here) plus a committed golden-run regression proving bit-for-bit… | M2.8, M2.9 |
| M2.11 | `trader backtest` CLI (single strategy) | A one-command single-strategy backtest producing a reproducible report + manifest | M2.10 |
| **M3 — Multi-strategy + scheduler (12 steps)** | | | |
| M3.1 | Strategy/sizing/scheduler core types and protocols | Importable, frozen, hashable core types and the Scheduler protocol that all M3 modules depend on | — |
| M3.2 | Seeded jitter module | A pure, seeded, isolated jitter module producing reproducible bounded drift per (seed,date,strategy,slot) | M3.1 |
| M3.3 | Trading-calendar wrapper (XNYS) | A deterministic XNYS calendar wrapper with the resolve_fire clamp/skip gate shared by backtest and live | M3.1 |
| M3.4 | Slot/trigger generation (merged, time-sorted, stable tie-break) | A reproducible Scheduler.triggers_for producing merged, sorted, calendar-gated, jittered TriggerSlots | M3.2, M3.3 |
| M3.5 | Fired-slot ledger (claim/do/done, exactly-once) | A crash-safe fired-slot ledger enforcing exactly-once per (date,strategy,slot) | M3.1 |
| M3.6 | StrategyRegistry + two stub strategies | A StrategyRegistry and two pure, asof-safe stub strategies wired to it | M3.1 |
| M3.7 | Strategy bindings loader (config -> StrategyBinding list) | A validated config->StrategyBinding loader feeding scheduler and orchestrator | M3.1, M3.6 |
| M3.8 | Sizing (Decision -> Order) | A sizing function turning Decisions into attributed, idempotency-ready Orders | M3.1 |
| M3.9 `⚙split` | Orchestrator run_cycle + global cycle lock + attribution | A lock-serialized, attribution-aware run_cycle shared by backtest and live, safe with FakeBroker/SimBroker… | M3.5, M3.6, M3.8 |
| M3.10 | Backtest engine extended to multi-strategy merged interleave +… | A multi-strategy backtest that interleaves merged triggers and reports per-strategy + combined attribution,… | M3.4, M3.7, M3.9 |
| M3.11 `⚙split` | Live APScheduler daemon (paper placeholder) | A paper-mode APScheduler daemon dispatching the right strategy per (strategy,slot) with… | M3.4, M3.5, M3.7, M3.9 |
| M3.12 | End-to-end M3 parity + exit-criteria integration test | A passing end-to-end test demonstrating the M3 exit criteria: dual-path dispatch parity, serialized overlaps,… | M3.10, M3.11 |
| **M4 — Paper trading + Dockerize (10 steps)** | | | |
| M4.1 | Reconciliation engine | A reconciliation engine that trues local state to broker truth and surfaces divergence | M0.7, M0.8 |
| M4.2 | Risk rules (individual checks) | A complete, individually-tested set of fail-closed risk rules | M0.2, M0.7 |
| M4.3 | Risk gate (manager + conflict policy) | The single fail-closed RiskManager with dual-scope limits and conflict netting | M4.2 |
| M4.4 | Wire the risk gate into the orchestrator | The orchestrator with the real risk gate as the enforced single chokepoint | M4.3 |
| M4.5 | Alerting channels (Telegram + email + heartbeat events) | Redundant Telegram+email alerting with a typed event taxonomy and fan-out resilience | M0.6 |
| M4.6 | Heartbeat + healthcheck | A heartbeat + healthcheck wiring that makes daemon liveness observable to Docker and alerts | M4.5, M0.9 |
| M4.7 | Paper pipeline integration (live quotes + SimBroker + risk +… | A complete paper-mode trading pipeline (live data, simulated fills) with full audit + risk + reconciliation | M4.4, M4.6, M2.5 |
| M4.8 | Dockerfile + entrypoint | A reproducible, non-root, healthchecked Docker image of the daemon | M4.7 |
| M4.9 | docker compose + durable volumes | A compose deployment with durable volumes verified to survive container recreation | M4.8 |
| M4.10 | Paper soak runbook + multi-day soak | A validated multi-day paper soak + the operational runbooks, clearing the system for guarded live trading | M4.9 |
| **M5 — Live (guarded) (8 steps)** | | | |
| M5.1 | Schwab order + account endpoints | Contract-tested Schwab order + account endpoints on the first-party client (no live calls yet) | M1 |
| M5.2 | SchwabBroker adapter (implements Broker) | A SchwabBroker conforming to Broker, swappable with SimBroker, safe-mode aware | M5.1, M0.3 |
| M5.3 | Idempotent order placement (write-ahead + reuse +… | Idempotent, crash-safe order placement proven at-most-once under fuzzed failure interleavings | M4.1, M5.2 |
| M5.4 | Kill switch | A persisted, auto-tripping kill switch enforced at cycle start and pre-submit | M4.3 |
| M5.5 | PDT rule (configurable) | A configurable PDT day-trade-count rule wired into the risk gate | M4.3 |
| M5.6 | Go-live double-confirm + safe rollout guards | A double-confirm go-live gate with a conservative preflight, CI-enforced | M5.3, M5.4 |
| M5.7 | Guarded live verification (first real orders) | Verified guarded live trading at minimal size with intent-match, clean reconciliation, and a working kill… | M5.6 |
| M5.8 | Deploy live via compose | The validated trader running live on the server via docker compose with monitoring and runbooks | M5.7 |
| **M6 — Refine calculation (9 steps)** | | | |
| M6.1 | Strategy interface conformance test + golden contract for any… | A reusable strategy conformance test + helper module that any new strategy must pass; CI now fails if a… | — |
| M6.2 | Shared indicator helpers (asof-safe, no-lookahead) in… | A shared, deterministic, no-lookahead indicator library that both production strategies and the offline… | — |
| M6.3 | Strategy development guide + copyable template class | A copy-paste strategy template that already passes the conformance suite, plus a developer guide that… | M6.1, M6.2 |
| M6.4 | Real strategy implementation: zscore_revert (mean-reversion)… | A real, validated mean-reversion strategy plugged in via the registry and config binding with zero changes to… | M6.1, M6.2 |
| M6.5 | Backtest metrics module (per-run analytics over the… | A tested, reusable analytics layer that turns raw backtest records into comparable per-strategy and combined… | — |
| M6.6 | Per-strategy + combined backtest report generation (HTML/JSON +… | A reproducible per-strategy + combined backtest report (HTML + deterministic JSON) including the run manifest… | M6.5 |
| M6.7 | Wire reporting into the backtest CLI + multi-strategy backtest… | An end-to-end offline `trader backtest` that runs two real-ish strategies on cached data and emits the… | M6.4, M6.6 |
| M6.8 | Golden-run reproducibility test for the multi-strategy report | A committed golden-run test proving the multi-strategy backtest (per-strategy + combined) is reproducible… | M6.7 |
| M6.9 | Offline vectorized parameter-research harness (NEVER on the… | An isolated, offline-only vectorized parameter-sweep tool that accelerates research while being structurally… | M6.2, M6.5 |
| **M7 — Web UI (read-only monitoring) (11 steps)** | | | |
| M7.1 | Read-only state DB access layer (mode=ro / query_only) | A read-only DB access module proven (by test) to reject all writes while serving parameterized reads under WAL | — |
| M7.2 | FastAPI app skeleton + /healthz + settings + crash isolation | A runnable FastAPI app with health check, settings, read-only DB wired in, and request-level crash isolation | M7.1 |
| M7.3 | Auth core: argon2id verify, signed stateless session cookie,… | Tested auth primitives (argon2id verify, signed stateless session, CSRF, lockout) with an injected clock | M7.2 |
| M7.4 | Auth middleware + login/logout routes + login page | Working single-admin login/logout with CSRF, lockout, secure stateless session cookies, and a guard… | M7.3 |
| M7.5 | Read-only repository/query layer + secret-scrubbing serializers | A read-only repository that supplies all monitoring data as safe dicts with secrets structurally excluded | M7.1 |
| M7.6 | Base templates + HTMX auto-refresh layout + static assets | Shared template layout with local HTMX, auto-refresh fragment pattern, and styling — the chrome every view… | M7.4 |
| M7.7 | System status + Schedule + Token/re-auth views | Authenticated System, Schedule, and Re-auth/token monitoring pages with HTMX auto-refresh | M7.5, M7.6 |
| M7.8 | Per-strategy + Account + P&L views | Authenticated per-strategy and account/P&L monitoring pages with auto-refresh | M7.7 |
| M7.9 | Orders/Fills + Alerts + Config view | Authenticated Orders/Fills, Alerts, and read-only Config monitoring pages completing the §19.3 surface | M7.8 |
| M7.10 | Safety invariant tests: no-write, no-broker-import,… | Passing guard tests that codify the M7 read-only / no-broker / no-secret exit criteria | M7.2, M7.9 |
| M7.11 `⚙split` | trader-web entrypoint + compose web + Caddy reverse proxy (TLS,… | Deployable read-only web UI: trader-web entrypoint, isolated compose web service, and Caddy TLS proxy… | M7.10 |

> **Legend:** `⚙split` = the step bundles several concerns; see its **Plan-review note** for the recommended sub-split. Full detail (files, libraries, validation) for each step is in the per-milestone sections below.

## How to read this

Each **milestone** is a shippable feature; each **sub-step** is a baby step with a single focused concern, a small set of files, and its own validation. Build in order; within a milestone, follow `Depends on`.

**Sub-step template:** *Goal* · *Build (files)* · *Libraries* · *Details* · *Validation — unit tests* · *Validation — manual* · *Deliverable* · *Depends on*.

**Definition of Done (every sub-step):** code written → unit tests green → `ruff` + `mypy` clean → manual check (if any) performed → committed on its own branch/commit. CI must be green before the next sub-step.

## Conventions

- **Testing.** `pytest`. Inject `Clock`/`sleep`/RNG and HTTP transport — **no wall-clock, no global RNG, no live network in CI**. HTTP is mocked with `respx`; the broker/data/clock are the M0.8 fakes. Markers: `unit` (default), `integration`, `network` (opt-in, excluded from CI). Concurrency invariants are tested **deterministically** (instrumented locks / sequence assertions), not via flaky thread races.
- **Safety gate.** **No real order can be placed before M5.** M1 is read-only; M2 is offline backtest; M3–M4 use `SimBroker`/`FakeBroker` only and the daemon **refuses `mode=live`** (CI-enforced). Real money first appears in M5.7, manually, at the smallest size.
- **Determinism.** Seed every RNG; compute in UTC; canonicalize hashes — backtests reproduce bit-for-bit (M2.9/M2.10).
- **Secrets.** Loaded only by the secrets/auth components; never in the repo, image, logs, or UI (§13). Scrubbing is asserted by tests.
- **Git.** `git init` at M0.1; one branch/commit per sub-step; PR-sized changes.

## Provenance & review

M1/M3/M6/M7 were detailed by parallel agents against `design.md`; M0/M2/M4/M5 were authored directly; all eight were then put through an adversarial review (granularity / validation-completeness / build-order). Review fixes are folded in inline and marked **⚙** (added tests) or **⚙ Plan-review note** (splits, dependencies, clarifications). See the [Plan-review summary](#plan-review-summary) at the end.

---

## M0 — Skeleton & foundations

> **Intent.** Stand up the repository, tooling, and the load-bearing abstractions the whole system is built on: core domain types and Protocol interfaces (so live/backtest parity is structural from day one), the layered+validated pydantic config, structured logging with secret scrubbing, the SQLite state layer + migration runner, the shared test doubles (FakeClock/FakeBroker/FakeMarketData), and a CLI skeleton. Nothing here talks to a network or places an order.
>
> **Prerequisites:** None (greenfield).
> **New libraries:** `python>=3.11`, `uv (or poetry)`, `ruff`, `mypy`, `pytest`, `pytest-cov`, `pre-commit`, `pydantic`, `pydantic-settings`, `pyyaml`, `structlog`, `typer`
>
> **Exit criteria.** `config` loads + validates the §11 example YAML; all core types and Protocols import and typecheck (mypy clean); the migration runner builds the initial SQLite schema (WAL on); `trader status` runs and prints mode + 'not authenticated'; ruff + mypy + pytest + pre-commit all green in CI. No network, no orders.

*9 sub-steps.*

#### M0.1 — Project scaffolding, tooling & CI

**Goal.** Create the repo skeleton, dependency/build config, linters, test runner, pre-commit, and CI so every later sub-step has a green baseline to extend.

**Build (files):**

- `pyproject.toml` *(create)* — Project metadata, deps, and tool config: [project] (name=trader, requires-python='>=3.11'), dependency groups; [tool.ruff], [tool.mypy] (strict on src/trader), [tool.pytest.ini_options] (testpaths, markers: unit/integration/network), [tool.coverage]. Console script trader=trader.app.cli:app.
- `uv.lock` *(create)* — Resolved + hashed lockfile (uv lock) for reproducible builds (§13 pin+hash-lock). (Or poetry.lock.)
- `.gitignore` *(create)* — Ignore state/, data/, secrets/, .env, *.sqlite*, __pycache__, .venv, .pytest_cache, .mypy_cache.
- `.pre-commit-config.yaml` *(create)* — Hooks: ruff (lint+format), mypy, end-of-file/trailing-whitespace, check-added-large-files.
- `src/trader/__init__.py` *(create)* — Top-level package marker + __version__.
- `tests/conftest.py` *(create)* — Shared pytest fixtures root; registers markers; a trivial smoke test target.
- `tests/unit/test_smoke.py` *(create)* — A trivial passing test so the suite is green from commit 1.
- `.github/workflows/ci.yml` *(create)* — CI: setup uv/python 3.11, install, ruff check, mypy, pytest -q --cov; fail on any.

**Libraries:** `uv`, `ruff`, `mypy`, `pytest`, `pytest-cov`, `pre-commit`

**Details.** Use `uv` for env+lockfile (fast, hash-locked) — poetry is an acceptable substitute. Enable mypy strict on src/trader so Protocol conformance is enforced early. Register pytest markers (unit default; integration and network opt-in) so CI can exclude network tests. `git init` and commit this as the baseline; thereafter one branch/commit per sub-step.

**Validation — unit tests:**

- tests/unit/test_smoke.py::test_smoke asserts True (proves the toolchain runs)
- a meta test tests/unit/test_packaging.py::test_console_script_importable imports trader.app.cli:app

**Validation — manual:**

- Run: `uv sync && uv run ruff check . && uv run mypy src && uv run pytest -q` — expected: all green
- Run: `uv run pre-commit run --all-files` — expected: all hooks pass
- Push and confirm the CI workflow is green

**Deliverable.** A reproducible, linted, type-checked, CI-backed empty project that builds and runs an (empty) test suite.

**Depends on:** —

#### M0.2 — Core domain types & enums

**Goal.** Define the immutable value types and enums every layer exchanges, with Decimal money and tz-aware UTC time, so the domain language is fixed before any behavior is built.

**Build (files):**

- `src/trader/core/__init__.py` *(create)* — Package marker; re-export the public types/enums.
- `src/trader/core/types.py` *(create)* — Frozen dataclasses (or pydantic models): Quote, Bar, Position, Account, Order (incl. client_order_id, strategy_id), Fill, Decision, MarketSnapshot, RiskVerdict, TriggerSlot, StrategyBinding (mirrors §5). All prices Decimal; all timestamps tz-aware UTC.
- `src/trader/core/enums.py` *(create)* — Enums: Side(BUY/SELL), Action(BUY/SELL/HOLD), OrderType(MARKET/LIMIT), TimeInForce(DAY/GTC/FOK), OrderStatus(WORKING/FILLED/PARTIAL_FILL/CANCELED/REJECTED/EXPIRED), Mode(PAPER/LIVE/BACKTEST), ConflictPolicy(NET/INDEPENDENT/PRIORITY).
- `tests/unit/core/test_types.py` *(create)* — Construction, immutability, Decimal/tz-aware invariants.

**Libraries:** `pydantic (optional)`

**Details.** Match §5 exactly so the interfaces in M0.3 reference real types. Enforce tz-aware UTC (reject naive datetimes in __post_init__) and Decimal for all monetary fields (reject float). Frozen/immutable to prevent accidental mutation across the cycle. Order carries client_order_id and strategy_id (idempotency + attribution).

**Validation — unit tests:**

- tests/unit/core/test_types.py::test_money_is_decimal asserts constructing a Quote with float last raises (Decimal required)
- tests/unit/core/test_types.py::test_timestamps_tz_aware asserts a naive datetime is rejected
- tests/unit/core/test_types.py::test_types_are_frozen asserts attribute assignment raises FrozenInstanceError
- tests/unit/core/test_types.py::test_order_has_strategy_and_client_id asserts both fields exist and are required

**Validation — manual:**

- Run: `uv run pytest tests/unit/core/test_types.py -q && uv run mypy src/trader/core` — expected: green

**Deliverable.** A typed, immutable domain vocabulary (Quote/Bar/Order/Fill/Decision/…) with money-as-Decimal and tz-aware time enforced.

**Depends on:** M0.1

#### M0.3 — Core interfaces / Protocols

**Goal.** Define the narrow Protocols that enable live/backtest parity (everything is injected behind these), so strategies and the orchestrator depend only on abstractions.

**Build (files):**

- `src/trader/core/protocols.py` *(create)* — @runtime_checkable Protocols from §5: Clock, MarketDataProvider, Broker, Strategy, RiskManager, Scheduler. Signatures only (no logic). MarketDataProvider methods take asof (no-lookahead boundary).
- `tests/unit/core/test_protocols.py` *(create)* — Conformance tests using trivial inline fakes implementing each Protocol.

**Libraries:** —

**Details.** These are the parity seams (§5, Appendix B): the Strategy/orchestrator never import Schwab, sockets, or wall-clock — only these Protocols. Use typing.Protocol + runtime_checkable so isinstance checks and mypy both validate conformance. MarketDataProvider.get_quote/get_bars take an asof param so no-lookahead is structural.

**Validation — unit tests:**

- tests/unit/core/test_protocols.py::test_fakes_satisfy_protocols asserts isinstance(FakeClock(), Clock) etc. for every Protocol
- a mypy-level check: an intentionally-wrong fake (missing a method) fails mypy (documented as an xfail-typecheck note)

**Validation — manual:**

- Run: `uv run pytest tests/unit/core/test_protocols.py -q && uv run mypy src/trader/core` — expected: green

**Deliverable.** The complete set of injected interfaces (Clock/MarketDataProvider/Broker/Strategy/RiskManager/Scheduler) that the rest of the system programs against.

**Depends on:** M0.2

#### M0.4 — Configuration models (pydantic)

**Goal.** Model the entire §11 config as validated pydantic types (system params separated from strategy params), so config errors fail fast at load.

**Build (files):**

- `src/trader/config/__init__.py` *(create)* — Package marker.
- `src/trader/config/models.py` *(create)* — AppConfig with sub-models: AccountConfig, ScheduleConfig (global: timezone, market_calendar, base_seed, catch_up, misfire_grace_seconds), SlotSpec (id,time,drift_max_minutes,drift_direction,distribution,on_overshoot), StrategyBindingConfig (id,name,enabled,params,universe,slots,risk_overrides), RiskConfig, ExecutionConfig, BacktestConfig, AlertingConfig, ObservabilityConfig. mode: Mode.
- `tests/unit/config/test_models.py` *(create)* — Validation rules + parse the §11 example.

**Libraries:** `pydantic`

**Details.** Mirror §11 field-for-field. Validators: drift_max_minutes ≤ a hard ceiling (e.g. 60) and ≥0; daily_loss_limit_pct>0; rate_limit_per_min≤120; timezone is a valid IANA zone; conflict_policy/mode are enums; strategy ids unique; slot times match HH:MM; universe non-empty per enabled strategy; risk_overrides keys ⊆ RiskConfig keys. Strategy params stay an open dict (validated by each strategy later).

**Validation — unit tests:**

- tests/unit/config/test_models.py::test_parses_example_config loads the §11 YAML into AppConfig without error
- tests/unit/config/test_models.py::test_drift_ceiling rejects drift_max_minutes=120
- tests/unit/config/test_models.py::test_unique_strategy_ids rejects two bindings with the same id
- tests/unit/config/test_models.py::test_risk_override_keys_subset rejects an unknown risk_overrides key
- tests/unit/config/test_models.py::test_mode_and_conflict_enums reject invalid enum strings

**Validation — manual:**

- Run: `uv run pytest tests/unit/config/test_models.py -q` — expected: green; the §11 example parses

**Deliverable.** A fully-typed, validated configuration schema covering schedule, strategies, risk, execution, backtest, alerting, and observability.

**Depends on:** M0.2

#### M0.5 — Layered config loader

**Goal.** Load and merge config with precedence defaults < file(YAML) < env < CLI, validated into AppConfig, so the SAME object drives live and backtest.

**Build (files):**

- `src/trader/config/loader.py` *(create)* — load_config(path, cli_overrides, env_prefix='TRADER_') -> AppConfig: read defaults, deep-merge YAML file, overlay env (pydantic-settings, nested via TRADER__SECTION__KEY), overlay CLI dict; validate; return. Records the resolved source of each value for debugging.
- `config/default.yaml` *(create)* — Annotated default config (safe defaults: mode=paper).
- `tests/unit/config/test_loader.py` *(create)* — Precedence + override tests.

**Libraries:** `pydantic-settings`, `pyyaml`

**Details.** Deep-merge semantics: later layers override earlier at the leaf level (lists replace, dicts merge). pydantic-settings handles env layering; CLI overrides come from the CLI (M0.9) as a dict. Secrets are NOT in the YAML — only references (§13); the loader never reads credentials. The resolved AppConfig is the single object passed everywhere.

**Validation — unit tests:**

- tests/unit/config/test_loader.py::test_env_overrides_file sets TRADER__RISK__MAX_TRADES_PER_DAY and asserts it wins over the file
- tests/unit/config/test_loader.py::test_cli_overrides_env asserts a CLI override beats env
- tests/unit/config/test_loader.py::test_defaults_when_absent asserts defaults fill unspecified keys
- tests/unit/config/test_loader.py::test_invalid_merged_config_raises asserts a merged-but-invalid result raises ValidationError

**Validation — manual:**

- Run: `uv run pytest tests/unit/config/test_loader.py -q` — expected: green
- Run: `trader status --config config/default.yaml` (after M0.9) — expected: prints mode=paper

**Deliverable.** A layered config loader producing one validated AppConfig from defaults+file+env+CLI, never touching secrets.

**Depends on:** M0.4

#### M0.6 — Structured logging + secret scrubbing

**Goal.** Provide JSON structured logging with a scrubbing processor that redacts tokens/secrets, plus a correlation/cycle id, used by every component.

**Build (files):**

- `src/trader/observability/__init__.py` *(create)* — Package marker.
- `src/trader/observability/logging.py` *(create)* — configure_logging(level, json=True): structlog pipeline → JSON lines; a scrub processor redacting keys/substrings (access_token, refresh_token, app_secret, Authorization, code, password, raw account number); bind_cycle_id() contextvar helper.
- `tests/unit/observability/test_logging.py` *(create)* — JSON shape + scrubbing assertions.

**Libraries:** `structlog`

**Details.** The scrub processor is reused by the Schwab client (M1) and web (M7). It redacts both known keys and a registry of secret literals if provided. Logs are append-only JSON lines (rotated by the Docker json-file driver in prod, §16). Correlation id ties a cycle's inputs→decision→order→fill (§12 audit).

**Validation — unit tests:**

- tests/unit/observability/test_logging.py::test_emits_json asserts a log line parses as JSON with expected keys
- tests/unit/observability/test_logging.py::test_scrubs_token asserts a logged dict containing access_token='SEKRIT' renders '***' and never the literal
- tests/unit/observability/test_logging.py::test_scrubs_authorization_header asserts Basic/Bearer header values are redacted

**Validation — manual:**

- Run: `uv run pytest tests/unit/observability/test_logging.py -q` — expected: green

**Deliverable.** A structured JSON logger with guaranteed secret scrubbing and a correlation id, ready for the audit trail.

**Depends on:** M0.1

#### M0.7 — SQLite state layer + migration runner

**Goal.** Create the durable state foundation: a WAL-mode SQLite connection helper and an ordered migration runner with the initial schema, so later milestones add tables via migrations.

**Build (files):**

- `src/trader/state/__init__.py` *(create)* — Package marker.
- `src/trader/state/db.py` *(create)* — connect(path) -> sqlite3.Connection with PRAGMA journal_mode=WAL, busy_timeout=5000, foreign_keys=ON; row_factory. A read_only_connect(path) opener (mode=ro) for later web use is documented but the web impl lives in M7.
- `src/trader/state/migrations/001_initial.sql` *(create)* — Initial schema: schema_migrations(version,applied_at); orders; fills; positions; equity_snapshots; audit_log; daily_counters; kill_switch(flag,reason,updated_at). (Per-milestone tables — fired_slot ledger, attributed_position, tokens — are added by their own migrations later.)
- `src/trader/state/migrate.py` *(create)* — run_migrations(conn, dir): apply unappied .sql files in version order inside a transaction; record in schema_migrations; idempotent re-run is a no-op.
- `tests/unit/state/test_migrate.py` *(create)* — Migration apply/idempotency + PRAGMA assertions.

**Libraries:** —

**Details.** WAL mode (§3/§12) enables the future read-only web reader (M7) to read concurrently with the daemon writer. busy_timeout handles brief write contention. Migrations are forward-only, applied transactionally, tracked in schema_migrations so re-running is safe. Keep the initial schema minimal; each milestone owns its tables via a numbered migration (ledger in M3, attributed_position in M3, tokens in M1).

**Validation — unit tests:**

- tests/unit/state/test_migrate.py::test_applies_initial_schema asserts the expected tables exist after run_migrations on a tmp DB
- tests/unit/state/test_migrate.py::test_rerun_is_noop asserts a second run applies 0 migrations
- tests/unit/state/test_migrate.py::test_wal_and_busy_timeout asserts PRAGMA journal_mode=='wal' and busy_timeout>0

**Validation — manual:**

- Run: `uv run pytest tests/unit/state/test_migrate.py -q` — expected: green; tables created; re-run no-op

**Deliverable.** A WAL SQLite connection helper + transactional migration runner with the initial durable schema.

**Depends on:** M0.1

#### M0.8 — Shared test doubles (FakeClock, FakeBroker, FakeMarketData)

**Goal.** Provide the deterministic fakes the entire test suite reuses, so time, market data, and the broker are injectable and tests never touch the network or wall-clock.

**Build (files):**

- `tests/fakes/__init__.py` *(create)* — Package marker.
- `tests/fakes/clock.py` *(create)* — FakeClock(now) implementing Clock: set(t)/advance(delta); is_market_open via an injected predicate. Deterministic, no wall-clock.
- `tests/fakes/broker.py` *(create)* — FakeBroker implementing Broker: in-memory orders/positions/account; configurable fill/timeout/duplicate behaviors for idempotency + reconciliation tests (M3/M4/M5).
- `tests/fakes/market_data.py` *(create)* — FakeMarketDataProvider implementing MarketDataProvider: serves canned quotes/bars by asof (honors the no-lookahead contract).
- `tests/unit/test_fakes_conform.py` *(create)* — Asserts each fake satisfies its Protocol.

**Libraries:** —

**Details.** These fakes are first-class test infrastructure referenced by M1–M7 (the review flagged FakeBroker provenance — it is created here). FakeBroker can simulate: normal fill, partial fill, timeout-then-unknown (for idempotency), and position drift (for reconciliation). All fakes are deterministic and clock-injected.

**Validation — unit tests:**

- tests/unit/test_fakes_conform.py::test_fakes_satisfy_protocols asserts isinstance for Clock/Broker/MarketDataProvider
- tests/unit/test_fakes_conform.py::test_fakebroker_records_orders asserts submit_order stores the order and get_order returns its status

**Validation — manual:**

- Run: `uv run pytest tests/unit/test_fakes_conform.py -q` — expected: green

**Deliverable.** Deterministic, Protocol-conforming FakeClock/FakeBroker/FakeMarketData used by every later test.

**Depends on:** M0.3

#### M0.9 — CLI skeleton

**Goal.** Create the Typer CLI with all command stubs (run/backtest/status/reauth/kill/reconcile) and status --healthcheck, so each later milestone fleshes out one command.

**Build (files):**

- `src/trader/app/__init__.py` *(create)* — Package marker.
- `src/trader/app/cli.py` *(create)* — Typer app with subcommands (stubs that load config and print intent): run, backtest, status [--healthcheck], reauth, kill --on/--off, reconcile. Global --config option. status prints mode + 'not authenticated' + (later) token age.
- `tests/unit/app/test_cli.py` *(create)* — CliRunner invocation tests.

**Libraries:** `typer`

**Details.** The CLI loads AppConfig via M0.5 and is the single entrypoint (container default = run, §16). Stubs return clear 'not implemented yet' messages so the surface is testable now and filled in by M1 (reauth/status), M2 (backtest), M3 (run), M4 (reconcile), M5 (kill). status --healthcheck returns exit 0/non-zero for the Docker HEALTHCHECK (wired to the heartbeat in M4).

**Validation — unit tests:**

- tests/unit/app/test_cli.py::test_help_lists_commands asserts --help shows all six commands
- tests/unit/app/test_cli.py::test_status_runs asserts `status --config config/default.yaml` exits 0 and prints mode=paper
- tests/unit/app/test_cli.py::test_healthcheck_exit_code asserts status --healthcheck returns a deterministic code with no heartbeat yet

**Validation — manual:**

- Run: `uv run pytest tests/unit/app/test_cli.py -q` — expected: green
- Run: `trader --help` and `trader status` — expected: command list; mode=paper, not authenticated

**Deliverable.** A working CLI skeleton (all commands present as stubs) that loads validated config and runs `status`.

**Depends on:** M0.5, M0.6, M0.7


## M1 — First-party Schwab client (read-only)

> **Intent.** Build an in-house, fully-owned Schwab API client (no third-party broker SDK imported) parity-checked against schwab-py/Schwabdev. It provides an httpx transport with a token-bucket rate limiter (~120/min) and tenacity retry/backoff on 429/5xx, an OAuth authorization-code flow with a local HTTPS loopback callback, a SQLite token store with automatic access-token refresh, 401→refresh→retry, 7-day refresh-token age tracking that fires a re-auth alert, and READ-ONLY safe mode when the refresh token is dead. On top of the transport it adds typed models and read endpoints for quotes, pricehistory, and accountNumbers (hashed id), then a SchwabMarketData provider adapter implementing the core MarketDataProvider interface. All verified by recorded-HTTP contract tests using respx and an injected Clock, plus a documented security review of credential handling. NO order placement is built in this milestone — everything is read-only.
>
> **Prerequisites:** M0 (core types/Protocols, config, structured logging+scrubbing, SQLite state, CLI).
> **New libraries:** `httpx`, `tenacity`, `respx`, `freezegun`
>
> **Exit criteria.** The first-party client can complete the OAuth authorization-code exchange and (via recorded/mocked HTTP, with a manual smoke path documented for real credentials) fetch live quotes and daily candles for the configured universe through the SchwabMarketData adapter; the access token auto-refreshes and a 401 transparently triggers refresh-then-retry; the refresh-token 7-day age alert fires in a unit test using an injected clock; rate-limit (429) backoff and 5xx retry are exercised by contract tests; a dead refresh token flips the client into READ-ONLY safe mode and raises a typed alert instead of crash-looping; tokens are scrubbed from all logs (asserted in tests); all recorded-HTTP contract tests pass in CI with no live network calls; and a written security review of the credential-handling code path is completed. No order-placement code exists; nothing can place a real-money order in M1.

*10 sub-steps.*

#### M1.1 — Schwab client config, constants, and typed error taxonomy

**Goal.** Establish the configuration surface, base URLs/endpoint path constants (the [VERIFY] parity checklist from §8), and a typed exception hierarchy that the rest of the client raises, so every later step has a stable contract to build against.

**Build (files):**

- `src/trader/schwab/__init__.py` *(create)* — Package marker; re-exports the public client symbols added in later steps (kept minimal now).
- `src/trader/schwab/constants.py` *(create)* — Module-level constants: API_BASE='https://api.schwabapi.com', OAUTH_AUTHORIZE_URL, OAUTH_TOKEN_URL, MARKETDATA_BASE='/marketdata/v1', TRADER_BASE='/trader/v1', endpoint path templates (QUOTES_PATH, PRICEHISTORY_PATH, ACCOUNT_NUMBERS_PATH). Each marked with a '# [VERIFY]' comment referencing §8.
- `src/trader/schwab/errors.py` *(create)* — Exception hierarchy: SchwabError(base); SchwabAuthError; SchwabTokenExpiredError; SchwabRefreshTokenDeadError (drives safe mode); SchwabRateLimitError; SchwabServerError; SchwabBadResponseError; SchwabReadOnlyModeError. Each carries optional status_code and a scrubbed message.
- `src/trader/schwab/config.py` *(create)* — Pydantic model SchwabClientConfig: app_key (str), app_secret (SecretStr), redirect_uri (str, must be https), rate_limit_per_min (int, default 100, le 120), token_store_path (Path), refresh_token_max_age_days (int default 7), refresh_token_alert_lead_days (int default 2), request_timeout_seconds (float default 30), max_retries (int default 4). Validators: redirect_uri starts with 'https://'; rate_limit_per_min in [1,120].

**Libraries:** `pydantic`, `pydantic-settings`

**Details.** Use pydantic.SecretStr for app_secret so it never str-reprs in logs. Constants module is the single place the [VERIFY] Schwab facts live, isolating them per §8.7. Errors form a closed taxonomy: transport-layer code raises SchwabRateLimitError/SchwabServerError; auth code raises SchwabAuthError/SchwabTokenExpiredError/SchwabRefreshTokenDeadError; safe-mode guard raises SchwabReadOnlyModeError. Edge cases: validator must reject http:// redirect (Schwab requires HTTPS even on loopback, §8.1); rate_limit_per_min default 100 mirrors execution.rate_limit_per_min in §11 config. Do not import httpx or tenacity here — pure config/constants only.

**Validation — unit tests:**

- tests/unit/schwab/test_config.py::test_redirect_uri_must_be_https asserts SchwabClientConfig(redirect_uri='http://127.0.0.1:8182') raises ValidationError
- tests/unit/schwab/test_config.py::test_rate_limit_ceiling asserts rate_limit_per_min=200 raises ValidationError and =120 is accepted
- tests/unit/schwab/test_config.py::test_app_secret_not_in_repr asserts 'app_secret' value does not appear in repr(config) (SecretStr masks it)
- tests/unit/schwab/test_errors.py::test_error_hierarchy asserts SchwabTokenExpiredError and SchwabRefreshTokenDeadError are subclasses of SchwabAuthError which is a subclass of SchwabError

**Validation — manual:**

- Run: python -c "from trader.schwab.config import SchwabClientConfig; from trader.schwab.constants import API_BASE; print(API_BASE)" — expected: prints https://api.schwabapi.com with no import errors

**Deliverable.** Importable trader.schwab package with config model, endpoint constants, and a typed error taxonomy that compile and validate.

**Depends on:** —

#### M1.2 — Token model + SQLite token store with 7-day age tracking

**Goal.** Persist OAuth tokens (access + refresh) with issue/expiry timestamps in SQLite and expose age/expiry queries, including refresh-token-age computation, using an injected Clock so all time math is testable.

**Build (files):**

- `src/trader/auth/__init__.py` *(create)* — Package marker for the auth subpackage (part of the first-party client per §14).
- `src/trader/auth/tokens.py` *(create)* — Frozen dataclass TokenSet: access_token (str), refresh_token (str), access_token_expires_at (datetime, tz-aware UTC), refresh_token_issued_at (datetime, tz-aware UTC), scope (str|None). Helper methods that take a Clock: access_expired(clock, skew_seconds=60) -> bool; refresh_age_days(clock) -> float; refresh_expired(clock, max_age_days) -> bool; refresh_alert_due(clock, max_age_days, lead_days) -> bool.
- `src/trader/auth/token_store.py` *(create)* — TokenStore class wrapping a SQLite connection (single-row tokens table). Methods: save(TokenSet); load() -> TokenSet|None; clear(). Creates table if missing: tokens(id INTEGER PRIMARY KEY CHECK(id=1), access_token TEXT, refresh_token TEXT, access_expires_at TEXT, refresh_issued_at TEXT, scope TEXT, updated_at TEXT). Stores datetimes as ISO-8601 UTC strings.
- `tests/unit/auth/__init__.py` *(create)* — Test package marker.

**Libraries:** —

**Details.** TokenStore uses stdlib sqlite3 with WAL pragma (PRAGMA journal_mode=WAL) consistent with §3/§12; single-row table keyed id=1 so save() does INSERT OR REPLACE. refresh_token_issued_at is OUR own tracked value (§8.2 #3: we compute refresh-token age ourselves). refresh_alert_due returns True when refresh_age_days >= (max_age_days - lead_days). access_expired applies a 60s skew so we refresh slightly early. All methods take an injected Clock (from trader.core) rather than datetime.now(). Edge cases: load() returns None on empty table (drives first-time auth); never log token values; on parse failure of stored ISO timestamps raise SchwabBadResponseError. Use a temporary file or in-memory (':memory:' won't persist across connections — use tmp_path file in tests).

**Validation — unit tests:**

- tests/unit/auth/test_token_store.py::test_save_then_load_roundtrip asserts a saved TokenSet loads back equal (with a tmp_path sqlite file)
- tests/unit/auth/test_token_store.py::test_load_empty_returns_none asserts load() is None on a fresh DB
- tests/unit/auth/test_tokens.py::test_access_expired_with_skew uses a FakeClock fixed at T and a token expiring at T+30s, asserts access_expired(skew=60) is True
- tests/unit/auth/test_tokens.py::test_refresh_age_days computes age=6.5 days for a token issued 6.5 days before the FakeClock now
- tests/unit/auth/test_tokens.py::test_refresh_alert_due_at_5_days asserts refresh_alert_due(max_age_days=7, lead_days=2) is True at age 5.0 and False at age 4.9
- tests/unit/auth/test_tokens.py::test_refresh_expired_at_7_days asserts refresh_expired(max_age_days=7) is True at age 7.01

**Validation — manual:**

- Run: pytest tests/unit/auth -q — expected: all token store + token age tests pass

**Deliverable.** A persistable TokenSet + SQLite TokenStore that tracks refresh-token age and answers expiry/alert questions deterministically against an injected clock.

**Depends on:** M1.1

#### M1.3 — Token-bucket rate limiter (injected clock, no wall-clock)

**Goal.** Provide a deterministic, testable token-bucket limiter (~120/min ceiling, configured to 100/min) that throttles outbound requests, with an injected clock/sleep so tests don't sleep on the wall clock.

**Build (files):**

- `src/trader/schwab/rate_limit.py` *(create)* — TokenBucket class: __init__(rate_per_min, clock, sleep_fn). State: capacity (=rate_per_min), tokens (float), last_refill (monotonic-ish from clock). acquire() refills tokens based on elapsed time since last_refill, and if <1 token computes wait seconds and calls sleep_fn(wait) then deducts a token. Pure arithmetic; no global state.
- `tests/unit/schwab/__init__.py` *(create)* — Test package marker for schwab unit tests.

**Libraries:** —

**Details.** Refill rate = rate_per_min/60 tokens per second. clock supplies a monotonic timestamp (inject a callable returning float seconds; in production wire to time.monotonic via a small adapter, but the limiter only knows the injected callable so tests stay deterministic). sleep_fn is injected (default time.sleep) so tests pass a fake that records requested sleeps and advances the fake clock. Burst capacity = full bucket at start. Edge case: never allow tokens to exceed capacity on refill; wait computation = (1 - tokens)/refill_rate. Keep the limiter agnostic of httpx so it is unit-testable in isolation and reusable by the broker client later (M5).

**Validation — unit tests:**

- tests/unit/schwab/test_rate_limit.py::test_burst_up_to_capacity asserts the first `rate_per_min` acquire() calls do not sleep (fake sleep records nothing) when clock does not advance
- tests/unit/schwab/test_rate_limit.py::test_throttles_when_empty asserts that after draining the bucket, the next acquire() requests a sleep of ~ (60/rate) seconds via the fake sleep_fn
- tests/unit/schwab/test_rate_limit.py::test_refill_over_time asserts that advancing the fake clock by 30s refills ~rate/2 tokens

**Validation — manual:**

- Run: pytest tests/unit/schwab/test_rate_limit.py -q — expected: all rate limiter tests pass without any real sleeping (suite finishes in <1s)

**Deliverable.** A deterministic token-bucket rate limiter unit usable by the HTTP transport, fully covered by clock-injected tests.

**Depends on:** M1.1

#### M1.4 — OAuth token-exchange + refresh primitives (httpx, parity-checked)

**Goal.** Implement the low-level OAuth POSTs to Schwab's token endpoint — authorization_code exchange and refresh_token grant — returning a TokenSet, with token scrubbing and typed error mapping, verified via respx (no live calls).

**Build (files):**

- `src/trader/auth/oauth.py` *(create)* — Functions: build_authorize_url(config) -> str (constructs OAUTH_AUTHORIZE_URL?client_id=&redirect_uri=&response_type=code). exchange_code(http_client, config, code, clock) -> TokenSet (POST token endpoint grant_type=authorization_code, Authorization: Basic b64(key:secret), redirect_uri). refresh_access_token(http_client, config, refresh_token, clock) -> TokenSet (grant_type=refresh_token). Parses access_token, refresh_token, expires_in, scope; computes access_token_expires_at = clock.now()+expires_in; preserves/refreshes refresh_token_issued_at.
- `tests/unit/auth/test_oauth.py` *(create)* — respx-mocked tests of the two POSTs and the authorize URL builder.

**Libraries:** `httpx`, `respx`

**Details.** Uses an injected httpx.Client (sync) so transport is testable and shared. Authorization header is Basic base64(app_key:app_secret) per §8.2 (mark [VERIFY]). On a 4xx with invalid_grant / invalid_token on the REFRESH call, raise SchwabRefreshTokenDeadError (drives safe mode); on other 4xx raise SchwabAuthError; on 5xx raise SchwabServerError (so the transport layer can retry). Token scrubbing: when logging the request/response, redact access_token/refresh_token/code/Authorization header — implement a scrub() helper that replaces secret substrings with '***'. Edge cases: authorization code is single-use ~30s (§8.2) — surface a clear SchwabAuthError on reuse 4xx; refresh response may or may not include a new refresh_token — if absent, keep the existing refresh_token but DO NOT reset refresh_token_issued_at (age keeps counting toward the 7-day cap). expires_in defaults handled defensively. Parity note in a module docstring: endpoint shapes confirmed against schwab-py/Schwabdev as a spec, not imported.

**Validation — unit tests:**

- tests/unit/auth/test_oauth.py::test_build_authorize_url asserts the URL contains client_id, redirect_uri, response_type=code
- tests/unit/auth/test_oauth.py::test_exchange_code_success uses respx to mock the token endpoint returning a sample token JSON and asserts the returned TokenSet has correct access_token, refresh_token, and access_token_expires_at = now+expires_in (FakeClock)
- tests/unit/auth/test_oauth.py::test_refresh_keeps_issued_at_when_no_new_refresh asserts refresh_token_issued_at is preserved when the refresh response omits refresh_token
- tests/unit/auth/test_oauth.py::test_refresh_invalid_grant_raises_dead asserts a respx-mocked 400 invalid_grant on refresh raises SchwabRefreshTokenDeadError
- tests/unit/auth/test_oauth.py::test_basic_auth_header asserts the captured request Authorization header equals 'Basic '+b64(key:secret)
- tests/unit/auth/test_oauth.py::test_tokens_scrubbed_in_logs asserts that with caplog the access/refresh token strings never appear in emitted log records
- ⚙ *(added in plan review)* test_oauth.py::test_exchange_code_reuse_raises_auth_error — a 400 invalid_grant on the authorization_code EXCHANGE raises SchwabAuthError (NOT SchwabRefreshTokenDeadError)
- ⚙ *(added in plan review)* test_oauth.py::test_missing_expires_in_uses_default and ::test_malformed_token_response_raises_bad_response (missing access_token → SchwabBadResponseError)

**Validation — manual:**

- Run: pytest tests/unit/auth/test_oauth.py -q — expected: all OAuth primitive tests pass with no network access (respx asserts all routes mocked)

**Deliverable.** OAuth code-exchange and refresh functions returning TokenSet, contract-tested against recorded Schwab token-endpoint shapes, with token scrubbing.

**Depends on:** M1.1, M1.2

> ⚙ **Plan-review note.** Pin the exchange-vs-refresh error-mapping distinction: only the REFRESH invalid_grant path is 'dead'; the EXCHANGE path is a plain auth error.

#### M1.5 — Local HTTPS loopback callback server for the authorization-code flow

**Goal.** Capture the OAuth redirect callback (the single-use code) on a local HTTPS loopback listener so the interactive browser login can complete and hand the code to exchange_code.

**Build (files):**

- `src/trader/auth/callback_server.py` *(create)* — CallbackServer: starts an HTTPS http.server on the redirect host/port (default 127.0.0.1:8182) using an SSL context from a provided cert/key (self-signed). serve_until_code(timeout) blocks until the GET callback with ?code= arrives, returns the code (and state), then shuts down. Renders a minimal 'You may close this window' HTML. Includes a helper generate_self_signed_cert(path) for first-run setup (or documents using a prebuilt cert).
- `tests/unit/auth/test_callback_server.py` *(create)* — Tests parsing of the callback query and the bound port/host, without doing a real browser flow.

**Libraries:** —

**Details.** Per §8.1/§16.4: redirect must be HTTPS even on loopback. Use stdlib http.server + ssl.SSLContext.wrap_socket. The server runs in a background thread; serve_until_code waits on a threading.Event set by the handler when it parses ?code=. Validate the state param if provided (CSRF for the OAuth dance). Edge cases: timeout -> raise SchwabAuthError('callback timed out'); error callback (?error=) -> raise SchwabAuthError with the scrubbed error; only bind to loopback (never 0.0.0.0). Self-signed cert generation can use the cryptography lib if already a dep, otherwise document an openssl one-liner and accept a cert path via config — keep the unit test focused on query parsing (call the handler's parse logic directly or hit it over localhost with verify=False) so CI needs no browser. This is exercised manually end-to-end in M1.7.

**Validation — unit tests:**

- tests/unit/auth/test_callback_server.py::test_parses_code_from_query asserts the handler extracts code='abc' and state='xyz' from a request path '/?code=abc&state=xyz'
- tests/unit/auth/test_callback_server.py::test_error_callback_raises asserts a path '/?error=access_denied' surfaces a SchwabAuthError
- tests/unit/auth/test_callback_server.py::test_binds_loopback_only asserts the configured bind host is 127.0.0.1 (never 0.0.0.0)
- ⚙ *(added in plan review)* test_callback_server.py::test_generate_self_signed_cert_produces_usable_context (cert/key load into an SSLContext)
- ⚙ *(added in plan review)* test_callback_server.py::test_https_loopback_roundtrip (network-marked, opt-in): real httpx GET to https://127.0.0.1:<port>/?code= with verify=False captures the code over TLS

**Validation — manual:**

- Run: pytest tests/unit/auth/test_callback_server.py -q — expected: callback parsing tests pass without launching a browser

**Deliverable.** An HTTPS loopback callback capture server that yields the OAuth code to the exchange step, ready for the interactive reauth flow.

**Depends on:** M1.1

> ⚙ **Plan-review note.** The HTTPS-on-loopback requirement (§8.1) is a [VERIFY] correctness point — add an opt-in (network-marked) TLS roundtrip test so it isn't only smoke-tested.

#### M1.6 — Resilient HTTP transport: rate limit + tenacity retry + 401→refresh→retry + safe mode

**Goal.** Build the central authenticated HTTP transport that injects the bearer token, throttles via the limiter, retries 429/5xx with backoff (tenacity), auto-refreshes the access token, transparently does 401→refresh→retry, and flips to READ-ONLY safe mode when the refresh token is dead.

**Build (files):**

- `src/trader/schwab/http.py` *(create)* — SchwabHttp class: __init__(config, http_client (httpx.Client), token_store, rate_limiter, clock, alerter, oauth funcs). request(method, path, params/json) flow: (1) if in safe_mode raise SchwabReadOnlyModeError; (2) ensure_valid_access_token() — load TokenSet, if access_expired refresh it (and on SchwabRefreshTokenDeadError enter safe mode + alert); (3) rate_limiter.acquire(); (4) send with Authorization: Bearer; (5) on 401 -> refresh once -> retry once; (6) on 429/5xx raise typed errors that a tenacity-wrapped retry handles with exponential backoff + jitter; (7) scrub logs. Exposes enter_safe_mode()/is_read_only and get_json helper.
- `src/trader/schwab/retry.py` *(create)* — tenacity Retrying policy factory: retry on SchwabRateLimitError + SchwabServerError only (never on auth/4xx), wait=wait_exponential_jitter, stop_after_attempt(config.max_retries), honoring a Retry-After header when present. Uses an injected sleep so tests don't wall-sleep (tenacity sleep override).

**Libraries:** `httpx`, `tenacity`, `respx`

**Details.** This is the security-and-correctness heart of M1. Order of operations matters: refresh-before-expiry happens proactively (cheap), the 401 path is the reactive fallback. The 401 path refreshes EXACTLY once then retries the original request once; a second 401 raises SchwabAuthError (avoid infinite loops). 429/5xx are mapped to retryable typed errors and run through tenacity with exponential backoff + jitter and a max attempt count from config (§8.6). Honor Retry-After if Schwab sends it. SAFE MODE: when refresh_access_token raises SchwabRefreshTokenDeadError, set self._safe_mode=True, call alerter.alert('refresh token dead — entering READ-ONLY safe mode'), and every subsequent request() raises SchwabReadOnlyModeError — never crash-loop (§8.2 #4). Inject the tenacity sleep and the clock so all backoff timing is deterministic in tests (no wall-clock). Token scrubbing applied to every request/response log line (reuse scrub() from M1.4). Edge cases: missing token in store -> raise SchwabAuthError('not authenticated; run reauth'); idempotent GETs only here (no writes in M1, so retry is always safe).

**Validation — unit tests:**

- tests/unit/schwab/test_http_refresh.py::test_proactive_refresh_when_access_expired uses respx + FakeClock to assert an expired access token triggers a refresh POST before the data GET
- tests/unit/schwab/test_http_401.py::test_401_triggers_refresh_then_retry asserts a respx route returning 401 then 200 causes exactly one refresh and one retry, returning the 200 body
- tests/unit/schwab/test_http_401.py::test_double_401_raises asserts two consecutive 401s raise SchwabAuthError (no infinite loop)
- tests/unit/schwab/test_http_retry.py::test_429_backoff_then_success asserts respx 429,429,200 succeeds within max_retries and the injected sleep recorded backoff waits (honoring Retry-After when set)
- tests/unit/schwab/test_http_retry.py::test_5xx_exhausts_retries_raises asserts persistent 503 raises SchwabServerError after config.max_retries attempts
- tests/unit/schwab/test_http_safe_mode.py::test_dead_refresh_enters_safe_mode asserts a dead-refresh response sets is_read_only True, calls alerter once, and the next request() raises SchwabReadOnlyModeError
- tests/unit/schwab/test_http_safe_mode.py::test_no_retry_on_auth_errors asserts SchwabAuthError is never retried by the tenacity policy
- ⚙ *(added in plan review)* test_http_retry.py::test_429_honors_retry_after_header (separate from the exponential-backoff test)
- ⚙ *(added in plan review)* test_http_refresh.py::test_proactive_refresh_avoids_401_path and ::test_401_on_nonexpired_token_still_refreshes_once (precedence of proactive vs reactive)
- ⚙ *(added in plan review)* test_http_refresh.py::test_no_token_raises_not_authenticated (empty store → SchwabAuthError before any HTTP call)

**Validation — manual:**

- Run: pytest tests/unit/schwab/test_http_refresh.py tests/unit/schwab/test_http_401.py tests/unit/schwab/test_http_retry.py tests/unit/schwab/test_http_safe_mode.py -q — expected: all transport resilience tests pass, suite finishes fast (injected sleep, no real backoff)

**Deliverable.** A resilient, auth-aware HTTP transport with rate limiting, retry/backoff, transparent 401-refresh-retry, and fail-safe READ-ONLY mode, fully contract-tested.

**Depends on:** M1.2, M1.3, M1.4

> ⚙ **Plan-review note.** SPLIT WHEN BUILDING into 3 baby steps — M1.6a retry.py tenacity policy (retry only rate-limit/server, honor Retry-After, injected sleep); M1.6b SchwabHttp core request path (token inject, rate-limit, map 429/5xx, run policy); M1.6c auth behaviors (proactive refresh, 401→refresh→retry-once, dead-refresh→safe-mode+alert). This is the security/correctness heart of M1.

#### M1.7 — OAuth orchestration: authenticate + auto-refresh + token-age alert scheduler hook

**Goal.** Tie the OAuth primitives, callback server, token store, and HTTP transport into a single authenticator that runs the interactive first-auth, persists tokens, and exposes a check_token_age() that fires the 7-day re-auth alert.

**Build (files):**

- `src/trader/auth/authenticator.py` *(create)* — Authenticator class: interactive_authorize(open_browser_fn) — builds authorize URL, opens browser (injected fn, default webbrowser.open), starts CallbackServer, captures code, calls exchange_code, saves TokenSet to store, returns it. check_token_age() — loads TokenSet, if refresh_alert_due fires alerter.alert(re-auth reminder with days remaining), if refresh_expired enters safe mode + alerts. ensure_authenticated() — raises if no token. Pure dependency injection (store, oauth funcs, callback server factory, alerter, clock).
- `tests/unit/auth/test_authenticator.py` *(create)* — Tests the age-check alerting and the authorize-then-save flow with fakes (no real browser/network).

**Libraries:** `respx`

**Details.** interactive_authorize uses an injected open_browser_fn so tests pass a no-op and an injected CallbackServer fake that returns a canned code; exchange_code is respx-mocked. check_token_age is the function the daemon scheduler (M3) will call periodically; here it must (a) emit a single alert when refresh_alert_due (age >= 7-2=5 days by default) and (b) on full expiry call into the transport/safe-mode signal. The alert message includes days-until-expiry and a one-line pointer to the §16.4 reauth runbook (Option A). Edge cases: no token stored -> check_token_age is a no-op or distinct 'not authenticated' alert (configurable); avoid duplicate alerts by recording last-alert state (a column in the token store or a returned flag the caller dedupes) — for M1 simply return a structured AlertDecision and let the test assert it. This step satisfies the exit criterion 'token-age alert fires in test'.

**Validation — unit tests:**

- tests/unit/auth/test_authenticator.py::test_age_alert_fires_at_5_days uses FakeClock + a token issued 5 days ago and asserts alerter.alert was called once with a message containing the days-remaining countdown
- tests/unit/auth/test_authenticator.py::test_no_alert_before_lead_window asserts no alert at age 4.0 days (lead=2, cap=7)
- tests/unit/auth/test_authenticator.py::test_expired_refresh_signals_safe_mode asserts age 7.5 days triggers the safe-mode/expired alert path
- tests/unit/auth/test_authenticator.py::test_interactive_authorize_saves_token uses a fake callback server returning code='c', respx-mocked exchange, and asserts the resulting TokenSet was saved to the store

**Validation — manual:**

- Run: pytest tests/unit/auth/test_authenticator.py -q — expected: token-age alert and authorize/save tests pass
- Real-credential smoke (optional, outside CI; SAFE — read-only, no orders): with SCHWAB_APP_KEY/SECRET in env and a registered https://127.0.0.1:8182 redirect, run the `reauth` flow wired in M1.9 and confirm a token file is written to the state DB and `status` shows a token-age countdown

**Deliverable.** An Authenticator that performs interactive first-auth, persists tokens, and fires the 7-day re-auth alert deterministically in tests.

**Depends on:** M1.2, M1.4, M1.5, M1.6

#### M1.8 — Typed endpoint models + read endpoints: quotes, pricehistory, accountNumbers

**Goal.** Add typed request/response models and the three read-only endpoint methods (quotes, pricehistory candles, hashed account id resolution) on top of the resilient transport, parsing Schwab JSON into typed objects.

**Build (files):**

- `src/trader/schwab/models.py` *(create)* — Typed models (pydantic or frozen dataclasses) mirroring Schwab JSON: SchwabQuote (symbol, lastPrice, bidPrice, askPrice, totalVolume, closePrice/prev_close, quoteTime epoch-ms), SchwabCandle (datetime epoch-ms, open, high, low, close, volume), SchwabPriceHistory (symbol, candles list), AccountNumberMapping (accountNumber -> hashValue). Parsers convert epoch-ms to tz-aware UTC datetime and numeric fields to Decimal.
- `src/trader/schwab/endpoints.py` *(create)* — SchwabClient (read-only facade) methods: get_quotes(symbols: list[str], fields='quote') -> dict[str, SchwabQuote] (GET /marketdata/v1/quotes?symbols=&fields=); get_price_history(symbol, periodType, period, frequencyType, frequency, startDate?, endDate?) -> SchwabPriceHistory; get_account_numbers() -> list[AccountNumberMapping] (GET /trader/v1/accounts/accountNumbers). All go through SchwabHttp.get_json. NO order/positions/balances methods (those are M5).
- `tests/unit/schwab/test_endpoints.py` *(create)* — respx contract tests using recorded-shape JSON fixtures for each endpoint.
- `tests/fixtures/schwab/` *(create)* — Recorded/representative JSON response fixtures: quotes_AAPL_MSFT.json, pricehistory_AAPL_daily.json, account_numbers.json (sanitized, parity-checked against §8.4/§8.5 shapes).

**Libraries:** `respx`

**Details.** Models use Decimal for all prices (matches core.Quote/Bar in §5) and parse Schwab epoch-ms timestamps to tz-aware UTC. quotes endpoint is batched (symbols comma-joined) per §8.4. pricehistory params follow §8.4 ([VERIFY] combos) — for M1 focus on daily candles (periodType=year/month, frequencyType=daily, frequency=1). account_numbers maps raw->hashed; expose only the hashed id to callers (raw number must never leak into logs/UI per §8.5 + §13). Edge cases: missing prev_close/closePrice -> None; empty candles list returns empty SchwabPriceHistory; a symbol absent from the quotes response is omitted from the dict (caller decides). Contract tests assert exact query params (symbols=AAPL,MSFT&fields=quote), correct parsing of one full fixture, and that status-enum-like fields parse. These are the recorded-HTTP contract tests from §15 #5 (no live calls).

**Validation — unit tests:**

- tests/unit/schwab/test_endpoints.py::test_get_quotes_parses_fixture asserts get_quotes(['AAPL','MSFT']) returns two SchwabQuote objects with Decimal prices and tz-aware UTC quoteTime from the fixture
- tests/unit/schwab/test_endpoints.py::test_get_quotes_query_params asserts the captured request URL has symbols=AAPL,MSFT and fields=quote
- tests/unit/schwab/test_endpoints.py::test_get_price_history_daily parses pricehistory_AAPL_daily.json into N SchwabCandle with epoch-ms converted to UTC datetimes in ascending order
- tests/unit/schwab/test_endpoints.py::test_get_account_numbers_returns_hashed asserts the mapping exposes hashValue and that the raw accountNumber is not present in any log line (caplog scrub check)
- tests/unit/schwab/test_endpoints.py::test_missing_prev_close_is_none asserts a quote fixture without closePrice yields prev_close=None

**Validation — manual:**

- Run: pytest tests/unit/schwab/test_endpoints.py -q — expected: all three endpoint contract tests pass against fixtures with no network

**Deliverable.** A read-only SchwabClient exposing quotes, daily pricehistory, and hashed account-number resolution, with typed models and recorded-HTTP contract tests.

**Depends on:** M1.6

#### M1.9 — SchwabMarketData provider adapter (implements core MarketDataProvider) + reauth/status CLI hooks

**Goal.** Adapt the read-only SchwabClient to the core MarketDataProvider interface (asof-bound get_quote/get_bars) and wire the interactive reauth + token-status into the existing CLI, so the rest of the system consumes Schwab via the abstract interface.

**Build (files):**

- `src/trader/data/schwab_market_data.py` *(create)* — SchwabMarketData implementing core.MarketDataProvider: __init__(schwab_client, clock, max_staleness_seconds). get_quote(symbol, asof) -> core.Quote (calls get_quotes([symbol]), maps SchwabQuote->Quote, enforces ts<=asof+tolerance staleness or raises). get_bars(symbol, start, end, freq, asof) -> Sequence[core.Bar] (calls get_price_history for daily, maps candles->Bar, filters ts<=asof for no-lookahead per §9.2/Appendix B).
- `src/trader/data/__init__.py` *(update)* — Export SchwabMarketData (create the file if M0 left it empty).
- `src/trader/app/cli.py` *(update)* — Add/flesh out `reauth` subcommand (invokes Authenticator.interactive_authorize) and extend `status` to print the refresh-token age/expiry countdown and READ-ONLY safe-mode flag. (Builds on the M0 CLI stub; no order commands.)
- `tests/unit/data/test_schwab_market_data.py` *(create)* — Tests the adapter mapping + asof no-lookahead filtering using a fake SchwabClient.

**Libraries:** —

**Details.** The adapter is the boundary that lets live/backtest parity hold (§5): it implements exactly MarketDataProvider so the orchestrator/strategies (later milestones) never see Schwab specifics. get_bars MUST filter to candles with ts <= asof (no-lookahead is structural, Appendix B). get_quote enforces staleness: if quote.ts is older than asof - max_staleness_seconds, raise a typed error (feeds the M5 risk price-sanity rule, but the staleness check lives at the data boundary too). Map Decimal/tz-aware fields straight through. The CLI `reauth` is the §16.4 Option-A entrypoint (run on laptop). `status` reads the token store (read-only) and prints days-until-refresh-expiry and whether the client is in safe mode — this is also the manual exit-criterion surface. Edge cases: symbol not returned -> raise a clear 'no quote' error; freq other than daily -> raise NotImplementedError for M1 (intraday is [VERIFY]/later). Use a fake SchwabClient (not respx) in adapter tests to keep them focused on mapping/filtering.

**Validation — unit tests:**

- tests/unit/data/test_schwab_market_data.py::test_get_quote_maps_to_core_quote asserts a fake SchwabClient quote maps to core.Quote with matching Decimal last/bid/ask and tz-aware ts
- tests/unit/data/test_schwab_market_data.py::test_get_quote_stale_raises asserts a quote older than max_staleness_seconds relative to asof raises the staleness error
- tests/unit/data/test_schwab_market_data.py::test_get_bars_no_lookahead asserts get_bars filters out candles with ts > asof (Appendix B), returning only the in-range bars in ascending order
- tests/unit/data/test_schwab_market_data.py::test_get_bars_daily_mapping asserts daily candles map to core.Bar with Decimal OHLC and int volume
- ⚙ *(added in plan review)* test_schwab_market_data.py::test_get_quote_at_staleness_boundary (ts == asof − max_staleness exactly: pin inclusive/exclusive)

**Validation — manual:**

- Run: pytest tests/unit/data/test_schwab_market_data.py -q — expected: adapter mapping + no-lookahead tests pass
- Run (no creds needed): `python -m trader.app.cli status` — expected: prints mode, token-age countdown (or 'not authenticated'), and safe-mode=false; no orders are placed and no live network call is required for the status path when unauthenticated
- Real-credential smoke (optional, outside CI; SAFE read-only): `python -m trader.app.cli reauth` completes the browser login on https://127.0.0.1:8182, then a one-off script calling SchwabMarketData.get_quote('AAPL', now) returns a live Quote and get_bars returns daily candles — confirming the exit criterion 'authenticate + fetch live quotes/daily candles'. No order placement occurs.

**Deliverable.** A SchwabMarketData adapter conforming to MarketDataProvider plus working `reauth`/`status` CLI hooks, enabling live quote/daily-candle reads through the abstract interface.

**Depends on:** M1.7, M1.8

#### M1.10 — Credential-handling security review + log-scrubbing assertions + dependency pinning

**Goal.** Satisfy the M1 exit criterion 'security review of credential-handling code done': audit every place credentials/tokens are read/stored/logged/transmitted, add cross-cutting tests proving no secret ever appears in logs or outputs, and pin/hash-lock the new dependencies.

**Build (files):**

- `docs/security/m1-credential-review.md` *(create)* — Written security review: enumerates the credential surface (app_key/secret via SecretStr; tokens in SQLite token store; Basic auth header; Bearer header; callback code), confirms only auth/secrets components read credentials (§13 single-secrets-component rule), confirms scrubbing in all log paths, lists residual risks + mitigations, and records the [VERIFY] Schwab facts still to confirm against the live portal.
- `tests/unit/schwab/test_no_secret_leakage.py` *(create)* — Cross-cutting tests: run representative client flows (token exchange, refresh, get_quotes, get_account_numbers) under caplog and assert no access_token/refresh_token/app_secret/code/raw-account-number substring appears in any captured log record or exception message.
- `tests/unit/auth/test_token_store_perms.py` *(create)* — Asserts the token store file (and any token file) is created with restrictive permissions (chmod 600 equivalent) and never written to a git-tracked path.
- `pyproject.toml` *(update)* — Add pinned deps with hashes: httpx, tenacity, respx (dev), freezegun (dev); ensure lockfile updated (Poetry/uv) per §13 pin+hash-lock; add state/, data/, *.sqlite, .env to .gitignore if not already (verify).

**Libraries:** `httpx`, `tenacity`, `respx`, `freezegun`

**Details.** This step is gated on all client code existing so the review covers the real surface. The review doc must explicitly walk: (1) where SecretStr is used and that .get_secret_value() is only called inside auth/oauth at the moment of building the Basic header; (2) that token_store is the only persistence of tokens and uses 600 perms; (3) that scrub() wraps every request/response/error log in http.py and oauth.py; (4) that the SchwabMarketData/CLI never expose tokens or the raw account number (only hashed id, §8.5); (5) supply-chain: httpx/tenacity pinned+hashed, no third-party broker SDK imported (§8.7). The leakage test should iterate over a list of known-secret literals injected into the flows and assert absence in caplog.text and in str(exc) for each raised typed error. Edge case: ensure scrubbing also covers the Authorization header value (Basic and Bearer) and Retry-After/debug dumps. This step has no behavioral code changes beyond hardening and is the formal milestone exit gate.

**Validation — unit tests:**

- tests/unit/schwab/test_no_secret_leakage.py::test_no_token_in_logs asserts that after exchange/refresh/quote/account flows under caplog, none of the secret literals appear in caplog.text
- tests/unit/schwab/test_no_secret_leakage.py::test_no_secret_in_exception_messages asserts raised SchwabAuthError/SchwabBadResponseError str() contains no token/secret
- tests/unit/schwab/test_no_secret_leakage.py::test_no_raw_account_number_leak asserts raw accountNumber never appears in logs (only hashValue used downstream)
- tests/unit/auth/test_token_store_perms.py::test_token_file_permissions asserts the created sqlite/token file mode is 0o600

**Validation — manual:**

- Run: pytest tests/unit -q — expected: full M1 unit + contract suite (config, tokens, store, rate limit, oauth, callback, http resilience, authenticator, endpoints, adapter, leakage, perms) passes with no live network calls
- Open docs/security/m1-credential-review.md — expected: completed review covering all five credential surfaces with sign-off, satisfying the 'security review done' exit criterion
- Run the dependency audit: `pip hash`/lockfile check (or `uv lock --locked`) — expected: httpx/tenacity pinned with hashes, no third-party Schwab SDK present

**Deliverable.** A completed credential-handling security review, cross-cutting no-secret-leakage and file-permission tests, and pinned+hash-locked dependencies — closing all M1 exit criteria with zero order-placement capability.

**Depends on:** M1.9

> ⚙ **Plan-review note.** This is an EXIT-GATE aggregation (review markdown + cross-cutting leakage/permission tests + dep pin/hash-lock), not a feature step — acceptable to keep bundled. Decide & test the 'never written to a git-tracked path' guarantee (or drop the claim).


## M2 — Backtest engine

> **Intent.** Build the event-driven backtest harness that runs the SAME strategy/decision code over historical data: VirtualClock, an asof-bound HistoricalDataProvider backed by a Parquet cache, a realistic SimBroker (slippage/fees/partials), portfolio/P&L, the engine loop (single strategy here; multi-strategy interleave is added in M3), a reproducibility manifest, and a report. This milestone OWNS backtest/engine.py and backtest/report.py (later milestones update them).
>
> **Prerequisites:** M0; M1 (SchwabMarketData adapter is used by the M2.4 ingestion step).
> **New libraries:** `pandas`, `pyarrow`, `duckdb`, `numpy`
>
> **Exit criteria.** A trivial single strategy backtests over cached daily history end-to-end via `trader backtest`; the run is deterministic and a golden run reproduces bit-for-bit; no-lookahead is enforced structurally (asof) and tested; SimBroker fills model slippage/fees/partials. No live trading.

*11 sub-steps.*

#### M2.1 — Clock implementations (Real + Virtual)

**Goal.** Implement the two Clock impls so the same code runs on wall-clock (live) and a controllable virtual clock (backtest).

**Build (files):**

- `src/trader/clock/__init__.py` *(create)* — Package marker.
- `src/trader/clock/real.py` *(create)* — RealClock implementing Clock: now() returns tz-aware UTC datetime; monotonic() for intervals; is_market_open delegates to the calendar (M3) or a passed predicate.
- `src/trader/clock/virtual.py` *(create)* — VirtualClock implementing Clock: holds a current instant; now() returns it; advance_to(ts)/advance(delta) move it forward only (assert monotonic).
- `tests/unit/clock/test_clocks.py` *(create)* — Behavior tests.

**Libraries:** —

**Details.** VirtualClock is the backbone of no-lookahead (Appendix B): the engine advances it to each trigger and all data reads are bound to now(). advance_to must reject moving backward. RealClock never used in backtests.

**Validation — unit tests:**

- tests/unit/clock/test_clocks.py::test_virtual_advances_forward_only asserts advance_to(past) raises
- tests/unit/clock/test_clocks.py::test_virtual_now_returns_set_instant
- tests/unit/clock/test_clocks.py::test_realclock_tz_aware asserts now() is tz-aware UTC

**Validation — manual:**

- Run: `uv run pytest tests/unit/clock -q` — expected: green

**Deliverable.** RealClock + VirtualClock conforming to Clock, with monotonic forward-only virtual time.

**Depends on:** M0.3

#### M2.2 — Historical data cache (Parquet + catalog)

**Goal.** Cache OHLCV history on disk as partitioned Parquet with a catalog tracking cached ranges + content hashes, so backtests are fast, offline, and reproducible.

**Build (files):**

- `src/trader/data/cache.py` *(create)* — ParquetCache: write_bars(symbol, df), read_bars(symbol, start, end), missing_ranges(symbol, start, end), content_hash(symbol). Partition by symbol (and year). A small catalog table (sqlite or duckdb) tracks ingested ranges + ingest timestamp + content hash.
- `tests/unit/data/test_cache.py` *(create)* — Roundtrip, missing-range, content-hash tests.

**Libraries:** `pandas`, `pyarrow`, `duckdb`

**Details.** Per §9.4: partitioned Parquet + a catalog; DuckDB can query Parquet directly. content_hash over the canonical Parquet bytes feeds the run manifest (M2.9) so a backtest references an exact data snapshot. missing_ranges enables cache-on-demand ingestion (M2.4).

**Validation — unit tests:**

- tests/unit/data/test_cache.py::test_write_read_roundtrip asserts written bars read back equal
- tests/unit/data/test_cache.py::test_missing_ranges asserts gaps are computed correctly across partial coverage
- tests/unit/data/test_cache.py::test_content_hash_stable asserts the same data yields the same hash and different data differs

**Validation — manual:**

- Run: `uv run pytest tests/unit/data/test_cache.py -q` — expected: green

**Deliverable.** A content-hashed Parquet bar cache with range tracking for fast, reproducible offline backtests.

**Depends on:** M0.2

#### M2.3 — HistoricalDataProvider (asof-bound, no-lookahead)

**Goal.** Implement MarketDataProvider over the cache so the SAME strategy code is fed point-in-time data with structural no-lookahead.

**Build (files):**

- `src/trader/data/historical.py` *(create)* — HistoricalDataProvider(cache, clock, latency_seconds) implementing MarketDataProvider: get_quote(symbol, asof) and get_bars(symbol, start, end, freq, asof) return ONLY rows with ts <= asof - latency (Appendix B). Quote synthesized from the at-or-before bar/last.
- `tests/unit/data/test_historical.py` *(create)* — No-lookahead + boundary tests.

**Libraries:** —

**Details.** The single most important rule (Appendix B): never expose data after asof. latency models signal-to-data delay. get_bars returns ascending in-range bars; get_quote uses the last bar at-or-before asof.

**Validation — unit tests:**

- tests/unit/data/test_historical.py::test_no_lookahead asserts bars with ts>asof are excluded
- tests/unit/data/test_historical.py::test_asof_boundary_inclusive_exclusive pins behavior at ts==asof and ts==asof-latency exactly
- tests/unit/data/test_historical.py::test_quote_is_at_or_before_asof

**Validation — manual:**

- Run: `uv run pytest tests/unit/data/test_historical.py -q` — expected: green

**Deliverable.** An asof-bound HistoricalDataProvider that structurally prevents lookahead, reusable unchanged by the engine.

**Depends on:** M2.1, M2.2, M0.3

#### M2.4 — Data ingestion CLI (Schwab pricehistory → cache)

**Goal.** Add `trader data fetch` to pull daily candles from Schwab into the Parquet cache (read-only), so backtests run offline thereafter.

**Build (files):**

- `src/trader/app/cli.py` *(update)* — Add `data fetch --symbols --start --end --freq daily` invoking SchwabMarketData (M1) → ParquetCache; only fetches missing_ranges.
- `src/trader/data/ingest.py` *(create)* — ingest_daily(provider, cache, symbols, start, end): for each missing range, fetch bars via the MarketDataProvider and append to cache.
- `tests/unit/data/test_ingest.py` *(create)* — Ingestion-writes-cache test using FakeMarketDataProvider.

**Libraries:** —

**Details.** Uses the §8.4 daily pricehistory via the M1 SchwabMarketData adapter (read-only; no orders). Ingestion is cache-on-demand: only missing ranges fetched. CI tests use FakeMarketDataProvider; the real fetch is a manual smoke (needs creds, read-only).

**Validation — unit tests:**

- tests/unit/data/test_ingest.py::test_ingest_writes_missing_only asserts only missing ranges are fetched and written (FakeMarketDataProvider call count)

**Validation — manual:**

- Run: `uv run pytest tests/unit/data/test_ingest.py -q` — expected: green
- Real-credential smoke (optional, read-only, no orders): `trader data fetch --symbols AAPL,MSFT --start 2023-01-01 --end 2023-12-31` — expected: Parquet files appear under data/ and a re-run fetches nothing

**Deliverable.** An offline-first daily-candle ingestion path populating the Parquet cache from Schwab.

**Depends on:** M2.2

#### M2.5 — SimBroker core (fills, slippage, fees)

**Goal.** Implement the simulated broker's core: positions/cash tracking and market-order fills at the next quote/bar ± slippage with commissions/fees, matching the live broker economics.

**Build (files):**

- `src/trader/broker/__init__.py` *(create)* — Package marker.
- `src/trader/broker/sim.py` *(create)* — SimBroker implementing Broker: in-memory cash/positions; submit_order(MARKET) fills at next available quote/bar ± half-spread + slippage; applies a FeesModel (Schwab $0 commission + regulatory bps); get_positions/get_account; deterministic. A SlippageModel (bps/fixed/vol) and FeesModel are injected from config.
- `tests/unit/broker/test_sim_core.py` *(create)* — Fill price, cash/position, fees math.

**Libraries:** —

**Details.** Per §9.3: never fill at the signal instant — fill on the next quote/bar (the engine advances the clock between decision and fill). Conservative default: next price ± half-spread + slippage_bps. FeesModel adds SEC/TAF bps so backtest P&L matches live. Models injected from BacktestConfig (parity with live calibration later).

**Validation — unit tests:**

- tests/unit/broker/test_sim_core.py::test_market_fill_price asserts a BUY fills at next price + slippage and SELL at next price − slippage
- tests/unit/broker/test_sim_core.py::test_cash_and_position_update asserts cash decreases by qty*price+fees on BUY and position increments
- tests/unit/broker/test_sim_core.py::test_fees_applied asserts regulatory bps fees are deducted

**Validation — manual:**

- Run: `uv run pytest tests/unit/broker/test_sim_core.py -q` — expected: green

**Deliverable.** A deterministic SimBroker for market orders with realistic slippage and fees.

**Depends on:** M0.3, M0.8

#### M2.6 — SimBroker advanced fills (limit + partial)

**Goal.** Add limit-order range-crossing fills and volume-capped partial fills with working-order remainder, so higher-turnover strategies are modeled realistically.

**Build (files):**

- `src/trader/broker/sim.py` *(update)* — Add LIMIT handling: fill only if the bar's [low,high] crosses the limit price; partial fills capped at a fraction of bar volume (ADV cap from config); carry remainder as a WORKING order; expire DAY orders at session end.
- `tests/unit/broker/test_sim_fills.py` *(create)* — Limit fill/no-fill, partial, remainder, expiry tests.

**Libraries:** —

**Details.** Per §9.3: limit orders fill only when the bar range crosses the limit; partials capped at ≤ N% of bar volume with the remainder carried as a working order; DAY orders expire at close. Keep deterministic.

**Validation — unit tests:**

- tests/unit/broker/test_sim_fills.py::test_limit_fills_when_range_crosses
- tests/unit/broker/test_sim_fills.py::test_limit_no_fill_when_out_of_range
- tests/unit/broker/test_sim_fills.py::test_partial_fill_capped_by_volume asserts qty capped at ADV fraction and remainder stays WORKING
- tests/unit/broker/test_sim_fills.py::test_day_order_expires_at_close

**Validation — manual:**

- Run: `uv run pytest tests/unit/broker/test_sim_fills.py -q` — expected: green

**Deliverable.** Limit + partial-fill modeling in SimBroker with working-order remainders and DAY expiry.

**Depends on:** M2.5

#### M2.7 — Backtest portfolio & P&L

**Goal.** Track equity, realized/unrealized P&L, and an equity-curve time series from fills, producing the record metrics/report consume.

**Build (files):**

- `src/trader/backtest/__init__.py` *(create)* — Package marker.
- `src/trader/backtest/portfolio.py` *(create)* — Portfolio: apply_fill(fill); mark_to_market(quotes); realized_pnl/unrealized_pnl; equity(); snapshot(ts) appends to an equity series; per-symbol average cost. (Per-strategy attribution is layered in M3.)
- `tests/unit/backtest/test_portfolio.py` *(create)* — P&L + equity math tests.

**Libraries:** —

**Details.** Average-cost accounting; realized P&L on closing trades; unrealized via mark-to-market. Emits equity snapshots used by the report (M2.10) and metrics (M6.5). Decimal throughout.

**Validation — unit tests:**

- tests/unit/backtest/test_portfolio.py::test_realized_pnl_on_close asserts buy@10 sell@12 yields +2*qty realized
- tests/unit/backtest/test_portfolio.py::test_unrealized_marks_to_market
- tests/unit/backtest/test_portfolio.py::test_equity_snapshot_series_grows

**Validation — manual:**

- Run: `uv run pytest tests/unit/backtest/test_portfolio.py -q` — expected: green

**Deliverable.** A portfolio/P&L tracker producing an equity curve and realized/unrealized P&L from fills.

**Depends on:** M2.5

#### M2.8 — Event-driven backtest engine (single strategy)

**Goal.** Build the core engine loop that advances the VirtualClock to each trigger and runs decision→sizing→SimBroker for ONE strategy, recording trades and equity (multi-strategy interleave comes in M3).

**Build (files):**

- `src/trader/backtest/engine.py` *(create)* — BacktestEngine.run(strategy, universe, slots, start, end, data, sim_broker, portfolio, clock, seed): iterate sessions × slots; advance clock to each fire_ts; build MarketSnapshot via asof reads; call strategy.decide → (sizing stub or simple qty) → sim_broker.submit_order → next-bar fill → portfolio.apply_fill; collect trades + equity.
- `tests/unit/backtest/test_engine.py` *(create)* — Deterministic single-strategy run on synthetic data.

**Libraries:** —

**Details.** This is the shared event-driven core (Appendix A). In M2 it uses fixed slot times (jitter/calendar/merge added in M3) and a trivial sizing. The loop is the SAME structure live (M3 daemon) runs — only Clock/Data/Broker differ. Records every decision for the report/audit.

**Validation — unit tests:**

- tests/unit/backtest/test_engine.py::test_runs_and_records_trades asserts a synthetic always-BUY strategy produces N fills over N triggers
- tests/unit/backtest/test_engine.py::test_engine_is_deterministic asserts two runs with the same seed produce identical trades

**Validation — manual:**

- Run: `uv run pytest tests/unit/backtest/test_engine.py -q` — expected: green

**Deliverable.** A working single-strategy event-driven backtest engine producing trades + an equity curve.

**Depends on:** M2.3, M2.6, M2.7

#### M2.9 — Run manifest + seeded determinism

**Goal.** Emit a reproducibility manifest (config hash, data hash, seed, git commit, lib versions) and plumb a seeded RNG, so any backtest result is exactly re-derivable and portable.

**Build (files):**

- `src/trader/backtest/manifest.py` *(create)* — build_manifest(config, data_hashes, seed): config_hash = sha256 of canonicalized config (sorted-key JSON of the resolved AppConfig); data_hash = ParquetCache content hashes; plus git commit + python/lib versions + seed. Writes a manifest.json with each run.
- `src/trader/backtest/rng.py` *(create)* — make_rng(seed) -> numpy Generator(PCG64); never the global RNG.
- `tests/unit/backtest/test_manifest.py` *(create)* — Hash canonicalization + manifest field tests.

**Libraries:** `numpy`

**Details.** Per §9.5: config_hash hashes the canonicalized (sorted-key JSON) resolved config bytes — defined precisely so it is portable across machines/Python minors; data_hash hashes Parquet content bytes. Seeded numpy Generator(PCG64), recorded in the manifest. This underpins the golden-run guarantee.

**Validation — unit tests:**

- tests/unit/backtest/test_manifest.py::test_config_hash_canonical asserts reordering YAML keys yields the SAME config_hash (canonicalization works)
- tests/unit/backtest/test_manifest.py::test_manifest_has_all_fields asserts seed, git_commit, lib_versions, data_hash present
- tests/unit/backtest/test_manifest.py::test_rng_is_seeded_not_global asserts make_rng(42) reproduces a sequence and is independent of the global RNG

**Validation — manual:**

- Run: `uv run pytest tests/unit/backtest/test_manifest.py -q` — expected: green

**Deliverable.** A portable, content-addressed run manifest + seeded RNG making every backtest exactly reproducible.

**Depends on:** M2.2

#### M2.10 — Backtest report (creates report.py) + golden run

**Goal.** Create the report module (equity curve, trade blotter, basic metrics, JSON output) and prove the whole pipeline is bit-for-bit reproducible via a golden run. This is the canonical creator of backtest/report.py.

**Build (files):**

- `src/trader/backtest/report.py` *(create)* — BacktestReport.build(trades, equity_series, manifest) -> a JSON report (+ later HTML in M6.6): equity curve, trade blotter, P&L, max drawdown, hit rate, turnover, and the manifest. THIS FILE IS CREATED HERE; M3.10 and M6.6 UPDATE it.
- `tests/unit/backtest/test_report.py` *(create)* — Report field tests.
- `tests/backtest/test_golden_single.py` *(create)* — Golden-run reproducibility: a fixed config+data fixture reproduces a committed report bit-for-bit (manifest git_commit/lib_versions stripped before compare; config_hash/data_hash KEPT and regenerated in-CI).
- `tests/backtest/golden/report_single.json` *(create)* — Committed golden report fixture, regenerated by a documented helper in the CI image.

**Libraries:** —

**Details.** Report is JSON-first (HTML templating added in M6.6). The golden test guards against accidental lookahead/non-determinism regressions: it compares the produced report to a committed golden, stripping environment-sensitive manifest fields (git commit, lib versions) but keeping config_hash/data_hash (which M2.9 made portable). A regen helper documents how to refresh the golden in the same CI image.

**Validation — unit tests:**

- tests/unit/backtest/test_report.py::test_report_fields asserts equity curve, blotter, drawdown, hit_rate present
- tests/backtest/test_golden_single.py::test_matches_committed_golden asserts two runs are byte-identical and equal the committed golden (after stripping git/lib fields)

**Validation — manual:**

- Run: `uv run pytest tests/unit/backtest/test_report.py tests/backtest/test_golden_single.py -q` — expected: green; golden reproduces

**Deliverable.** A JSON backtest report module (owned here) plus a committed golden-run regression proving bit-for-bit reproducibility.

**Depends on:** M2.8, M2.9

#### M2.11 — `trader backtest` CLI (single strategy)

**Goal.** Wire the engine, data, SimBroker, portfolio, manifest, and report into the backtest CLI so a full single-strategy backtest runs from one command.

**Build (files):**

- `src/trader/app/cli.py` *(update)* — Flesh out `backtest --config --start --end`: load config, wire VirtualClock + HistoricalDataProvider + SimBroker + Portfolio, run the engine for the (first) configured strategy, write report+manifest to an output dir.
- `tests/integration/test_backtest_cli.py` *(create)* — End-to-end CLI backtest over a small cached fixture.

**Libraries:** —

**Details.** End-to-end glue. Uses cached data (M2.4) so it runs offline in CI over a tiny fixture. Output is a timestamped report dir with report.json + manifest.json.

**Validation — unit tests:**

- tests/integration/test_backtest_cli.py::test_backtest_cli_produces_report runs `backtest` over a fixture and asserts a report.json + manifest.json are written with non-empty trades/equity

**Validation — manual:**

- Run: `uv run pytest tests/integration/test_backtest_cli.py -q` — expected: green
- Run: `trader backtest --config config/default.yaml --start 2023-01-01 --end 2023-03-31` over previously-fetched data — expected: a report dir is written; re-running reproduces identical report.json

**Deliverable.** A one-command single-strategy backtest producing a reproducible report + manifest.

**Depends on:** M2.10


## M3 — Multi-strategy + scheduler

> **Intent.** Wire up the full multi-strategy dispatch path that runs identically in backtest and live `paper` mode. Build the seeded jitter module, an XNYS trading-calendar wrapper, merged time-sorted slot/trigger generation with stable tie-break, a durable fired-slot ledger, a StrategyRegistry + bindings loader feeding two stub strategies (threshold, zscore_revert), Decision-to-Order sizing, a `run_cycle` orchestrator serialized by a single global cycle lock with per-strategy attribution, the multi-strategy backtest interleave, and an APScheduler daemon that fires per-(strategy,slot) jobs through the calendar+jitter+ledger into a paper-mode `run_cycle`. SAFETY: everything stays read-only or paper (SimBroker/FakeBroker) — no real orders before M5.
>
> **Prerequisites:** M0, M2 (reuses backtest/engine.py + backtest/report.py, SimBroker, the migration runner, and FakeBroker/FakeClock test doubles).
> **New libraries:** `exchange_calendars`, `apscheduler<4`, `numpy`
>
> **Exit criteria.** Two strategies (threshold + zscore_revert) configured on different per-strategy schedules run successfully in BOTH the multi-strategy backtest interleave AND live `paper` cycles, each dispatched to the correct strategy by strategy_id; the seeded jitter is reproducible per (seed,date,strategy_id,slot_id); the XNYS calendar wrapper correctly gates sessions/half-days with clamp/skip and DST-stable open/close; merged triggers are time-sorted with the stable (fire_ts, strategy_id, slot_id) tie-break; the fired-slot ledger enforces exactly-once per (date,strategy_id,slot_id) across crashes/double-scheduling; overlapping fires serialize through the single global cycle lock; per-strategy attribution appears in the audit log and backtest report; and a strategy exception is isolated without crashing the daemon. All validated by unit + integration tests with injected fakes (FakeBroker/SimBroker, fake Clock, respx-style HTTP avoided since M3 is local) and NO real-money side effects (SimBroker/FakeBroker only — real orders deferred to M5).

*12 sub-steps.*

#### M3.1 — Strategy/sizing/scheduler core types and protocols

**Goal.** Add the remaining M3-specific dataclasses and protocols (StrategyBinding, SlotSpec, TriggerSlot, Scheduler protocol, Strategy protocol if not already final) into core/ so every later step has stable, importable types and no module needs to redeclare them.

**Build (files):**

- `src/trader/core/types.py` *(update)* — Add/confirm frozen dataclasses: SlotSpec(slot_id:str, time:str 'HH:MM', drift_max_minutes:int, drift_direction:Literal['forward','symmetric','backward']='forward', distribution:Literal['uniform','truncnorm','triangular']='uniform', on_overshoot:Literal['clamp','skip']='clamp', catch_up:Optional[bool]=None); StrategyBinding(strategy_id, strategy_name, params:dict, universe:tuple[str,...], slots:tuple[SlotSpec,...], enabled:bool=True, risk_overrides:Optional[dict]=None); TriggerSlot(strategy_id, slot_id, fire_ts:datetime, drift_seconds:int, seed:Optional[int]). Confirm MarketSnapshot, Decision, Order, Position, Account, Quote already exist from M0 (do not duplicate).
- `src/trader/core/protocols.py` *(update)* — Add Scheduler Protocol with triggers_for(self, on_date) -> Sequence[TriggerSlot]; confirm Strategy Protocol decide(snapshot, positions, account, data, clock) -> Sequence[Decision] exists. No logic, signatures only.
- `tests/unit/core/test_m3_types.py` *(create)* — Tests asserting the new dataclasses are frozen (assigning raises FrozenInstanceError), defaults are correct (SlotSpec.drift_direction=='forward', on_overshoot=='clamp'), and tuples are used for universe/slots so instances are hashable.

**Libraries:** —

**Details.** Use the EXACT field names/signatures from design §5. Keep these as plain frozen dataclasses (no pydantic) — config-layer validation lives in M3.7. Use tuples (not lists) for universe/slots so StrategyBinding and TriggerSlot are hashable and safe as dict keys in tests. If M0 already defined Strategy/MarketSnapshot/Decision/Order, only ADD the missing SlotSpec/StrategyBinding/TriggerSlot/Scheduler — do not redefine. Edge case: SlotSpec.catch_up Optional so a slot can override the global schedule.catch_up.

**Validation — unit tests:**

- tests/unit/core/test_m3_types.py::test_slotspec_defaults asserts drift_direction=='forward' and on_overshoot=='clamp'
- tests/unit/core/test_m3_types.py::test_dataclasses_frozen asserts mutating a TriggerSlot/StrategyBinding raises dataclasses.FrozenInstanceError
- tests/unit/core/test_m3_types.py::test_binding_hashable asserts hash(StrategyBinding(...)) does not raise

**Validation — manual:**

- Run `python -c "from trader.core.types import SlotSpec, StrategyBinding, TriggerSlot; print('ok')"` and observe it prints ok with no ImportError

**Deliverable.** Importable, frozen, hashable core types and the Scheduler protocol that all M3 modules depend on.

**Depends on:** —

#### M3.2 — Seeded jitter module

**Goal.** Deterministic, isolated per-slot drift derivation keyed by (base_seed, date, strategy_id, slot_id), never touching the global RNG — the single jitter code used identically in live and backtest.

**Build (files):**

- `src/trader/scheduler/jitter.py` *(create)* — Functions: stable_seed(base_seed:Optional[int], slot_date:date, strategy_id:str, slot_id:str)->int using hashlib.blake2b over the joined string (base_seed or 'ENTROPY', slot_date.isoformat(), strategy_id, slot_id) -> int from first 8 bytes; when base_seed is None, return a fresh OS-entropy seed via secrets.randbits(64) (live unpredictability). compute_drift(slot:SlotSpec, base_seed, slot_date, strategy_id)->tuple[int,int] returning (drift_seconds, seed): build numpy.random.default_rng(seed); pick lo/hi from drift_direction (forward [0,max], symmetric [-max,max], backward [-max,0]) where max=drift_max_minutes*60; sample per distribution (uniform=rng.uniform; truncnorm/triangular optional, default uniform); round to int seconds; clamp into [lo,hi].
- `tests/unit/scheduler/test_jitter.py` *(create)* — Reproducibility + bounds + direction + independence tests.

**Libraries:** `numpy`

**Details.** CRITICAL design rules (§7.2, Appendix C): never use numpy/random global state — always construct a dedicated Generator from the derived seed. stable_seed MUST include strategy_id and slot_id so each strategy's drift is independent and (in backtest) reproducible. For live (base_seed=None) draw fresh entropy so two days differ. Determinism note (§9.5): blake2b gives a stable hash across processes (Python's built-in hash() is salted — DO NOT use it). Return both drift_seconds and the concrete seed so the realized seed can be persisted per trigger for replay.

**Validation — unit tests:**

- tests/unit/scheduler/test_jitter.py::test_reproducible_with_seed asserts compute_drift(slot, base_seed=42, date, 'momentum') returns identical drift across two calls AND across re-import (stable_seed deterministic)
- tests/unit/scheduler/test_jitter.py::test_forward_bounds asserts 0<=drift<=drift_max_minutes*60 for direction='forward' over 200 sampled seeds
- tests/unit/scheduler/test_jitter.py::test_symmetric_bounds asserts -max<=drift<=max for direction='symmetric'
- tests/unit/scheduler/test_jitter.py::test_strategy_independence asserts compute_drift differs between strategy_id 'momentum' vs 'meanrev' for same (seed,date,slot_id) with high probability
- tests/unit/scheduler/test_jitter.py::test_entropy_when_seed_none asserts two compute_drift calls with base_seed=None usually differ (uses real entropy, not asserted deterministic)
- ⚙ *(added in plan review)* test_jitter.py::test_entropy_wiring_deterministic — monkeypatch secrets.randbits to two known values and assert base_seed=None uses it (test the wiring, not probability — the 'usually differ' test is flaky)

**Validation — manual:**

- Run `pytest tests/unit/scheduler/test_jitter.py -q` and observe all tests pass with no global-RNG usage

**Deliverable.** A pure, seeded, isolated jitter module producing reproducible bounded drift per (seed,date,strategy,slot).

**Depends on:** M3.1

#### M3.3 — Trading-calendar wrapper (XNYS)

**Goal.** A thin wrapper over exchange_calendars XNYS exposing sessions, open/close times (incl. half-days), is-open checks, and a clamp-or-skip helper for drifted fire times, with DST correctness and a manual holiday-override hook.

**Build (files):**

- `src/trader/scheduler/calendar.py` *(create)* — TradingCalendar(code='XNYS', tz='America/New_York', extra_closures:frozenset[date]=frozenset()). Methods: is_session(d:date)->bool (False for weekends/holidays/extra_closures); sessions(start:date,end:date)->list[date]; session_open(d)->datetime, session_close(d)->datetime (tz-aware ET, half-days return early close); is_open(at:datetime)->bool (open<=at<=close on a session); resolve_fire(fire_ts:datetime, slot:SlotSpec)->Optional[datetime] applying gating: not a session -> None (skip); fire_ts>close -> on_overshoot 'clamp' -> close-epsilon (epsilon=1s) else None; fire_ts<open (symmetric/backward early edge) -> max(open+epsilon, fire_ts).
- `tests/unit/scheduler/test_calendar.py` *(create)* — Session/half-day/clamp/skip/DST tests using fixed historical dates.

**Libraries:** `exchange_calendars`

**Details.** Wrap exchange_calendars.get_calendar('XNYS'). Convert library UTC timestamps to ET via zoneinfo. Half-day example: 2024-07-03 (early close 13:00 ET) and 2024-11-29 (13:00 ET) — assert session_close returns 13:00 ET. DST: 2024-03-10 (spring forward) and 2024-11-03 (fall back) — assert daytime session open/close land at 09:30/16:00 ET despite the offset change (equity slots sit clear of 01:00-03:00). extra_closures supports ad-hoc closures the library lags on (design §7.3). resolve_fire is the single gate used by both backtest and live. Edge: a forward-drifted 15:30 slot on a 13:00 half-day overshoots -> clamp to 12:59:59 ET. Keep the wrapper deterministic (no now() reads).

**Validation — unit tests:**

- tests/unit/scheduler/test_calendar.py::test_weekend_holiday_not_session asserts is_session(Sat) and is_session(2024-12-25) are False
- tests/unit/scheduler/test_calendar.py::test_extra_closure asserts a date in extra_closures is not a session
- tests/unit/scheduler/test_calendar.py::test_half_day_close asserts session_close(2024-07-03) == 13:00 ET
- tests/unit/scheduler/test_calendar.py::test_resolve_fire_clamps_overshoot asserts a 15:30+drift fire on 2024-07-03 clamps to 12:59:59 ET
- tests/unit/scheduler/test_calendar.py::test_resolve_fire_skip_on_closed asserts resolve_fire on a holiday returns None
- tests/unit/scheduler/test_calendar.py::test_dst_open_close_stable asserts open==09:30 ET, close==16:00 ET on 2024-03-11 and 2024-11-04
- ⚙ *(added in plan review)* test_calendar.py::test_resolve_fire_exactly_at_close and ::test_resolve_fire_exactly_at_open (pin edge inclusivity)
- ⚙ *(added in plan review)* test_calendar.py::test_resolve_fire_before_open_clamps (symmetric/backward early edge)
- ⚙ *(added in plan review)* test_calendar.py::test_localize_handles_dst_gap_and_fold (feed a spring-forward gap / fall-back fold local time; assert a well-defined UTC instant)

**Validation — manual:**

- Run `pytest tests/unit/scheduler/test_calendar.py -q` and confirm all calendar/half-day/DST tests pass

**Deliverable.** A deterministic XNYS calendar wrapper with the resolve_fire clamp/skip gate shared by backtest and live.

**Depends on:** M3.1

#### M3.4 — Slot/trigger generation (merged, time-sorted, stable tie-break)

**Goal.** Combine all enabled bindings' slots into one chronologically sorted, calendar-gated, jittered list of TriggerSlots for a date — the Scheduler.triggers_for implementation used identically live and in backtest.

**Build (files):**

- `src/trader/scheduler/triggers.py` *(create)* — SlotScheduler(bindings:Sequence[StrategyBinding], calendar:TradingCalendar, tz, base_seed:Optional[int]) implementing the Scheduler protocol. triggers_for(on_date:date)->list[TriggerSlot]: if not calendar.is_session(on_date) return []; for each enabled binding, for each slot: nominal = localize(on_date, slot.time, tz); drift_seconds, seed = compute_drift(...); fire = nominal + timedelta(seconds=drift); resolved = calendar.resolve_fire(fire, slot); if resolved is None skip (record reason); else append TriggerSlot(strategy_id, slot_id, resolved, drift_seconds, seed). Sort by stable key (fire_ts, strategy_id, slot_id).
- `tests/unit/scheduler/test_triggers.py` *(create)* — Merge/sort/tie-break/skip-disabled/empty-on-holiday tests with a deterministic seed and a fake/real calendar.

**Libraries:** —

**Details.** Implements the merged interleave from design §4.3 and Appendix C #3. Stable tie-break key is EXACTLY (fire_ts, strategy_id, slot_id) so identical fire_ts across strategies is deterministically ordered in backtest and well-defined live. Disabled bindings (enabled=False) contribute zero triggers. Localize using zoneinfo (datetime.combine(date, parsed_time, tzinfo=ZoneInfo(tz))) — avoid naive arithmetic across DST. Reuse compute_drift (M3.2) and resolve_fire (M3.3) so live/backtest parity is structural. Keep a parallel list of skipped (strategy_id, slot_id, reason) for alerting later. This module is pure given (bindings, calendar, seed, date) -> fully reproducible with a fixed base_seed.

**Validation — unit tests:**

- tests/unit/scheduler/test_triggers.py::test_merged_sorted asserts two strategies' slots come back sorted by fire_ts ascending
- tests/unit/scheduler/test_triggers.py::test_stable_tiebreak asserts when two TriggerSlots share fire_ts they are ordered by (strategy_id, slot_id)
- tests/unit/scheduler/test_triggers.py::test_disabled_skipped asserts an enabled=False binding yields no triggers
- tests/unit/scheduler/test_triggers.py::test_holiday_empty asserts triggers_for(2024-12-25) == []
- tests/unit/scheduler/test_triggers.py::test_reproducible asserts triggers_for with base_seed fixed yields identical fire_ts list across two calls

**Validation — manual:**

- Run `pytest tests/unit/scheduler/test_triggers.py -q`; all pass
- Run `python -c "from trader.scheduler.triggers import SlotScheduler; print('ok')"` -> ok

**Deliverable.** A reproducible Scheduler.triggers_for producing merged, sorted, calendar-gated, jittered TriggerSlots.

**Depends on:** M3.2, M3.3

#### M3.5 — Fired-slot ledger (claim/do/done, exactly-once)

**Goal.** Durable SQLite ledger keyed (slot_date, strategy_id, slot_id) UNIQUE giving exactly-once per strategy/slot/day surviving crashes and double-scheduling.

**Build (files):**

- `src/trader/state/migrations/00X_fired_slot_ledger.sql` *(create)* — CREATE TABLE fired_slot (slot_date TEXT NOT NULL, strategy_id TEXT NOT NULL, slot_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('claimed','done','failed')), planned_fire_ts TEXT, drift_seconds INTEGER, seed INTEGER, claimed_at TEXT, finished_at TEXT, error TEXT, UNIQUE(slot_date, strategy_id, slot_id));
- `src/trader/state/ledger.py` *(create)* — FiredSlotLedger(conn) repository: claim(slot_date, strategy_id, slot_id, planned_fire_ts, drift_seconds, seed)->bool (BEGIN IMMEDIATE; INSERT ... status='claimed'; on sqlite3.IntegrityError return False = already fired); mark_done(...); mark_failed(..., error); was_fired(slot_date, strategy_id, slot_id)->Optional[str] status.
- `tests/unit/state/test_ledger.py` *(create)* — Exactly-once + idempotent-claim + status-transition tests on an in-memory/temp SQLite.

**Libraries:** —

**Details.** Implements design §7.5 / §12 exactly-once. Use INSERT under BEGIN IMMEDIATE and rely on the UNIQUE constraint as the real guarantee (not the scheduler). claim returns False when the row already exists -> caller aborts (no double fire). Store planned_fire_ts as ISO UTC string; persist realized drift_seconds + seed for replay (§7.5). Use the M0 SQLite connection/migration runner (WAL mode). Tests use a tmp_path sqlite file (not :memory: if testing WAL) and assert a second claim of the same key returns False even after a simulated crash (new connection). This is read-only/paper-safe: the ledger touches no broker.

**Validation — unit tests:**

- tests/unit/state/test_ledger.py::test_first_claim_succeeds asserts claim(...) returns True and was_fired returns 'claimed'
- tests/unit/state/test_ledger.py::test_double_claim_blocked asserts a second claim of the same (date,strategy,slot) returns False
- tests/unit/state/test_ledger.py::test_claim_survives_reconnect asserts re-opening the DB and re-claiming still returns False
- tests/unit/state/test_ledger.py::test_mark_done_and_failed asserts status transitions to 'done'/'failed' and error is recorded
- tests/unit/state/test_ledger.py::test_independent_strategies asserts same slot_id under different strategy_id both claim True
- ⚙ *(added in plan review)* test_ledger.py::test_orphaned_claimed_slot_recovery — define & test the policy for a row stuck in 'claimed' after a crash-mid-cycle (either blocked+alerted, or a recovery sweep re-opens stale claims past a grace window)

**Validation — manual:**

- Run `pytest tests/unit/state/test_ledger.py -q`; all pass
- Run the migration against a tmp DB and `sqlite3 tmp.db '.schema fired_slot'` shows the UNIQUE(slot_date,strategy_id,slot_id) constraint

**Deliverable.** A crash-safe fired-slot ledger enforcing exactly-once per (date,strategy,slot).

**Depends on:** M3.1

> ⚙ **Plan-review note.** The exactly-once guarantee must cover CRASH-DURING-cycle, not just duplicate-claim: specify and test how orphaned 'claimed' rows are handled on restart.

#### M3.6 — StrategyRegistry + two stub strategies

**Goal.** A name->class registry so no strategy logic is hardcoded in the engine, plus the two stub strategies (threshold, zscore_revert) implementing the pure Strategy protocol.

**Build (files):**

- `src/trader/strategy/registry.py` *(create)* — StrategyRegistry with register(name)(cls) decorator and create(name, params)->Strategy (constructs cls(**params)); get(name)->type; raises KeyError with available names on miss. A module-level default REGISTRY plus a function to register built-ins.
- `src/trader/strategy/strategies/threshold.py` *(create)* — ThresholdStrategy(band:float=0.02, lot:int=10) registered as 'threshold'; decide(snapshot, positions, account, data, clock) per design §6: for each (sym,q) in snapshot.quotes, skip if prev_close is None; BUY lot if last < prev_close*(1-band) rationale='dip'; SELL lot if last > prev_close*(1+band) rationale='pop'; else HOLD 0.
- `src/trader/strategy/strategies/zscore_revert.py` *(create)* — ZScoreRevertStrategy(lookback:int=20, z_entry:float=2.0) registered as 'zscore_revert'; decide pulls trailing `lookback` daily bars via data.get_bars(sym, ..., asof=snapshot.asof) (asof-bound, no lookahead), computes mean/std of closes, z=(last-mean)/std; BUY lot when z<=-z_entry (oversold), SELL when z>=z_entry; HOLD if insufficient bars or std==0.
- `tests/unit/strategy/test_strategies.py` *(create)* — Registry + both strategies' deterministic-signal tests on synthetic snapshots/bars.

**Libraries:** —

**Details.** Strategies are PURE (design boundary rule 1): read only injected snapshot/positions/account/data/clock; never datetime.now() or network. Use Decimal for price math (Decimal(str(1-band))) to match the Quote types. zscore_revert MUST fetch history only via the injected asof-bound MarketDataProvider so it inherits no-lookahead (Appendix B). Tests inject a FakeMarketData returning handcrafted bars and assert exact decisions: e.g. threshold with band=0.02, prev_close=100, last=97 -> BUY; last=103 -> SELL; last=100 -> HOLD; missing prev_close -> skipped. zscore: lookback bars all at 100 then last=90 with std>0 -> z<=-2 -> BUY; std==0 -> HOLD (no div-by-zero). Registry.create('threshold',{'band':0.01}) returns a ThresholdStrategy with band 0.01.

**Validation — unit tests:**

- tests/unit/strategy/test_strategies.py::test_registry_create asserts create('threshold',{'lot':5}).lot==5 and unknown name raises KeyError
- tests/unit/strategy/test_strategies.py::test_threshold_buy_dip asserts BUY emitted when last<prev_close*(1-band)
- tests/unit/strategy/test_strategies.py::test_threshold_sell_pop asserts SELL when last>prev_close*(1+band)
- tests/unit/strategy/test_strategies.py::test_threshold_skips_missing_prev_close asserts no decision for a quote with prev_close None
- tests/unit/strategy/test_strategies.py::test_zscore_buy_oversold asserts BUY when z<=-z_entry given fake bars
- tests/unit/strategy/test_strategies.py::test_zscore_zero_std_holds asserts HOLD (no exception) when std==0 or bars insufficient

**Validation — manual:**

- Run `pytest tests/unit/strategy/test_strategies.py -q`; all pass

**Deliverable.** A StrategyRegistry and two pure, asof-safe stub strategies wired to it.

**Depends on:** M3.1

#### M3.7 — Strategy bindings loader (config -> StrategyBinding list)

**Goal.** Parse and validate the `schedule` + `strategies` config blocks into pydantic-validated StrategyBinding objects, inheriting global schedule defaults per binding/slot.

**Build (files):**

- `src/trader/strategy/bindings.py` *(create)* — Pydantic models: ScheduleConfig(timezone, market_calendar='XNYS', base_seed:Optional[int]=None, catch_up:bool=False, misfire_grace_seconds:int=120); SlotConfig(id, time:str validated 'HH:MM', drift_max_minutes:int<=ceiling, drift_direction, distribution, on_overshoot, catch_up:Optional[bool]=None); StrategyBindingConfig(id, name, enabled=True, params:dict={}, universe:list[str], slots:list[SlotConfig], risk_overrides:Optional[dict]=None). Function load_bindings(cfg)->tuple[ScheduleConfig, list[StrategyBinding]] mapping configs to the core StrategyBinding/SlotSpec frozen types, applying schedule.catch_up as the slot default when slot.catch_up is None, validating strategy names exist in the registry, and rejecting duplicate strategy ids.
- `tests/unit/strategy/test_bindings.py` *(create)* — Validation + inheritance + unknown-name + duplicate-id tests using the §11 example YAML snippet.

**Libraries:** —

**Details.** Design §7.1/§11: each (strategy_id, slot) inherits global schedule defaults. Enforce drift_max_minutes <= a hard ceiling (e.g. 60) per §7.2/§11 (`drift_max_minutes <= ceiling`). Validate time matches ^\d{2}:\d{2}$ and is a real time. Reject duplicate strategy ids (dispatch key collision) and names not present in the registry (resolved via M3.6 registry). Convert lists to tuples when building the frozen core types so bindings stay hashable. The same validated bindings feed both the backtest interleave (M3.10) and the live daemon (M3.11), so this is the single config->binding boundary.

**Validation — unit tests:**

- tests/unit/strategy/test_bindings.py::test_loads_two_bindings asserts the §11 example yields bindings 'momentum'(threshold) and 'meanrev'(zscore_revert) with correct universes
- tests/unit/strategy/test_bindings.py::test_drift_ceiling_rejected asserts drift_max_minutes>ceiling raises ValidationError
- tests/unit/strategy/test_bindings.py::test_unknown_strategy_name_rejected asserts an unregistered name raises
- tests/unit/strategy/test_bindings.py::test_duplicate_id_rejected asserts two bindings with id 'momentum' raise
- tests/unit/strategy/test_bindings.py::test_slot_inherits_global_catch_up asserts a slot with catch_up None inherits schedule.catch_up

**Validation — manual:**

- Run `pytest tests/unit/strategy/test_bindings.py -q`; all pass

**Deliverable.** A validated config->StrategyBinding loader feeding scheduler and orchestrator.

**Depends on:** M3.1, M3.6

#### M3.8 — Sizing (Decision -> Order)

**Goal.** Convert a strategy Decision into a concrete Order with a pre-generated client_order_id and strategy_id attribution, ready for the (future) risk gate and broker.

**Build (files):**

- `src/trader/sizing/sizer.py` *(create)* — size_decision(decision:Decision, strategy_id:str, exec_cfg, clock)->Optional[Order]: HOLD or quantity<=0 -> None; map action BUY/SELL -> Side; order_type from exec_cfg.order_type (market|limit); limit_price=decision.limit_price when LIMIT (else None); client_order_id=uuid4 hex; tif='DAY'. Returns frozen Order with strategy_id set.
- `tests/unit/sizing/test_sizer.py` *(create)* — HOLD->None, BUY/SELL mapping, client_order_id uniqueness, limit pass-through tests.

**Libraries:** —

**Details.** Design §4.2/§5: client_order_id generated & persisted BEFORE submit (idempotency seed) — here we generate it; persistence is wired in the orchestrator (M3.9) and real submit in M5. Keep sizing intentionally thin in M3 (the design lets strategy express simple share-delta intent; sophisticated equity-% sizing can extend later). DO NOT clamp/limit here — that is the risk gate's job (M5). Inject a uuid factory or accept a rng/uuid arg so tests can assert determinism if needed; default uuid4. strategy_id is threaded through for attribution (design §10).

**Validation — unit tests:**

- tests/unit/sizing/test_sizer.py::test_hold_returns_none asserts size_decision(Decision('HOLD',...)) is None
- tests/unit/sizing/test_sizer.py::test_buy_maps_side asserts BUY decision -> Order.side=='BUY', quantity preserved, strategy_id set
- tests/unit/sizing/test_sizer.py::test_limit_passthrough asserts order_type LIMIT carries decision.limit_price
- tests/unit/sizing/test_sizer.py::test_client_order_id_unique asserts two sized orders get distinct client_order_ids

**Validation — manual:**

- Run `pytest tests/unit/sizing/test_sizer.py -q`; all pass

**Deliverable.** A sizing function turning Decisions into attributed, idempotency-ready Orders.

**Depends on:** M3.1

#### M3.9 — Orchestrator run_cycle + global cycle lock + attribution

**Goal.** The shared decision->sizing->broker cycle used identically by backtest and live, serialized by one global cycle lock, attributing fills to strategy_id and writing the audit chain.

**Build (files):**

- `src/trader/orchestrator/cycle.py` *(create)* — Orchestrator(broker, data, clock, sizer, audit, attribution, cycle_lock, risk=None). run_cycle(strategy:Strategy, universe:Sequence[str], strategy_id:str, now:datetime)->CycleResult: ACQUIRE cycle_lock (with-block); build MarketSnapshot from data.get_quote(sym, asof=now) for sym in universe (skip/flag missing); get positions=broker.get_positions(), account=broker.get_account(); decisions=strategy.decide(snapshot, positions, account, data, clock); for each decision: order=sizer(decision, strategy_id, ...); if order: persist intent 'pending' (write-ahead), optional risk.check passthrough (no-op in M3, returns approve), broker_id=broker.submit_order(order), fill=poll broker.get_order(...), attribution.apply(fill, strategy_id), write audit row; RELEASE lock. Catch per-strategy exceptions -> log/alert/mark cycle failed, never propagate (Appendix C #6). Returns CycleResult(strategy_id, decisions, orders, fills, errors).
- `src/trader/orchestrator/lock.py` *(create)* — GlobalCycleLock: a threading.RLock-backed context manager with acquire(timeout)->bool; provide an asyncio variant or a simple blocking lock with a grace timeout per §7.5. A NullLock/test double for single-threaded tests.
- `src/trader/state/attribution.py` *(create)* — AttributionLedger(conn): apply(fill, strategy_id) upserts a per-strategy attributed sub-position (symbol, strategy_id, signed qty, avg_price); get_attributed(strategy_id)->positions; reconcile_total(broker_positions) returns any unattributed delta parked in 'unknown'. Migration for attributed_position table.
- `tests/unit/orchestrator/test_cycle.py` *(create)* — Cycle with FakeBroker: decisions->orders->fills, attribution per strategy, lock serialization, strategy-exception isolation.

**Libraries:** —

**Details.** Design §4.2 / §7.5 / Appendix C. SAFETY: M3 uses FakeBroker (tests) or SimBroker (paper) ONLY — no SchwabBroker. The risk gate is M5; here run_cycle calls an injected risk that defaults to approve-all (single chokepoint already structurally present so M5 just swaps the impl). The single GLOBAL cycle lock serializes the whole decision->sizing->submit critical section so account state is read-modify-written atomically for one strategy at a time (two overlapping fires must NOT both see stale balances). Attribution writes per-strategy sub-positions tagged by strategy_id (design §10 #16 strictly-separate sub-positions). Persist a cycle/correlation id on every audit row. Exception in one strategy's cycle is caught, recorded as failed, alerted — must not crash the daemon or block other strategies. Use FakeBroker (from M0/integration tests) returning canned fills; inject a fake clock. Lock serialization test: run two run_cycle calls on threads sharing one lock and assert their critical sections do not interleave (e.g. via a recorded enter/exit order or a shared counter that would race without the lock).

**Validation — unit tests:**

- tests/unit/orchestrator/test_cycle.py::test_decisions_to_fills asserts a threshold BUY decision becomes an Order submitted to FakeBroker and a Fill recorded
- tests/unit/orchestrator/test_cycle.py::test_hold_no_order asserts HOLD decisions produce no broker calls
- tests/unit/orchestrator/test_cycle.py::test_attribution_per_strategy asserts a fill is attributed to the run's strategy_id in attribution ledger
- tests/unit/orchestrator/test_cycle.py::test_lock_serializes asserts two concurrent run_cycle calls under one GlobalCycleLock do not overlap their critical sections
- tests/unit/orchestrator/test_cycle.py::test_strategy_exception_isolated asserts a strategy raising inside decide() yields CycleResult with errors and does not propagate
- ⚙ *(added in plan review)* test_cycle.py::test_lock_serializes_deterministic — inject an instrumented lock recording acquire/release; assert strictly nested ordering (no interleave) instead of a flaky real-thread race
- ⚙ *(added in plan review)* test_cycle.py::test_every_order_passes_risk_check (spy) — risk.check called exactly once per order before submit; a reject prevents submit (test the chokepoint invariant even though M3's stub approves all)
- ⚙ *(added in plan review)* test_cycle.py::test_client_order_id_persisted_before_submit (spy ordering) — 'pending' written with the client_order_id BEFORE broker.submit_order
- ⚙ *(added in plan review)* test_attribution.py::test_reconcile_parks_unattributed_delta — broker total > attributed sum parks the delta in 'unknown' + flags

**Validation — manual:**

- Run `pytest tests/unit/orchestrator/test_cycle.py -q`; all pass with FakeBroker (no network, no real orders)

**Deliverable.** A lock-serialized, attribution-aware run_cycle shared by backtest and live, safe with FakeBroker/SimBroker only.

**Depends on:** M3.5, M3.6, M3.8

> ⚙ **Plan-review note.** SPLIT WHEN BUILDING into 3 baby steps — M3.9a GlobalCycleLock + NullLock (orchestrator/lock.py); M3.9b AttributionLedger + attributed_position migration (state/attribution.py); M3.9c Orchestrator.run_cycle wiring (snapshot→decide→sizer→risk-passthrough→broker→attribution→audit, with per-strategy exception isolation).

#### M3.10 — Backtest engine extended to multi-strategy merged interleave + per-strategy attribution

**Goal.** Walk the merged, time-sorted triggers across all enabled strategies on the VirtualClock, running the SAME run_cycle per trigger against SimBroker, producing per-strategy and combined attribution/report.

**Build (files):**

- `src/trader/backtest/engine.py` *(update)* — Extend the M2 event loop: for each session in calendar.sessions(start,end): triggers = SlotScheduler(bindings, calendar, tz, base_seed).triggers_for(date); for (fire_ts, strat_id, slot_id) in triggers (already sorted): virtual_clock.advance_to(fire_ts); binding=bindings_by_id[strat_id]; strategy=registry.create(binding.strategy_name, binding.params); orchestrator.run_cycle(strategy, binding.universe, strat_id, now=fire_ts). HistoricalDataProvider stays asof-bound (ts<=fire_ts). Collect per-strategy CycleResults.
- `src/trader/backtest/report.py` *(update)* — Add per-strategy attribution to the report: per-strategy trade blotter, P&L, and a combined equity curve alongside the per-strategy ones (design §9.6), plus the run manifest from M2 extended with the bindings/seed snapshot.
- `tests/backtest/test_multi_strategy_interleave.py` *(create)* — Two-strategy interleave + attribution + reproducibility tests on cached/synthetic Parquet history.

**Libraries:** —

**Details.** Design §4.3 / §9. The ONLY difference from live is the three injected impls (VirtualClock, HistoricalDataProvider, SimBroker) — the merged-trigger walk reuses M3.4 triggers_for and M3.9 run_cycle verbatim (parity). Two strategies on different schedules (e.g. momentum 09:45/12:30/15:30, meanrev 10:15/14:00) must interleave in fire-time order across the day. Per-strategy attribution comes from the attribution ledger (M3.9). Determinism: with a fixed base_seed the trigger list and thus the whole run is reproducible (extends the M2 golden-run guarantee to multi-strategy). Use a small synthetic 2-3 day Parquet fixture so the test is fast and offline. Assert no-lookahead survives (data only returns ts<=fire_ts).

**Validation — unit tests:**

- tests/backtest/test_multi_strategy_interleave.py::test_two_strategies_interleaved asserts run_cycle is invoked for both strategy_ids in fire-time order across a session
- tests/backtest/test_multi_strategy_interleave.py::test_per_strategy_attribution asserts the report has separate blotters/P&L per strategy_id and a combined curve
- tests/backtest/test_multi_strategy_interleave.py::test_reproducible_run asserts two runs with the same base_seed produce identical trigger fire_ts sequence and identical combined equity curve
- tests/backtest/test_multi_strategy_interleave.py::test_no_lookahead asserts SimBroker fills use next-quote/bar and data never returns ts>fire_ts

**Validation — manual:**

- Run `pytest tests/backtest/test_multi_strategy_interleave.py -q`; all pass
- Run `python -m trader.app backtest --config config/default.yaml` (with two strategies enabled) and observe a report listing both strategy_ids with per-strategy P&L and a combined equity curve — NO real orders (SimBroker only)

**Deliverable.** A multi-strategy backtest that interleaves merged triggers and reports per-strategy + combined attribution, reproducibly.

**Depends on:** M3.4, M3.7, M3.9

> ⚙ **Plan-review note.** report.py and engine.py are CREATED in M2 (M2.8/M2.10); this step UPDATEs them (add multi-strategy interleave + per-strategy attribution). Declares an implicit dependency on M2. The backtest path intentionally does NOT use the M3.5 fired-slot ledger (exactly-once is a live-daemon concern).

#### M3.11 — Live APScheduler daemon (paper placeholder)

**Goal.** An APScheduler 3.x daemon that registers one job per (strategy_id, slot), each gating on calendar + jitter + ledger and running the SAME run_cycle through the global lock in PAPER mode (SimBroker against live/paper quotes) — no real orders.

**Build (files):**

- `src/trader/scheduler/daemon.py` *(create)* — SchedulerDaemon(bindings, schedule_cfg, calendar, ledger, orchestrator, registry, clock): start() builds a BlockingScheduler (or BackgroundScheduler) with timezone=ZoneInfo(schedule.timezone); for each enabled binding+slot register CronTrigger(hour, minute, timezone=...) with max_instances=1, coalesce=True, misfire_grace_time=schedule.misfire_grace_seconds; callback=_make_fire(strategy_id, slot). _make_fire: compute today's drift via compute_drift, sleep/schedule the realized drift OR re-derive fire_ts and gate via calendar.resolve_fire (skip+alert if None); ledger.claim(today, strategy_id, slot_id, fire_ts, drift, seed) -> if False abort (already fired); else run orchestrator.run_cycle(...) under the global lock; ledger.mark_done/mark_failed. Provide stop() for clean SIGTERM shutdown (finish/abort current cycle, release lock).
- `src/trader/app/cli.py` *(update)* — Wire `trader run` to build paper-mode wiring (SimBroker + live/paper MarketData + RealClock) and start SchedulerDaemon. Assert mode in {paper} for M3 (live is M5); refuse to start with SchwabBroker order path. Add a `--once`/dry tick option for tests.
- `tests/unit/scheduler/test_daemon.py` *(create)* — Job registration, calendar/ledger gating, overlapping-fire serialization, exception isolation — using an injected fake clock and a memory scheduler, no wall-clock sleeps.

**Libraries:** `apscheduler<4`

**Details.** Design §7.4/§7.5/§16.5/Appendix C. SAFETY (pre-M5): wire SimBroker only — the daemon must NOT construct SchwabBroker or any real-order path; assert/refuse mode='live'. APScheduler pinned to 3.x (CronTrigger, misfire_grace_time, coalesce, max_instances=1 per job, persistent jobstore optional). Per-job max_instances=1 is NOT enough for cross-strategy safety, so the actual decision->execute step still runs under the GlobalCycleLock (M3.9) — assert this. Calendar gate + ledger claim happen INSIDE the callback (validate at fire time, day status can change, §7.3). Missed-trigger: catch_up=false default -> if past grace window, skip + alert (do not fire stale). Tests must avoid real time: inject a fake clock and trigger the registered callbacks directly (don't run the blocking loop); assert (a) one job per enabled (strategy,slot), (b) a holiday/closed gate causes skip+no run_cycle, (c) a duplicate fire (ledger already claimed) aborts, (d) two callbacks racing serialize through the lock, (e) a strategy exception marks the slot failed and does not crash the scheduler. Manual validation runs paper only.

**Validation — unit tests:**

- tests/unit/scheduler/test_daemon.py::test_one_job_per_slot asserts the scheduler registers exactly one job per enabled (strategy_id, slot_id) with max_instances=1
- tests/unit/scheduler/test_daemon.py::test_calendar_gate_skips asserts firing on a closed day invokes no run_cycle and emits a skip alert
- tests/unit/scheduler/test_daemon.py::test_ledger_blocks_double_fire asserts a second fire of an already-claimed slot aborts without a second run_cycle
- tests/unit/scheduler/test_daemon.py::test_overlapping_fires_serialize asserts two near-simultaneous callbacks serialize via the global lock
- tests/unit/scheduler/test_daemon.py::test_strategy_exception_does_not_crash asserts a failing cycle marks the slot failed and the scheduler keeps running
- ⚙ *(added in plan review)* test_daemon.py::test_overlapping_callbacks_share_one_lock — assert every registered callback acquires the SAME GlobalCycleLock instance and a held lock blocks/queues per policy (deterministic, not a thread race)
- ⚙ *(added in plan review)* test_daemon.py::test_misfire_past_grace_skips_and_alerts (catch_up=false) and ::test_catch_up_true_fires_within_grace
- ⚙ *(added in plan review)* test_daemon.py::test_daemon_refuses_live_mode and ::test_daemon_uses_simbroker_only (CI-enforce the pre-M5 safety gate, not a manual check)

**Validation — manual:**

- Run `pytest tests/unit/scheduler/test_daemon.py -q`; all pass
- Run `trader run --config config/default.yaml` with mode=paper and two strategies; observe structured logs showing both strategies' jobs registered, calendar/jitter applied, and run_cycle executing against SimBroker — confirm NO real orders are placed (SimBroker fills only) and that starting with mode=live is refused in M3

**Deliverable.** A paper-mode APScheduler daemon dispatching the right strategy per (strategy,slot) with calendar+jitter+ledger gating and lock-serialized cycles — no real-money side effects.

**Depends on:** M3.4, M3.5, M3.7, M3.9

> ⚙ **Plan-review note.** SPLIT WHEN BUILDING into 2 baby steps — M3.11a SchedulerDaemon job registration + callback gating (calendar/jitter/ledger/lock) + SIGTERM stop(); M3.11b CLI `trader run` paper-mode wiring + mode guard + --once dry tick.

#### M3.12 — End-to-end M3 parity + exit-criteria integration test

**Goal.** A single integration test proving the milestone exit: two strategies on different schedules run in BOTH backtest and paper cycles, dispatched correctly, overlapping fires serialize, and per-strategy attribution appears in the audit — the same run_cycle code path in both.

**Build (files):**

- `tests/integration/test_m3_multistrategy_parity.py` *(create)* — Build two bindings (threshold on slots A/B, zscore_revert on slot C) from the §11 example; (1) run the backtest engine over a synthetic 2-day Parquet fixture with a fixed base_seed and capture per-strategy CycleResults + attribution; (2) drive the SchedulerDaemon callbacks (injected fake clock + SimBroker + a HistoricalDataProvider serving the SAME fixture as 'paper' quotes) for the same dates/triggers; assert the dispatched strategy_id per trigger and the resulting attributed positions match between the two paths (parity), that overlapping/equal fire_ts fires serialize via the global lock, and that the audit log contains per-strategy rows.

**Libraries:** —

**Details.** This is the milestone's exit-criteria gate (design §17 M3 row + Appendix C). It must demonstrate parity: the merged-trigger walk (backtest) and the daemon callbacks (paper) both feed the IDENTICAL run_cycle, so for the same fixture + seed the dispatched strategy per trigger and the per-strategy attribution agree. Use a fake clock and the SAME synthetic Parquet for both paths so the only differences are the injected Clock/Broker wiring (SimBroker both sides in test) — proving structural parity. Include a case where two strategies share a drifted fire_ts to assert deterministic (fire_ts, strategy_id, slot_id) tie-break and lock serialization. NO real broker anywhere (paper/sim only) — safe pre-M5. Keep it offline and deterministic (no wall-clock, no network).

**Validation — unit tests:**

- tests/integration/test_m3_multistrategy_parity.py::test_correct_dispatch asserts each trigger ran the strategy named on its TriggerSlot in both paths
- tests/integration/test_m3_multistrategy_parity.py::test_backtest_paper_attribution_parity asserts per-strategy attributed positions match between backtest and paper paths for the same seed+fixture
- tests/integration/test_m3_multistrategy_parity.py::test_overlapping_fires_serialize_and_order asserts equal-fire_ts triggers are ordered by (strategy_id, slot_id) and serialize through the global lock
- tests/integration/test_m3_multistrategy_parity.py::test_audit_has_per_strategy_rows asserts the audit log contains rows tagged with each strategy_id
- ⚙ *(added in plan review)* test_m3_multistrategy_parity.py::test_dispatch_sequence_parity — capture the ordered (strategy_id, slot_id, fire_ts) actually passed to run_cycle in BOTH the backtest-walk and the daemon-callback paths and assert the sequences are identical (proves the daemon code path, not a re-run of the engine)

**Validation — manual:**

- Run `pytest tests/integration/test_m3_multistrategy_parity.py -q`; all pass with SimBroker only (no real orders)
- Run `pytest tests/unit tests/integration tests/backtest -q` and confirm the full M3 suite is green

**Deliverable.** A passing end-to-end test demonstrating the M3 exit criteria: dual-path dispatch parity, serialized overlaps, and per-strategy attribution — all paper-safe.

**Depends on:** M3.10, M3.11

> ⚙ **Plan-review note.** Strengthen parity beyond 'same inputs→same outputs': assert equality at the dispatch layer across the two distinct code paths.


## M4 — Paper trading + Dockerize

> **Intent.** Run the full pipeline in PAPER mode against live quotes (SimBroker fills, no real orders) with the real risk gate, reconciliation, audit trail, alerting, and heartbeat — then package the daemon as a Docker image deployed via docker compose with durable volumes and a healthcheck. This is the dress rehearsal before real money.
>
> **Prerequisites:** M0, M2, M3 (orchestrator run_cycle + global cycle lock + attribution; replaces M3's approve-all risk stub with the real gate).
> **New libraries:** `(docker / docker compose — infra, not pip)`
>
> **Exit criteria.** A multi-day in-container paper soak runs with no manual intervention except the weekly re-auth; state survives container recreation; the risk gate is the single chokepoint for every order; reconciliation, alerting, and heartbeat all work. Still zero real orders (SimBroker).

*10 sub-steps.*

#### M4.1 — Reconciliation engine

**Goal.** Diff broker truth against local intent (account + per-strategy attribution) on startup/after-submit/EOD, truing up to broker reality and flagging unexplained divergence.

**Build (files):**

- `src/trader/execution/__init__.py` *(create)* — Package marker.
- `src/trader/execution/reconcile.py` *(create)* — reconcile(broker, state): pull positions/orders from the Broker; diff vs local intent and per-strategy attribution; update local to broker truth; park unattributed delta in an 'unknown' bucket; return a discrepancy report; on unexplained divergence signal the kill switch (M5).
- `tests/unit/execution/test_reconcile.py` *(create)* — Mismatch handling + unknown-bucket tests with FakeBroker.

**Libraries:** —

**Details.** Broker = source of truth for positions/fills; local = source of truth for intent (§10). Reconciliation runs on startup (before acting), after each submit, and at EOD. Unattributed positions land in 'unknown' and alert; unexplained divergence is a kill-switch trigger (wired in M5).

**Validation — unit tests:**

- tests/unit/execution/test_reconcile.py::test_local_trued_to_broker asserts a broker position absent locally is adopted + flagged
- tests/unit/execution/test_reconcile.py::test_unattributed_delta_parked_in_unknown asserts per-strategy sum < broker total parks the delta in 'unknown'
- tests/unit/execution/test_reconcile.py::test_clean_state_no_discrepancy

**Validation — manual:**

- Run: `uv run pytest tests/unit/execution/test_reconcile.py -q` — expected: green

**Deliverable.** A reconciliation engine that trues local state to broker truth and surfaces divergence.

**Depends on:** M0.7, M0.8

#### M4.2 — Risk rules (individual checks)

**Goal.** Implement each risk rail as a small, independently-tested rule: position/notional/exposure/loss/trade-count limits, allow/deny lists, price sanity, duplicate guard — all fail-closed.

**Build (files):**

- `src/trader/risk/__init__.py` *(create)* — Package marker.
- `src/trader/risk/rules.py` *(create)* — Pure rule functions each (order, ctx)->RuleResult: max_position_size, max_order_notional, max_gross_exposure, daily_loss_limit, max_trades_per_day, allowlist/denylist, price_sanity (zero/neg/NaN, max spread%, staleness, deviation-from-prev-close band), duplicate_order_guard. Evaluate on the RESULTING position, fail closed on missing data.
- `tests/unit/risk/test_rules.py` *(create)* — Reject/clamp + fail-closed per rule, incl. boundaries.

**Libraries:** —

**Details.** Per §10. Each rule is pure and individually tested (the highest-value tests for real money). Evaluate the resulting position (not the order in isolation). Fail closed: missing/stale/uncertain data → reject. Price sanity duplicates the M1 staleness check at the risk layer (defense in depth).

**Validation — unit tests:**

- tests/unit/risk/test_rules.py::test_max_order_notional_rejects_over and ::test_clamp_to_limit
- tests/unit/risk/test_rules.py::test_resulting_position_cap evaluates post-order position
- tests/unit/risk/test_rules.py::test_price_sanity_rejects_zero_negative_nan_wide_spread_and_stale
- tests/unit/risk/test_rules.py::test_daily_loss_limit_halts_new_entries
- tests/unit/risk/test_rules.py::test_denylist_blocks and ::test_allowlist_default_deny
- tests/unit/risk/test_rules.py::test_fail_closed_on_missing_data

**Validation — manual:**

- Run: `uv run pytest tests/unit/risk/test_rules.py -q` — expected: green

**Deliverable.** A complete, individually-tested set of fail-closed risk rules.

**Depends on:** M0.2, M0.7

#### M4.3 — Risk gate (manager + conflict policy)

**Goal.** Compose the rules into the single non-bypassable RiskManager with account-wide + per-strategy scopes and the same-ticker conflict policy (net default).

**Build (files):**

- `src/trader/risk/gate.py` *(create)* — RiskManager implementing the core RiskManager Protocol: check(order, ctx)->RiskVerdict runs per-strategy overrides merged over account-wide limits; resolve_conflicts(decisions, policy) implements net|independent|priority (net default); returns typed approve/clamp/reject with reasons; logs every verdict.
- `tests/unit/risk/test_gate.py` *(create)* — Scope merge + conflict-policy + chokepoint tests.

**Libraries:** —

**Details.** Per §10: account-wide limits are the hard guardrail; per-strategy risk_overrides merge over them; an order must pass both. conflict_policy=net nets same-ticker decisions across strategies before submission and splits fills back pro-rata (attribution in M3.9). The gate is fail-closed and the single chokepoint.

**Validation — unit tests:**

- tests/unit/risk/test_gate.py::test_per_strategy_overrides_merge asserts a stricter per-strategy notional wins
- tests/unit/risk/test_gate.py::test_account_wide_is_hard_cap asserts account limit rejects even if per-strategy allows
- tests/unit/risk/test_gate.py::test_conflict_net_nets_same_ticker asserts +10/-4 across strategies → one +6 order
- tests/unit/risk/test_gate.py::test_reject_returns_typed_verdict_with_reasons

**Validation — manual:**

- Run: `uv run pytest tests/unit/risk/test_gate.py -q` — expected: green

**Deliverable.** The single fail-closed RiskManager with dual-scope limits and conflict netting.

**Depends on:** M4.2

#### M4.4 — Wire the risk gate into the orchestrator

**Goal.** Replace M3's approve-all risk stub with the real RiskManager so every order traverses the gate before the broker.

**Build (files):**

- `src/trader/orchestrator/cycle.py` *(update)* — Inject RiskManager; route every sized order through check() (and resolve_conflicts across a cycle's decisions) before broker.submit_order; on reject, log + skip + (optionally) alert.
- `tests/unit/orchestrator/test_cycle_risk.py` *(create)* — Chokepoint + reject-blocks-submit tests with spy risk + FakeBroker.

**Libraries:** —

**Details.** Enforces §4.1 boundary rule 2 (single chokepoint). The orchestrator structure is unchanged from M3 — only the injected RiskManager swaps from approve-all to real. A spy proves check() is called exactly once per order, before submit, and a reject prevents submit.

**Validation — unit tests:**

- tests/unit/orchestrator/test_cycle_risk.py::test_every_order_passes_risk_check (spy) asserts check() called once per order before submit
- tests/unit/orchestrator/test_cycle_risk.py::test_reject_prevents_submit asserts a rejecting RiskManager yields zero broker.submit calls
- tests/unit/orchestrator/test_cycle_risk.py::test_conflict_netting_applied_before_submit

**Validation — manual:**

- Run: `uv run pytest tests/unit/orchestrator/test_cycle_risk.py -q` — expected: green

**Deliverable.** The orchestrator with the real risk gate as the enforced single chokepoint.

**Depends on:** M4.3

#### M4.5 — Alerting channels (Telegram + email + heartbeat events)

**Goal.** Implement the redundant alert channels and the event taxonomy so failures, kill-switch trips, reconciliation mismatches, and the weekly re-auth reminder are never silent.

**Build (files):**

- `src/trader/observability/alerting.py` *(create)* — Alerter Protocol; TelegramAlerter (httpx POST to bot API), EmailAlerter (stdlib smtplib), MultiAlerter (fan-out, one failure ≠ silent); AlertEvent taxonomy (crash, broker/auth error, kill-switch trip, daily-loss breach, reconcile mismatch, stale-data halt, skipped slot, token re-auth reminder, heartbeat).
- `tests/unit/observability/test_alerting.py` *(create)* — Formatting + dispatch (mocked) + fan-out resilience tests.

**Libraries:** —

**Details.** Per §12. At least two channels so one failing channel isn't silent. TelegramAlerter uses httpx (already a dep from M1); EmailAlerter uses smtplib. MultiAlerter dispatches to all and logs per-channel failure. Credentials come from env (§13). Tests mock the transports (no real sends in CI).

**Validation — unit tests:**

- tests/unit/observability/test_alerting.py::test_telegram_formats_and_posts (respx) asserts a POST to the bot endpoint with the message
- tests/unit/observability/test_alerting.py::test_multialerter_one_channel_fails_others_still_send
- tests/unit/observability/test_alerting.py::test_no_secrets_in_alert_body

**Validation — manual:**

- Run: `uv run pytest tests/unit/observability/test_alerting.py -q` — expected: green
- Manual: set TELEGRAM_* / SMTP_* env and run a one-off `Alerter.alert('test')` — expected: message arrives on both channels

**Deliverable.** Redundant Telegram+email alerting with a typed event taxonomy and fan-out resilience.

**Depends on:** M0.6

#### M4.6 — Heartbeat + healthcheck

**Goal.** Emit a liveness heartbeat each scheduler tick and back the `status --healthcheck` exit code with it, so Docker (and an alert) can detect silent death.

**Build (files):**

- `src/trader/observability/heartbeat.py` *(create)* — Heartbeat: touch(state) writes last_alive_at + scheduler state each tick; is_alive(state, max_age) compares to clock.now(); a missed heartbeat raises an alert.
- `src/trader/app/cli.py` *(update)* — status --healthcheck reads the heartbeat and returns exit 0 if fresh else non-zero (the Docker HEALTHCHECK, §16.1).
- `tests/unit/observability/test_heartbeat.py` *(create)* — Heartbeat freshness + healthcheck exit-code tests.

**Libraries:** —

**Details.** Per §16.1: the HEALTHCHECK calls `trader status --healthcheck` which is fresh-heartbeat→0, stale→non-zero. A stale heartbeat also fires an alert (silent-death detection). Clock-injected so tests are deterministic.

**Validation — unit tests:**

- tests/unit/observability/test_heartbeat.py::test_fresh_heartbeat_healthy asserts is_alive True within max_age
- tests/unit/observability/test_heartbeat.py::test_stale_heartbeat_unhealthy_and_alerts
- tests/unit/observability/test_heartbeat.py::test_healthcheck_exit_codes

**Validation — manual:**

- Run: `uv run pytest tests/unit/observability/test_heartbeat.py -q` — expected: green

**Deliverable.** A heartbeat + healthcheck wiring that makes daemon liveness observable to Docker and alerts.

**Depends on:** M4.5, M0.9

#### M4.7 — Paper pipeline integration (live quotes + SimBroker + risk + reconcile + audit)

**Goal.** Wire the full paper-mode pipeline end-to-end: live quotes (SchwabMarketData) → strategy → risk gate → SimBroker fills → reconciliation → durable audit trail + alerting.

**Build (files):**

- `src/trader/orchestrator/cycle.py` *(update)* — Ensure run_cycle persists the full audit chain (inputs→decision→risk verdict→order→fill) with a correlation id; emit metrics + alerts on events.
- `src/trader/app/cli.py` *(update)* — `run` in paper mode wires RealClock + SchwabMarketData (live quotes) + SimBroker (paper fills) + RiskManager + reconciliation + alerting + heartbeat; refuses mode=live (still pre-M5).
- `tests/integration/test_paper_pipeline.py` *(create)* — Full-cycle integration with FakeMarketData + SimBroker asserting an end-to-end decision→audit row.

**Libraries:** —

**Details.** Paper mode = real live quotes, simulated fills (§10 dry-run default). The whole pipeline including the risk gate and persistence runs exactly as live will, so the audit schema matches. CI uses FakeMarketData; the manual soak (M4.10) uses real live quotes. Still refuses mode=live.

**Validation — unit tests:**

- tests/integration/test_paper_pipeline.py::test_end_to_end_paper_cycle asserts a cycle writes an audit row with the full chain and a SimBroker fill, no real order
- tests/integration/test_paper_pipeline.py::test_run_refuses_live_mode asserts mode=live exits non-zero in M4

**Validation — manual:**

- Run: `uv run pytest tests/integration/test_paper_pipeline.py -q` — expected: green

**Deliverable.** A complete paper-mode trading pipeline (live data, simulated fills) with full audit + risk + reconciliation.

**Depends on:** M4.4, M4.6, M2.5

#### M4.8 — Dockerfile + entrypoint

**Goal.** Package the daemon as a slim, non-root Docker image with tzdata, an exec entrypoint, and a HEALTHCHECK.

**Build (files):**

- `deploy/Dockerfile` *(create)* — FROM python:3.11-slim; install tzdata + build deps; install the package from the lockfile; create a non-root user; COPY src; ENTRYPOINT exec form running the CLI; HEALTHCHECK CMD trader status --healthcheck.
- `deploy/entrypoint.sh` *(create)* — Exec-form entrypoint that runs `trader run` as PID 1 (signal forwarding for clean SIGTERM).
- `deploy/.dockerignore` *(create)* — Exclude state/, data/, secrets/, .git, .venv, caches from the build context.

**Libraries:** —

**Details.** Per §16.1: slim base + tzdata (the daemon needs America/New_York), non-root, exec entrypoint so SIGTERM reaches the daemon for clean shutdown (finish/abort cycle, release lock, flush state), HEALTHCHECK wired to the M4.6 heartbeat. No secrets baked in.

**Validation — unit tests:**

- *(integration/manual milestone — see manual validation)*

**Validation — manual:**

- Run: `docker build -f deploy/Dockerfile -t trader:dev .` — expected: image builds
- Run: `docker run --rm trader:dev status` — expected: prints mode + not authenticated; exits cleanly
- Run: `docker run --rm trader:dev sh -c 'date'` confirms tzdata present; send SIGTERM to a running container and confirm clean shutdown in logs

**Deliverable.** A reproducible, non-root, healthchecked Docker image of the daemon.

**Depends on:** M4.7

#### M4.9 — docker compose + durable volumes

**Goal.** Define the compose service with named volumes (state + data), env_file secrets, restart policy, log rotation, and resource limits, and prove state survives recreation.

**Build (files):**

- `deploy/docker-compose.yml` *(create)* — trader service per §16.2: build/image, restart: unless-stopped, TZ env, env_file ./secrets/.env, volumes config(ro)+trader_state(/state)+trader_data(/data), healthcheck, json-file logging w/ rotation, resource limits; named volumes.
- `deploy/secrets/.env.example` *(create)* — Template for SCHWAB_APP_KEY/SECRET, alert tokens (gitignored real file).
- `tests/unit/deploy/test_compose_config.py` *(create)* — Static YAML assertions on the compose file.

**Libraries:** —

**Details.** Per §16.2: named volumes make /state (SQLite+tokens+ledger) and /data (Parquet) survive `compose up -d` recreates. A static test parses the compose YAML to assert the durability + (later) exposure invariants. Real secrets live in a gitignored secrets/.env.

**Validation — unit tests:**

- tests/unit/deploy/test_compose_config.py::test_named_volumes_present asserts trader_state and trader_data are declared and mounted
- tests/unit/deploy/test_compose_config.py::test_restart_policy_unless_stopped
- tests/unit/deploy/test_compose_config.py::test_env_file_referenced_not_inline_secrets

**Validation — manual:**

- Run: `uv run pytest tests/unit/deploy/test_compose_config.py -q && docker compose -f deploy/docker-compose.yml config` — expected: green + valid config
- Manual: `docker compose up -d`, write some state, `docker compose down && up -d` — expected: state (token age, ledger) persists across recreate

**Deliverable.** A compose deployment with durable volumes verified to survive container recreation.

**Depends on:** M4.8

#### M4.10 — Paper soak runbook + multi-day soak

**Goal.** Run a multi-day in-container paper soak against live quotes to validate the whole system end-to-end before any real money.

**Build (files):**

- `docs/runbooks/paper-soak.md` *(create)* — Runbook: deploy in paper mode, what to watch (heartbeat, alerts, audit, token-age, reconciliation), how to inject faults, success criteria.
- `docs/runbooks/weekly-reauth.md` *(create)* — The §16.4 headless re-auth runbook (Option A laptop-auth→copy-token; Option B SSH port-forward).

**Libraries:** —

**Details.** The dress rehearsal (§15 paper soak). Runs for several market days with real live quotes and simulated fills, exercising the scheduler/jitter/calendar, risk gate, reconciliation, alerting, heartbeat, and the weekly re-auth — with zero real orders. Induce faults (kill the container, expire the token, feed a stale quote) and confirm alerts + safe behavior.

**Validation — unit tests:**

- *(integration/manual milestone — see manual validation)*

**Validation — manual:**

- Manual: deploy paper mode for ≥3 market days; expected: triggers fire on schedule with drift, audit rows accumulate, heartbeat stays fresh, no real orders, state survives a mid-soak `compose down/up`
- Manual: force the refresh token to expire; expected: re-auth alert fires ahead of time and the daemon enters READ-ONLY safe mode (not crash) if missed
- Manual: stop the container abruptly; expected: on restart it reconciles before acting and resumes without double-firing (ledger)

**Deliverable.** A validated multi-day paper soak + the operational runbooks, clearing the system for guarded live trading.

**Depends on:** M4.9


## M5 — Live (guarded)

> **Intent.** Introduce the FIRST real-money capability behind hard safety gates: the Schwab order endpoints + SchwabBroker adapter, idempotent order placement (write-ahead client order id, reuse-on-retry, reconcile-before-resend), the kill switch, the PDT rule, and a go-live double-confirm. Real orders happen only in the final, manual, smallest-size verification step.
>
> **Prerequisites:** M0, M1 (Schwab client/transport), M3 (orchestrator/sizing), M4 (risk gate, reconciliation, kill-switch infra).
> **New libraries:** `(none new — reuses M1 httpx client + M4 risk/reconcile)`
>
> **Exit criteria.** Small-size live trades match intent; idempotency prevents duplicate fills under retry (property-tested); the kill switch halts new orders and survives restart; PDT enforcement works; going live requires an explicit double-confirm; reconciliation is clean; deployed via `docker compose up -d`.

*8 sub-steps.*

#### M5.1 — Schwab order + account endpoints

**Goal.** Add the order placement/replace/cancel + status-poll + positions/balances endpoints to the first-party client, contract-tested against recorded shapes (no live calls in CI).

**Build (files):**

- `src/trader/schwab/orders.py` *(create)* — SchwabClient order methods: place_order(hashed_acct, order_json)->order_id (POST .../orders, parse 201 Location header), get_order(id)->status, cancel_order(id), replace_order(id, json); plus get_positions/get_account (GET accounts?fields=positions). Builds the §8.5 order JSON (MARKET/LIMIT, instruction, instrument).
- `tests/unit/schwab/test_orders.py` *(create)* — respx contract tests for place/poll/cancel + payload shape.
- `tests/fixtures/schwab/order_*.json` *(create)* — Recorded-shape fixtures (sanitized).

**Libraries:** —

**Details.** Per §8.5 ([VERIFY]). place_order reads the new id from the 201 Location header (not the body) and never assumes a synchronous fill — status is polled. Uses the hashed account id (M1.8). All [VERIFY] facts isolated here. Contract-tested with respx; no live calls in CI.

**Validation — unit tests:**

- tests/unit/schwab/test_orders.py::test_place_order_parses_location_header asserts the order id is read from Location on 201
- tests/unit/schwab/test_orders.py::test_order_json_shape asserts MARKET/LIMIT payloads match §8.5
- tests/unit/schwab/test_orders.py::test_poll_status_maps_enums and ::test_cancel_order

**Validation — manual:**

- Run: `uv run pytest tests/unit/schwab/test_orders.py -q` — expected: green, no network

**Deliverable.** Contract-tested Schwab order + account endpoints on the first-party client (no live calls yet).

**Depends on:** M1

#### M5.2 — SchwabBroker adapter (implements Broker)

**Goal.** Adapt the Schwab order/account endpoints to the core Broker interface so the orchestrator can place real orders through the same abstraction as SimBroker.

**Build (files):**

- `src/trader/broker/schwab_broker.py` *(create)* — SchwabBroker implementing Broker: submit_order/get_order/cancel_order/get_positions/get_account delegating to the SchwabClient; maps Schwab statuses → OrderStatus; respects READ-ONLY safe mode (refuses submit, raises typed error).
- `tests/unit/broker/test_schwab_broker.py` *(create)* — Conformance + mapping tests with a fake/mocked SchwabClient.

**Libraries:** —

**Details.** The live counterpart of SimBroker (§5). Behind the Broker Protocol so the orchestrator is unchanged. In READ-ONLY safe mode (dead refresh token, M1.6) submit_order refuses and raises — never silently drops. Idempotency lives in M5.3 (above the broker).

**Validation — unit tests:**

- tests/unit/broker/test_schwab_broker.py::test_satisfies_broker_protocol
- tests/unit/broker/test_schwab_broker.py::test_status_mapping maps Schwab→OrderStatus
- tests/unit/broker/test_schwab_broker.py::test_safe_mode_refuses_submit

**Validation — manual:**

- Run: `uv run pytest tests/unit/broker/test_schwab_broker.py -q` — expected: green

**Deliverable.** A SchwabBroker conforming to Broker, swappable with SimBroker, safe-mode aware.

**Depends on:** M5.1, M0.3

#### M5.3 — Idempotent order placement (write-ahead + reuse + reconcile-before-resend)

**Goal.** Guarantee at-most-once fills: persist the client order id as 'pending' BEFORE the network call, reuse it on retry, and reconcile before any resend — property/fuzz tested under timeouts and crashes.

**Build (files):**

- `src/trader/execution/idempotency.py` *(create)* — submit_idempotent(broker, state, order): write order 'pending' (client_order_id) before submit; on timeout/unknown response, query status/reconcile before resend; reuse the same client_order_id; mark filled/failed. Never resend an order with unknown outcome without reconciling.
- `tests/unit/execution/test_idempotency.py` *(create)* — Property/fuzz tests under timeout/crash interleavings (FakeBroker).
- `tests/integration/test_idempotency_fuzz.py` *(create)* — Hypothesis-driven interleavings asserting at-most-once.

**Libraries:** `hypothesis`

**Details.** Per §8.6/§10 — the highest-severity correctness concern (a naive retry can double a real position). Write-ahead 'pending' makes the intent durable before the network call; reconcile-before-resend + reused client id ensure at-most-once even across crashes. Hypothesis fuzzes timeout/duplicate/crash orderings.

**Validation — unit tests:**

- tests/unit/execution/test_idempotency.py::test_pending_persisted_before_submit (spy ordering) asserts the 'pending' row is written before broker.submit_order
- tests/unit/execution/test_idempotency.py::test_retry_reuses_client_id_no_double_fill
- tests/integration/test_idempotency_fuzz.py::test_at_most_once_under_interleavings (hypothesis) asserts no double fill across randomized timeout/crash sequences

**Validation — manual:**

- Run: `uv run pytest tests/unit/execution/test_idempotency.py tests/integration/test_idempotency_fuzz.py -q` — expected: green

**Deliverable.** Idempotent, crash-safe order placement proven at-most-once under fuzzed failure interleavings.

**Depends on:** M4.1, M5.2

#### M5.4 — Kill switch

**Goal.** Add the persisted kill switch checked every cycle and immediately pre-submit, flippable by CLI, with automatic trips on dangerous conditions.

**Build (files):**

- `src/trader/state/migrations/00X_kill_switch.sql` *(update)* — Ensure kill_switch table (flag, reason, source, updated_at) exists (from M0.7) and is read each cycle.
- `src/trader/risk/kill_switch.py` *(create)* — KillSwitch: is_engaged(state); engage(reason, source)/disengage(); auto-trip hooks (daily-loss breach, repeated broker errors, reconcile mismatch, stale data). Read at cycle start AND pre-submit.
- `src/trader/app/cli.py` *(update)* — `kill --on/--off [--reason]` flips the persisted flag.
- `tests/unit/risk/test_kill_switch.py` *(create)* — Engage/halt/persist/auto-trip tests.

**Libraries:** —

**Details.** Per §10. Persisted so it survives restarts; checked at the start of every cycle and immediately before every submit. Auto-trips on daily-loss-limit breach, repeated broker errors, reconciliation mismatch, or stale data. On trip: halt new orders + alert (no auto-flatten by default).

**Validation — unit tests:**

- tests/unit/risk/test_kill_switch.py::test_engaged_blocks_new_orders
- tests/unit/risk/test_kill_switch.py::test_persists_across_restart (re-open DB)
- tests/unit/risk/test_kill_switch.py::test_auto_trip_on_daily_loss and ::test_checked_pre_submit

**Validation — manual:**

- Run: `uv run pytest tests/unit/risk/test_kill_switch.py -q` — expected: green
- Manual: `trader kill --on` then run a cycle — expected: no orders placed; alert fired; `kill --off` re-enables

**Deliverable.** A persisted, auto-tripping kill switch enforced at cycle start and pre-submit.

**Depends on:** M4.3

#### M5.5 — PDT rule (configurable)

**Goal.** Enforce the pattern-day-trader constraint as a configurable risk rule (rolling 5-day day-trade count, $25k threshold).

**Build (files):**

- `src/trader/risk/pdt.py` *(create)* — PDTRule: track day-trades over a rolling 5 business-day window from fills; block a 4th day-trade when equity < $25k and enforce_pdt; configurable thresholds ([VERIFY] post-2026 FINRA 4210).
- `tests/unit/risk/test_pdt.py` *(create)* — Counting + blocking + disabled-flag tests.

**Libraries:** —

**Details.** Per §10 ([VERIFY] — do NOT hardcode; configurable). Counts day-trades (open+close same session) over a rolling 5-business-day window; blocks the 4th when equity<$25k. enforce_pdt toggles it (cash accounts may disable). Integrated into the risk gate.

**Validation — unit tests:**

- tests/unit/risk/test_pdt.py::test_blocks_fourth_day_trade_under_25k
- tests/unit/risk/test_pdt.py::test_allows_when_equity_over_25k
- tests/unit/risk/test_pdt.py::test_rolling_window_expiry and ::test_disabled_when_enforce_pdt_false

**Validation — manual:**

- Run: `uv run pytest tests/unit/risk/test_pdt.py -q` — expected: green

**Deliverable.** A configurable PDT day-trade-count rule wired into the risk gate.

**Depends on:** M4.3

#### M5.6 — Go-live double-confirm + safe rollout guards

**Goal.** Make switching to live require two explicit signals and start with the smallest possible exposure, so live can never be entered silently.

**Build (files):**

- `src/trader/app/cli.py` *(update)* — `run` in live mode requires mode=live AND env TRADER_CONFIRM_LIVE=I_UNDERSTAND (or a CLI --confirm-live); logs + ALERTS the live state at startup; wires SchwabBroker + idempotency + full risk gate + kill switch.
- `src/trader/app/live_guard.py` *(create)* — live_preflight(config): asserts allowlist set, conservative max_order_notional/position caps, kill switch off, reconciliation clean, token valid; refuses to start otherwise.
- `tests/unit/app/test_live_guard.py` *(create)* — Refuse-without-confirm + preflight tests.

**Libraries:** —

**Details.** Per §10. Two signals (config flag + env/CLI confirm) prevent accidental live; the live state is alerted at startup so it's never silent. live_preflight enforces a conservative rollout (allowlist, small caps, clean reconcile, valid token, kill switch off). Unit-tested so the safety gate is CI-enforced (not manual).

**Validation — unit tests:**

- tests/unit/app/test_live_guard.py::test_refuses_live_without_confirm asserts mode=live without TRADER_CONFIRM_LIVE exits non-zero
- tests/unit/app/test_live_guard.py::test_preflight_requires_allowlist_and_small_caps
- tests/unit/app/test_live_guard.py::test_startup_alert_on_live

**Validation — manual:**

- Run: `uv run pytest tests/unit/app/test_live_guard.py -q` — expected: green

**Deliverable.** A double-confirm go-live gate with a conservative preflight, CI-enforced.

**Depends on:** M5.3, M5.4

#### M5.7 — Guarded live verification (first real orders)

**Goal.** Place the first real-money orders at the smallest size behind the allowlist and verify intent-match, reconciliation, and the kill switch — the system's safety gate, manual by necessity.

**Build (files):**

- `docs/runbooks/go-live.md` *(create)* — Go-live runbook: tiny size, single allowlisted symbol, monitoring checklist, abort/kill procedure, rollback to paper.

**Libraries:** —

**Details.** This is the FIRST place real orders occur (the §17 safety gate). Done manually with the smallest possible size and a one-symbol allowlist, watching the audit trail, reconciliation, and alerts. Verify a real fill matches intent, reconciliation stays clean, and the kill switch halts new orders. Keep size minimal until confidence is established.

**Validation — unit tests:**

- *(integration/manual milestone — see manual validation)*

**Validation — manual:**

- Manual (REAL MONEY, smallest size): enable live with the double-confirm and a 1-share allowlisted order — expected: the order places, fills, and the audit/positions match intent; reconciliation is clean
- Manual: flip `kill --on` mid-session — expected: no further orders; alert fired
- Manual: induce a retry (network blip) — expected: no duplicate fill (idempotency holds)

**Deliverable.** Verified guarded live trading at minimal size with intent-match, clean reconciliation, and a working kill switch.

**Depends on:** M5.6

#### M5.8 — Deploy live via compose

**Goal.** Promote the validated live configuration to the server via docker compose, with monitoring and the re-auth runbook in place.

**Build (files):**

- `deploy/docker-compose.yml` *(update)* — Document/enable the live env (TRADER_CONFIRM_LIVE in the gitignored secrets/.env); confirm volumes, restart, healthcheck, alerting wired.
- `docs/runbooks/go-live.md` *(update)* — Add the compose deployment + monitoring steps.

**Libraries:** —

**Details.** Per §16. Deploy the same image in live mode; the daemon reconciles before acting on boot, honors the kill switch + ledger, and alerts on the live state. Keep the conservative caps until soak confidence; the weekly re-auth runbook (M4.10) applies.

**Validation — unit tests:**

- *(integration/manual milestone — see manual validation)*

**Validation — manual:**

- Manual: `docker compose up -d` in live mode — expected: startup alert confirms LIVE, healthcheck green, conservative caps active, reconciliation clean on boot

**Deliverable.** The validated trader running live on the server via docker compose with monitoring and runbooks.

**Depends on:** M5.7


## M6 — Refine calculation

> **Intent.** Turn the placeholder strategy layer into a real, extensible one without touching the stable Strategy interface or the live decision path. Deliver a strategy development guide + copyable template, at least one real strategy (zscore_revert) plus shared indicator helpers, an OFFLINE-ONLY vectorized parameter-research harness that reuses the same indicator math but never runs live, and per-strategy + combined backtest comparison/reporting. Exit: adding a new strategy is a pure config binding change plus a class swap, and backtests are reproducible both per-strategy and combined.
>
> **Prerequisites:** M0, M2 (engine/report/manifest), M3 (StrategyRegistry M3.6, bindings loader M3.7, attribution M3.9).
> **New libraries:** `jinja2`, `matplotlib`
>
> **Exit criteria.** Adding a new strategy requires only (a) a config binding entry (id/name/params/universe/slots) and (b) a strategy class registered in the StrategyRegistry that passes tests/unit/test_strategy_contract.py - no changes to orchestrator, risk gate, broker, or scheduler. At least one real strategy (zscore_revert, mean-reversion using shared indicators) is implemented and unit-tested. A strategy development guide + copyable template exist. `trader backtest` over a >=2-strategy config emits a per-strategy AND combined report (HTML + deterministic JSON) with a run manifest, and the multi-strategy golden-run test (tests/backtest/test_golden_multistrategy.py) reproduces it bit-for-bit across runs and against a committed golden. The optional vectorized parameter-research harness exists in an isolated src/trader/research package proven (by test) to import no broker/auth/schwab/execution/orchestrator code, so it can never touch the live path. Nothing in M6 places real orders: every validation is unit-tested with injected fakes/VirtualClock or run as an offline/backtest-only CLI command (no real-money side effects, consistent with the pre-M5 safety rule).

*9 sub-steps.*

#### M6.1 — Strategy interface conformance test + golden contract for any registered strategy

**Goal.** Lock the stable Strategy contract (§5/§6) with a generic conformance test so every current and future strategy is proven pure, asof-bound, and broker-agnostic before we add real ones. This is the guardrail that makes the M6 exit criterion ('class swaps') safe.

**Build (files):**

- `tests/unit/test_strategy_contract.py` *(create)* — Generic, parametrized conformance suite run against every class in StrategyRegistry plus the existing stubs.
- `src/trader/strategy/contract.py` *(create)* — Helper assertions reused by tests: assert_decisions_well_formed(decisions, universe), assert_no_wallclock(strategy_cls), and a fixture-builder make_snapshot(asof, quotes).

**Libraries:** —

**Details.** contract.py exposes pure helper functions (no test framework deps): (1) assert_decisions_well_formed(decisions, universe): every Decision.action in {BUY,SELL,HOLD}; symbol in universe; quantity int >=0; HOLD => quantity==0; limit_price either None or Decimal>0; rationale is str. (2) assert_no_wallclock(strategy_cls): use inspect.getsource on the class module and assert no literal 'datetime.now(' / 'time.time(' / 'date.today(' tokens appear (enforces boundary rule 1, §4.1) - tolerate false positives by allowing an explicit allowlist comment marker. (3) make_snapshot(asof, quotes_dict) building MarketSnapshot from core types. test_strategy_contract.py: parametrize over registry.all_names() (from M3 StrategyRegistry) constructing each with default params; feed a deterministic FakeMarketDataProvider (asof-bound, returns canned bars) + injected VirtualClock fixed at asof + handcrafted MarketSnapshot + empty positions + a fixed Account; assert decide(...) returns a Sequence[Decision] that passes assert_decisions_well_formed; assert calling decide twice with identical inputs yields identical output (determinism / purity); assert the strategy never mutates the passed positions/account/snapshot (compare deep-copies before/after); assert assert_no_wallclock passes for each strategy module. Edge cases covered by fixtures: missing prev_close on a quote, zero-volume bar, empty universe -> returns [] or all-HOLD without raising.

**Validation — unit tests:**

- tests/unit/test_strategy_contract.py::test_decide_returns_wellformed_decisions asserts every registered strategy returns valid Decision objects scoped to its universe
- tests/unit/test_strategy_contract.py::test_decide_is_deterministic asserts identical inputs -> identical outputs across two calls
- tests/unit/test_strategy_contract.py::test_decide_does_not_mutate_inputs asserts positions/account/snapshot unchanged after decide
- tests/unit/test_strategy_contract.py::test_strategy_module_has_no_wallclock asserts no datetime.now/time.time in strategy source
- ⚙ *(added in plan review)* test_strategy_contract.py::test_no_wallclock_runtime — monkeypatch datetime.now/time.time/date.today to RAISE and assert decide() still works using only the injected clock (runtime guard; keep the source-grep only as a lint)
- ⚙ *(added in plan review)* test_strategy_contract.py::test_empty_universe / ::test_missing_prev_close / ::test_zero_volume_bar — every registered strategy handles these without raising (assertions, not just 'covered by fixtures')

**Validation — manual:**

- Run `poetry run pytest tests/unit/test_strategy_contract.py -v` and observe all cases pass for the existing M3 stub strategies (threshold, zscore_revert stub) with no network and no real clock used.

**Deliverable.** A reusable strategy conformance test + helper module that any new strategy must pass; CI now fails if a strategy reads the wall clock, mutates inputs, or emits malformed decisions.

**Depends on:** —

#### M6.2 — Shared indicator helpers (asof-safe, no-lookahead) in strategy/indicators.py

**Goal.** Provide pure, well-tested indicator functions (SMA, EMA, rolling mean/std, z-score, returns) that operate ONLY on already-asof-bounded bar sequences, so real strategies and the offline research harness share identical math (parity).

**Build (files):**

- `src/trader/strategy/indicators.py` *(create)* — Pure functions over Sequence[Bar] or Sequence[Decimal] returning Decimal/optional values; explicit insufficient-data handling.
- `tests/unit/test_indicators.py` *(create)* — Deterministic numeric assertions with handcrafted inputs and known expected values.

**Libraries:** —

**Details.** indicators.py functions (all Decimal-based, deterministic, no global state, no pandas in the hot path so they match what a strategy sees from get_bars): sma(values: Sequence[Decimal], window: int) -> Optional[Decimal]; ema(values, window) -> Optional[Decimal] with standard alpha=2/(window+1), seeded by SMA of first window; rolling_mean(values, window) and rolling_std(values, window, ddof=1) -> Optional[Decimal]; zscore(values, window) -> Optional[Decimal] = (last - mean)/std, returns None if std==0 or insufficient data; simple_returns(values) -> list[Decimal]; closes_from_bars(bars: Sequence[Bar]) -> list[Decimal] (extract close, assume bars already sorted ascending by ts and already asof-filtered upstream). Edge cases: window<=0 -> ValueError; len(values)<window -> None (never raise, never pad with future data); std==0 -> zscore None; NaN/None inputs rejected with ValueError. CRITICAL no-lookahead note in docstring: these functions assume the caller passes only ts<=asof rows (the MarketDataProvider already guarantees this per Appendix B); functions never reorder, shift(-1), or peek beyond the provided slice. Use Decimal arithmetic; for std use Decimal sqrt via decimal context to keep determinism (avoid float reductions per §9.5).

**Validation — unit tests:**

- tests/unit/test_indicators.py::test_sma_known_values asserts sma([10,20,30],3)==Decimal(20) and returns None when len<window
- tests/unit/test_indicators.py::test_zscore_known_values asserts zscore on a handcrafted series equals a precomputed Decimal and returns None when std==0
- tests/unit/test_indicators.py::test_ema_seeded_by_sma asserts EMA first value equals SMA of first window then recursion matches expected
- tests/unit/test_indicators.py::test_insufficient_data_returns_none_not_raise asserts all rolling fns return None (not exception) on short input
- tests/unit/test_indicators.py::test_invalid_window_raises asserts window<=0 raises ValueError

**Validation — manual:**

- Run `poetry run pytest tests/unit/test_indicators.py -v`; confirm all numeric assertions pass with exact Decimal values (no floating-point tolerance needed).

**Deliverable.** A shared, deterministic, no-lookahead indicator library that both production strategies and the offline research harness import - the single source of truth for the math.

**Depends on:** —

#### M6.3 — Strategy development guide + copyable template class

**Goal.** Document exactly how to write a new strategy against the stable interface and ship a fill-in-the-blanks template, so 'new strategies are pure config + class swaps' is concretely achievable by a human.

**Build (files):**

- `src/trader/strategy/strategies/template.py` *(create)* — A documented ExampleTemplateStrategy implementing Strategy.decide with TODO markers, using indicators.py, registered under name 'template'.
- `docs/strategy_guide.md` *(create)* — Step-by-step guide: interface contract, asof/no-lookahead rules, how to add params, register the class, add a binding to config, run a backtest, and the contract-test requirement.

**Libraries:** —

**Details.** template.py: class ExampleTemplateStrategy with __init__(self, lookback: int = 20, lot: int = 10, **params) storing typed params; decide(self, snapshot, positions, account, data, clock) pulling trailing bars via data.get_bars(symbol, start=clock.now()-N days, end=clock.now(), freq='1d', asof=clock.now()) for each symbol in snapshot.quotes, computing an indicator via indicators.py, emitting Decision(...) with rationale, defaulting to HOLD. Includes inline comments mapping each requirement: pure function, only injected inputs, asof-bound reads, never datetime.now, returns desired absolute share delta (sizing/risk happen later in orchestrator). Register it in the StrategyRegistry (M3) via the registry's decorator/registration mechanism so the contract test (M6.1) auto-covers it. docs/strategy_guide.md sections: (1) The contract (link to §6 + interface signatures); (2) Boundary rules you must obey (no wall clock, no sockets, no whole-array pandas, asof-bounded reads only - Appendix B); (3) Anatomy of a strategy (walk through template.py); (4) Params: keep them in `params:` of the binding, validated by pydantic strategy-param model if present; (5) Registering: add to registry; (6) Wiring config binding (id/name/params/universe/slots) - copy from config/default.yaml; (7) Testing: must pass tests/unit/test_strategy_contract.py and add a focused unit test on synthetic data (§15.1); (8) Backtesting it: `trader backtest --config ...` and reading the per-strategy report (forward-ref M6.6/M6.7). Keep guide in docs/ not as a report .md in repo root.

**Validation — unit tests:**

- tests/unit/test_strategy_contract.py (from M6.1) now also parametrizes over 'template' and passes, proving the template is a conformant strategy

**Validation — manual:**

- Open docs/strategy_guide.md and follow it to confirm steps reference real file paths and the registry API; run `poetry run pytest tests/unit/test_strategy_contract.py -k template -v` and see the template strategy pass the conformance suite.

**Deliverable.** A copy-paste strategy template that already passes the conformance suite, plus a developer guide that operationalizes the 'config binding + class swap' workflow.

**Depends on:** M6.1, M6.2

#### M6.4 — Real strategy implementation: zscore_revert (mean-reversion) behind the stable interface

**Goal.** Replace the M3 zscore_revert stub with a real mean-reversion calculation using shared indicators, proving the interface supports a non-trivial strategy with no engine changes (pure class swap).

**Build (files):**

- `src/trader/strategy/strategies/zscore_revert.py` *(update)* — Full implementation of ZScoreRevertStrategy.decide using indicators.zscore over trailing daily bars; entry/exit on z thresholds.
- `src/trader/strategy/params.py` *(create)* — Optional pydantic param models per strategy (ZScoreRevertParams) for validation; wired into binding loader if M3 supports it.
- `tests/unit/test_zscore_revert.py` *(create)* — Synthetic-bar unit tests asserting BUY below -z_entry, SELL above +z_entry, HOLD inside band, and exit logic.

**Libraries:** —

**Details.** ZScoreRevertParams(BaseModel): lookback: int (ge=2, default 20), z_entry: float (gt=0, default 2.0), z_exit: float (ge=0, default 0.5), lot: int (gt=0, default 10). ZScoreRevertStrategy(__init__(**params) -> validates via ZScoreRevertParams). decide(): for each sym in snapshot.quotes: bars = data.get_bars(sym, start=clock.now()-(lookback+5) days, end=clock.now(), freq='1d', asof=clock.now()); closes = indicators.closes_from_bars(bars); z = indicators.zscore(closes + [snapshot.quotes[sym].last], lookback) (include current quote as the latest observation, still asof-bound since it is the trigger-instant quote). Logic: if z is None (insufficient data) -> HOLD; if z <= -z_entry and current attributed/holding flat -> Decision(BUY, sym, lot, rationale=f'z={z:.2f}<=-{z_entry} oversold'); if z >= +z_entry -> Decision(SELL, sym, lot, rationale='overbought'); if abs(z) <= z_exit and holding -> emit closing Decision toward flat (use positions to find the strategy's attributed quantity if available, else size to lot); else HOLD. Strictly NO datetime.now (use clock), NO mutation of inputs. Register under name 'zscore_revert'. params.py keeps a registry name->ParamModel map consumed by the binding loader (M3) so config `params:` are validated at load time; if a strategy has no model, params pass through as dict (backward compatible). Edge cases: prev_close None irrelevant (uses bars); fewer than lookback bars -> HOLD; std==0 (flat price) -> zscore None -> HOLD.

**Validation — unit tests:**

- tests/unit/test_zscore_revert.py::test_buy_when_oversold feeds synthetic descending closes so z<=-z_entry and asserts a BUY Decision with correct lot and symbol
- tests/unit/test_zscore_revert.py::test_sell_when_overbought asserts SELL when z>=+z_entry
- tests/unit/test_zscore_revert.py::test_hold_inside_band asserts HOLD when |z|<z_entry and not holding
- tests/unit/test_zscore_revert.py::test_exit_toward_flat_when_reverted asserts a closing decision when holding and |z|<=z_exit
- tests/unit/test_zscore_revert.py::test_insufficient_bars_holds asserts HOLD when fewer than lookback bars available
- tests/unit/test_strategy_contract.py (M6.1) still passes for zscore_revert

**Validation — manual:**

- Run `poetry run pytest tests/unit/test_zscore_revert.py tests/unit/test_strategy_contract.py -k zscore -v`; all pass using an injected FakeMarketDataProvider and VirtualClock (no network, no real money - read-only/paper safe).

**Deliverable.** A real, validated mean-reversion strategy plugged in via the registry and config binding with zero changes to orchestrator/risk/broker - demonstrating the pure class-swap goal.

**Depends on:** M6.1, M6.2

#### M6.5 — Backtest metrics module (per-run analytics over the trade/equity record)

**Goal.** Compute the standard performance metrics (equity curve, returns, max drawdown, hit rate, turnover, exposure) from a backtest's recorded fills/equity so reporting (M6.6/M6.7) and comparison are driven by one tested calculation layer.

**Build (files):**

- `src/trader/backtest/metrics.py` *(create)* — Pure functions: build_equity_curve, max_drawdown, total_return, cagr, hit_rate, turnover, avg_exposure, summarize -> a Metrics dataclass.
- `tests/unit/test_backtest_metrics.py` *(create)* — Handcrafted equity/trade inputs with precomputed expected metric values.

**Libraries:** —

**Details.** metrics.py consumes the backtest's existing output structures from M2 (equity snapshots time series and the trade blotter / fills with strategy_id). Functions: build_equity_curve(equity_points) -> sorted list[(ts, equity)]; total_return(curve) -> Decimal; cagr(curve) using session count / 252; max_drawdown(curve) -> (peak_ts, trough_ts, dd_pct) computed in a single forward pass tracking running peak; hit_rate(trades) -> closed-trade win fraction (pair entries/exits per symbol+strategy FIFO to realize P&L per round trip); turnover(trades, avg_equity) -> sum(|notional|)/avg_equity; avg_exposure(positions_over_time) -> mean gross exposure / equity; summarize(curve, trades) -> Metrics(total_return, cagr, max_drawdown_pct, max_dd_window, hit_rate, num_trades, turnover, avg_exposure, final_equity, start_equity). All Decimal; deterministic; no plotting here. Accept an optional strategy_id filter so the SAME functions produce per-strategy and combined metrics (call summarize over filtered trades + reconstructed per-strategy equity contribution). Edge cases: empty trades -> hit_rate None, turnover 0; single equity point -> drawdown 0, return 0; division-by-zero guarded.

**Validation — unit tests:**

- tests/unit/test_backtest_metrics.py::test_max_drawdown_known_curve asserts dd on [100,120,90,150] equals 25% with correct peak/trough
- tests/unit/test_backtest_metrics.py::test_total_return_and_cagr asserts exact values on a handcrafted curve
- tests/unit/test_backtest_metrics.py::test_hit_rate_round_trips asserts FIFO round-trip pairing yields the expected win fraction
- tests/unit/test_backtest_metrics.py::test_turnover asserts turnover = total traded notional / avg equity
- tests/unit/test_backtest_metrics.py::test_empty_inputs_safe asserts no exceptions and sensible None/0 on empty trade list
- ⚙ *(added in plan review)* test_backtest_metrics.py::test_hit_rate_open_position_excluded, ::test_hit_rate_short_round_trip (SELL-open/BUY-close), ::test_cagr_subyear_annualization (precomputed expected)

**Validation — manual:**

- Run `poetry run pytest tests/unit/test_backtest_metrics.py -v`; verify exact Decimal metric values match the handcrafted fixtures.

**Deliverable.** A tested, reusable analytics layer that turns raw backtest records into comparable per-strategy and combined performance metrics.

**Depends on:** —

> ⚙ **Plan-review note.** Consumes the M2 backtest output structures and the M3.9 per-strategy attribution / blotter — declare those as prerequisites.

#### M6.6 — Per-strategy + combined backtest report generation (HTML/JSON + manifest)

**Goal.** Emit a reproducible backtest report that breaks out EACH strategy's performance and the COMBINED portfolio, with the run manifest (config_hash, data_hash, git commit, seeds) so results are re-derivable per §9.5/§9.6.

**Build (files):**

- `src/trader/backtest/report.py` *(create)* — BacktestReport: assembles per-strategy + combined Metrics, trade blotter, per-slot fire log, manifest; renders Jinja2 HTML and a machine-readable JSON.
- `src/trader/backtest/templates/report.html.j2` *(create)* — Jinja2 template: header w/ manifest, a combined section, then one section per strategy (metrics table + trade blotter + fire log + drift).
- `tests/unit/test_backtest_report.py` *(create)* — Asserts report JSON contains a combined block and one block per strategy_id, includes manifest fields, and HTML renders without error.

**Libraries:** `jinja2`

**Details.** report.py: build_report(run_result, config, manifest) where run_result carries the merged trade record + equity points + per-slot fire log (realized drift + seed) from the M2/M3 engine. Group trades/equity by strategy_id using M6.5 metrics.summarize(..., strategy_id=sid) for each enabled strategy AND for the combined portfolio (no filter). Output structure JSON: {manifest: {config_hash, data_hash, git_commit, base_seed, start, end, lib_versions}, combined: <Metrics+blotter+equity_curve points>, per_strategy: {sid: <Metrics+blotter+fire_log+equity_curve>}}. to_json(path) writes deterministically (sorted keys, Decimal->str, UTC ISO timestamps) so two runs of the same config produce byte-identical JSON (feeds the golden test M6.8). to_html(path) renders report.html.j2 (combined first, then a collapsible per-strategy section each). Manifest reuse: pull config_hash/data_hash/git commit from the existing M2 manifest builder; do not recompute differently. Edge cases: a strategy with zero trades still gets a section (all-zero metrics, 'no trades' blotter); combined equity is the account equity curve (not sum of per-strategy, since cash is shared) - document this and use the account-level equity series for combined.

**Validation — unit tests:**

- tests/unit/test_backtest_report.py::test_report_json_has_combined_and_per_strategy asserts keys 'combined' and 'per_strategy' with one entry per enabled strategy_id
- tests/unit/test_backtest_report.py::test_report_includes_manifest asserts config_hash/data_hash/git_commit/base_seed present
- tests/unit/test_backtest_report.py::test_html_renders feeds a synthetic run_result and asserts to_html produces non-empty HTML containing each strategy_id
- tests/unit/test_backtest_report.py::test_zero_trade_strategy_section asserts a strategy with no trades still produces a section without raising
- tests/unit/test_backtest_report.py::test_json_is_deterministic asserts two to_json calls on the same input are byte-identical

**Validation — manual:**

- Build a tiny synthetic run_result in a Python REPL/script (no network), call BacktestReport.to_html('/tmp/r.html') and to_json('/tmp/r.json'); open the HTML and confirm a Combined section plus one section per strategy with metric tables; `poetry run pytest tests/unit/test_backtest_report.py -v` passes.

**Deliverable.** A reproducible per-strategy + combined backtest report (HTML + deterministic JSON) including the run manifest - the artifact that proves 'backtests reproducible per strategy and combined'.

**Depends on:** M6.5

> ⚙ **Plan-review note.** report.py is CREATED in M2.10; this step UPDATEs it (per-strategy + combined HTML/JSON). Depends on M2.10 and the M3.10 report extension — declare the link so build order is unambiguous (no two milestones 'create' the same file).

#### M6.7 — Wire reporting into the backtest CLI + multi-strategy backtest run

**Goal.** Make `trader backtest` produce the per-strategy + combined report end-to-end over cached history for a config with >=2 strategies, with no live/real-money path involved (offline, deterministic).

**Build (files):**

- `src/trader/app/cli.py` *(update)* — Extend the existing `backtest` subcommand with --report-html/--report-json/--out-dir flags; after the engine run, call BacktestReport and write artifacts; print summary table to stdout.
- `config/default.yaml` *(update)* — Ensure a backtest-ready multi-strategy config (momentum=threshold + meanrev=zscore_revert) with base_seed set, per §11 example.
- `tests/integration/test_backtest_cli_report.py` *(create)* — Runs the backtest CLI against a tiny cached Parquet fixture and asserts report files are written with combined + per-strategy content.

**Libraries:** —

**Details.** cli.py backtest flow (reuse M2/M3 wiring - VirtualClock, HistoricalDataProvider over a fixture Parquet cache, SimBroker, merged time-sorted triggers across both strategies): after engine completes, build the manifest, call BacktestReport(run_result, config, manifest), write JSON+HTML into --out-dir (default ./backtest_reports/<run_id>/), and print a compact stdout table (per-strategy + combined: total_return, max_dd, hit_rate, num_trades). Flags: --report-json/--report-html booleans (default both on), --out-dir path, --config path (existing). The test uses a committed small Parquet fixture (e.g. tests/integration/fixtures/bars/) covering the two strategies' universes for a handful of sessions and a fixed base_seed in the test config so the run is deterministic and OFFLINE (no Schwab, no network - this is the read-only/no-real-money safe path mandated pre-M5). Assert: exit code 0; out-dir contains report.json + report.html; JSON has combined + both strategy_ids; the same fill set is attributed across strategies (sum reconciles to combined). Document in CLI help that backtest never touches the broker.

**Validation — unit tests:**

- tests/integration/test_backtest_cli_report.py::test_backtest_writes_reports asserts report.json and report.html exist after CLI run
- tests/integration/test_backtest_cli_report.py::test_report_has_both_strategies asserts JSON per_strategy contains 'momentum' and 'meanrev'
- tests/integration/test_backtest_cli_report.py::test_stdout_summary_table asserts captured stdout contains combined + per-strategy rows

**Validation — manual:**

- Run `poetry run trader backtest --config tests/integration/fixtures/backtest_two_strats.yaml --out-dir /tmp/bt` and confirm: exit 0, /tmp/bt/.../report.html shows Combined + momentum + meanrev sections, and NO network/broker call occurred (paper/offline only).

**Deliverable.** An end-to-end offline `trader backtest` that runs two real-ish strategies on cached data and emits the per-strategy + combined report - usable by the operator with one command.

**Depends on:** M6.4, M6.6

#### M6.8 — Golden-run reproducibility test for the multi-strategy report

**Goal.** Guarantee the per-strategy + combined backtest is bit-for-bit reproducible (guards against accidental lookahead/non-determinism regressions, §9.5/§15.7) - the concrete proof of the M6 exit criterion.

**Build (files):**

- `tests/backtest/test_golden_multistrategy.py` *(create)* — Runs the fixed-config/fixed-data backtest twice and against a committed golden JSON; asserts byte-identical report JSON.
- `tests/backtest/golden/report_two_strats.json` *(create)* — Committed golden report JSON for the fixture config + cached data + base_seed.
- `tests/backtest/fixtures/golden_config.yaml` *(create)* — Frozen backtest config (two strategies, base_seed, dates) pinned to the committed Parquet fixture.

**Libraries:** —

**Details.** Test runs the full backtest pipeline twice in-process (same VirtualClock/HistoricalDataProvider/SimBroker wiring, fixed base_seed) and asserts run1_json == run2_json (intra-run determinism), then asserts run1_json == committed golden report_two_strats.json minus volatile manifest fields (git_commit and lib_versions are normalized/stripped before compare since they change across environments; config_hash/data_hash/seed/metrics/trades ARE compared). The golden JSON is generated once via a documented `poetry run python -m trader.backtest.regen_golden` helper (or pytest --update-golden flag) so regen is reproducible. This exercises: seeded per-strategy jitter reproducibility (stable_hash(base_seed,date,strategy_id,slot_id) from §7.2 - same drift each run), asof no-lookahead (HistoricalDataProvider returns ts<=fire_ts only), and per-strategy attribution stability. Edge cases: ensure timestamps emitted UTC ISO, Decimals serialized as strings, dict keys sorted so equality is exact; if golden mismatch, test prints a unified diff of the two JSONs to ease debugging. Entirely offline - no Schwab, no real orders (pre-M5 safety).

**Validation — unit tests:**

- tests/backtest/test_golden_multistrategy.py::test_intra_run_determinism asserts two consecutive backtest runs produce byte-identical report JSON (after stripping volatile manifest fields)
- tests/backtest/test_golden_multistrategy.py::test_matches_committed_golden asserts the run matches tests/backtest/golden/report_two_strats.json
- tests/backtest/test_golden_multistrategy.py::test_per_strategy_blocks_reproducible asserts each strategy's trade blotter and metrics match the golden exactly

**Validation — manual:**

- Run `poetry run pytest tests/backtest/test_golden_multistrategy.py -v` twice; both runs pass identically. Then regenerate via the documented regen helper and confirm `git diff tests/backtest/golden/report_two_strats.json` is empty (proves stability across a clean regen).

**Deliverable.** A committed golden-run test proving the multi-strategy backtest (per-strategy + combined) is reproducible bit-for-bit - the regression guard for the whole M6 exit criterion.

**Depends on:** M6.7

> ⚙ **Plan-review note.** Specify exactly what config_hash/data_hash cover (canonicalized sorted-key JSON of the resolved config; Parquet content bytes) and regenerate the committed golden via the documented helper in the SAME CI image so the comparison is portable. Consider a minimal multi-strategy golden as early as M3 so M4/M5 regressions are caught before M6.8.

#### M6.9 — Offline vectorized parameter-research harness (NEVER on the live path)

**Goal.** Provide a fast, pandas/numpy-vectorized parameter-sweep tool for OFFLINE research that reuses the same indicator logic, with hard structural guarantees it can never run live or place orders (Appendix A: 'strictly for offline research, never on the live decision path').

**Build (files):**

- `src/trader/research/__init__.py` *(create)* — New isolated research package; module docstring states OFFLINE-ONLY, no broker/auth imports allowed.
- `src/trader/research/param_sweep.py` *(create)* — Vectorized sweep over a strategy's param grid against cached Parquet bars; outputs a results DataFrame/CSV; refuses to import broker/schwab/auth.
- `src/trader/app/cli.py` *(update)* — Add a `research sweep` subcommand (offline only) with a loud 'RESEARCH ONLY - does not trade' banner; reads only the data cache.
- `tests/unit/test_param_sweep.py` *(create)* — Asserts sweep produces a results grid, is deterministic for a fixed grid, and that the research package imports no broker/auth/schwab module.

**Libraries:** —

**Details.** param_sweep.py: load_cached_bars(symbols, start, end) from the Parquet cache (read-only, no network); for a given strategy family, run a VECTORIZED approximation using indicators.py-equivalent pandas ops on the full series for speed (clearly documented that this is an APPROXIMATION for ranking params, NOT the parity path - the event-driven backtest is the source of truth). sweep(param_grid: dict[str,list]) -> pandas.DataFrame with one row per param combo and columns: total_return, max_drawdown, hit_rate, num_trades (reuse formulas consistent with M6.5 where possible). Output to CSV/Parquet under research_results/. HARD ISOLATION (the load-bearing safety): (1) the research package must NOT import trader.broker, trader.schwab, trader.auth, trader.execution, or trader.orchestrator - enforce with a unit test using importlib + ast/inspect to scan the module's import graph; (2) no Order/Broker construction anywhere; (3) the CLI subcommand prints a banner 'RESEARCH ONLY - no orders, no broker, offline' and exits if mode!=backtest/research. Document in code + guide (M6.3) that promising params must be re-validated through the real event-driven backtest (M6.7) before any live use. Edge cases: empty grid -> empty DataFrame; missing cached symbol -> skip + warn (never fetch from network). This step is OPTIONAL per the milestone but isolated so it carries zero live risk.

**Validation — unit tests:**

- tests/unit/test_param_sweep.py::test_sweep_produces_grid asserts a 2x2 param grid yields 4 result rows with metric columns
- tests/unit/test_param_sweep.py::test_sweep_is_deterministic asserts identical results across two runs on the same cached fixture
- tests/unit/test_param_sweep.py::test_research_imports_no_broker scans src/trader/research import graph and asserts NO import of trader.broker/schwab/auth/execution/orchestrator
- tests/unit/test_param_sweep.py::test_missing_symbol_does_not_fetch asserts a missing cached symbol is skipped (no network) not fetched
- ⚙ *(added in plan review)* test_param_sweep.py::test_research_imports_no_broker (subprocess) — import the research modules in a subprocess and inspect sys.modules for any trader.broker/trader.schwab prefix AFTER import (catches transitive + lazy imports), and that sweep() imports none either

**Validation — manual:**

- Run `poetry run trader research sweep --strategy zscore_revert --grid lookback=10,20 z_entry=1.5,2.0 --data /data` against a local Parquet fixture; confirm it prints the 'RESEARCH ONLY' banner, writes a results CSV, and (verify via logs / a network-block) makes zero network calls and constructs no Order/Broker (no real-money path).

**Deliverable.** An isolated, offline-only vectorized parameter-sweep tool that accelerates research while being structurally incapable of trading - satisfying Appendix A and the pre-M5 safety rule.

**Depends on:** M6.2, M6.5

> ⚙ **Plan-review note.** Optional/lower-priority — deferrable without blocking M6 exit. Static import scans miss dynamic/lazy imports, so use the subprocess sys.modules check (mirror M7.10).


## M7 — Web UI (read-only monitoring)

> **Intent.** Build the password-gated, strictly read-only production monitoring UI as a separate FastAPI service (its own container) per §19 and §16.6. It opens the trading state SQLite DB read-only (sqlite mode=ro + PRAGMA query_only=ON), exposes ONLY GET endpoints plus login/logout POSTs, authenticates a single admin via argon2id with a signed stateless session cookie (CSRF + lockout), renders Jinja2+HTMX dashboards with auto-refresh, and is fronted by a Caddy TLS reverse proxy on an internal compose network. The service has no broker code path and writes nothing to the trading system; tests assert no write endpoints exist, the DB handle is read-only, secrets never appear in responses, and a UI crash never affects the trader.
>
> **Prerequisites:** M0, M3, M4 (read-only over the consolidated §12 state schema: orders/fills/positions/attributed_position/audit/kill-switch/heartbeat/fired-slot ledger/tokens). Read-only; no broker import.
> **New libraries:** `fastapi`, `uvicorn[standard]`, `jinja2`, `itsdangerous`, `argon2-cffi`, `python-multipart`, `httpx (test client / respx already present)`
>
> **Exit criteria.** Admin logs in over TLS (Caddy :443, the only exposed port) and monitors live data across all §19.3 surfaces (system/heartbeat/mode/kill-switch, per-strategy decisions/positions/P&L, account, orders/fills, alerts, config, token-age/re-auth). Automated tests prove: the service exposes NO write endpoints (only GET + login/logout POST), the state DB handle is read-only (mode=ro + PRAGMA query_only; any write raises), trader.web imports no broker/schwab/execution/auth code path, and no OAuth tokens/app secret/password hash appear in any response or rendered page. A request-level exception handler plus separate-container isolation guarantee a UI crash never affects the trader (the trader container stays healthy when web is stopped/crashes). All M7 unit tests pass via injected settings/clock + a seeded temp DB and FastAPI TestClient (no live network, no wall clock). Nothing in M7 places or can place an order — it is strictly read-only monitoring.

*11 sub-steps.*

#### M7.1 — Read-only state DB access layer (mode=ro / query_only)

**Goal.** Provide a connection/query helper the web service uses to read the trading state SQLite DB with a guaranteed read-only handle, so any accidental write raises rather than mutating trading state.

**Build (files):**

- `src/trader/web/db.py` *(create)* — ReadOnlyStateDB class: opens sqlite via URI `file:{path}?mode=ro&immutable=0` with `uri=True`, runs `PRAGMA query_only=ON` and `PRAGMA busy_timeout=5000` on every connection; `row_factory=sqlite3.Row`. Methods: `connect()` context manager yielding a connection; `query(sql, params=()) -> list[sqlite3.Row]` (parameterized only); `query_one(sql, params) -> Row|None`. Reads `observability.db_path` from injected config. Raises a clear error if the file is missing. No write methods exist.
- `tests/unit/web/test_db_readonly.py` *(create)* — Unit tests against a temp SQLite file seeded with a tiny table.

**Libraries:** —

**Details.** Use stdlib `sqlite3` with `sqlite3.connect('file:'+path+'?mode=ro', uri=True)`. After connect, execute `PRAGMA query_only=ON` (belt-and-suspenders alongside mode=ro). Set `busy_timeout` so reads don't fail while the daemon holds a brief write lock under WAL. Inject `db_path` (do not hardcode `/state/trader.sqlite`) so tests pass a temp path. Do NOT enable `immutable=1` (the daemon is actively writing -wal/-shm). Provide a single `query()` taking parameterized SQL; never build SQL by string interpolation. Keep this module import-clean: it must NOT import anything from `trader.broker`, `trader.schwab`, `trader.execution`, or `trader.auth`. Edge cases: missing DB file -> raise FileNotFoundError-style error surfaced as 503 later; concurrent WAL reads must succeed.

**Validation — unit tests:**

- tests/unit/web/test_db_readonly.py::test_select_returns_rows asserts a seeded row is read back via query()
- tests/unit/web/test_db_readonly.py::test_insert_raises asserts executing INSERT/UPDATE/CREATE on the handle raises sqlite3.OperationalError (read-only database / query_only)
- tests/unit/web/test_db_readonly.py::test_pragma_query_only_set asserts `PRAGMA query_only` returns 1 on the live connection
- tests/unit/web/test_db_readonly.py::test_missing_db_raises asserts opening a nonexistent path raises a clear error
- ⚙ *(added in plan review)* test_db_readonly.py::test_all_writes_raise (parametrized) — INSERT, UPDATE, DELETE, CREATE, DROP, ATTACH all raise on the mode=ro handle (full mutating-statement matrix, not just INSERT)

**Validation — manual:**

- Run: poetry run pytest tests/unit/web/test_db_readonly.py -q ; expect all tests pass and the INSERT-raises test confirms the handle cannot mutate state

**Deliverable.** A read-only DB access module proven (by test) to reject all writes while serving parameterized reads under WAL.

**Depends on:** —

#### M7.2 — FastAPI app skeleton + /healthz + settings + crash isolation

**Goal.** Stand up the FastAPI app factory with settings, the read-only DB wired in, a public /healthz endpoint, and a global exception handler so a UI error returns 500 (never crashes the process or touches the trader).

**Build (files):**

- `src/trader/web/settings.py` *(create)* — pydantic-settings `WebSettings`: WEB_ADMIN_USER, WEB_ADMIN_PASSWORD_HASH (argon2id), SESSION_SECRET, db_path (from observability.db_path), config_path (/config/config.yaml), SESSION_IDLE_SECONDS (default 1800), SESSION_ABSOLUTE_SECONDS (default 28800), LOGIN_MAX_ATTEMPTS (default 5), LOGIN_LOCKOUT_SECONDS (default 300), AUTO_REFRESH_SECONDS (default 15). Loaded from env (env_file in compose).
- `src/trader/web/app.py` *(create)* — `create_app(settings: WebSettings) -> FastAPI` app factory: instantiates ReadOnlyStateDB, mounts /static, configures Jinja2Templates, registers a global exception handler returning a generic 500 page (logs the traceback to the web service's own logger), and a `/healthz` GET returning {status:'ok'} (200) that does a trivial `SELECT 1` against the read-only DB and returns 503 if the DB is unreachable. App state holds settings + db.
- `src/trader/web/__init__.py` *(create)* — package marker / re-export create_app.
- `tests/unit/web/test_app_health.py` *(create)* — Tests using fastapi.testclient.TestClient.

**Libraries:** `fastapi`, `uvicorn[standard]`, `jinja2`

**Details.** App factory pattern so tests inject a `WebSettings` pointing at a temp DB and a known admin hash. /healthz must be unauthenticated (used by compose healthcheck `curl -fsS http://localhost:8000/healthz`). The global exception handler is the crash-isolation guarantee at the request level: any unhandled exception in a route returns 500 and logs, never propagates to crash uvicorn; combined with the separate-container design this satisfies 'a UI crash never affects the trader'. Use a dedicated structlog/stdlib logger named 'trader.web' writing to stdout (its own logs, per §12/§19 — NOT the trading DB). The app must import NOTHING from broker/schwab/execution/auth — add a guard test later (M7.10). Mount StaticFiles at /static from src/trader/web/static.

**Validation — unit tests:**

- tests/unit/web/test_app_health.py::test_healthz_ok asserts GET /healthz returns 200 {status:'ok'} when DB reachable
- tests/unit/web/test_app_health.py::test_healthz_db_down returns 503 when db_path points to a missing file
- tests/unit/web/test_app_health.py::test_unhandled_exception_returns_500 registers a temp route that raises and asserts client gets 500 (process not killed) and the error is logged

**Validation — manual:**

- Run: WEB_ADMIN_USER=admin WEB_ADMIN_PASSWORD_HASH='<argon2 hash>' SESSION_SECRET=test poetry run uvicorn trader.web.app:create_app --factory --port 8000 then curl -fsS http://localhost:8000/healthz ; expect {"status":"ok"}

**Deliverable.** A runnable FastAPI app with health check, settings, read-only DB wired in, and request-level crash isolation.

**Depends on:** M7.1

> ⚙ **Plan-review note.** Request-level 500 handling is necessary but ≠ process/container isolation. The 'a UI crash never affects the trader' invariant is proven by the M7.11 cross-container kill check, which should be an automated (compose-based) check, not purely manual.

#### M7.3 — Auth core: argon2id verify, signed stateless session cookie, lockout

**Goal.** Implement password verification (argon2id), signed stateless session tokens (itsdangerous), and an in-memory login attempt/lockout tracker — the security primitives, independent of routes.

**Build (files):**

- `src/trader/web/security.py` *(create)* — Functions: `verify_password(plain, stored_hash) -> bool` using argon2.PasswordHasher().verify with constant-time semantics (catch VerifyMismatchError -> False); `make_session_token(username, now) -> str` and `read_session_token(token, now, idle_s, absolute_s) -> str|None` using itsdangerous.URLSafeTimedSerializer(SESSION_SECRET, salt='trader-web-session') carrying {user, issued_at, last_seen}; idle + absolute expiry checks; `LoginThrottle` class (keyed by username+client-ip) tracking failures with lockout window (record_failure/record_success/is_locked(now)). `make_csrf_token`/`validate_csrf` helpers (signed, salt='trader-web-csrf').
- `tests/unit/web/test_security.py` *(create)* — Unit tests for hashing, session token round-trip, expiry, lockout, CSRF — all using an injected `now` (no wall clock).

**Libraries:** `argon2-cffi`, `itsdangerous`

**Details.** Never store or log plaintext passwords. argon2id is the argon2-cffi default. Session token is STATELESS (no server-side session table — §12 says signed stateless cookies, web writes nothing): the signed payload itself is the session; idle timeout enforced by re-issuing the cookie with refreshed last_seen on each authenticated request, absolute timeout enforced from issued_at. Pass `now: datetime` explicitly into make/read/throttle functions (injected clock, NOT datetime.now()) so expiry/lockout are deterministically testable. LoginThrottle is in-memory (process-local) — acceptable for single-admin; document that it resets on restart. CSRF token is a signed value bound into the session/page and compared on POST (double-submit not needed since SameSite=strict, but include it for login/logout POST per §19.5). Edge cases: tampered token -> BadSignature -> None; expired -> SignatureExpired -> None; clock skew tolerated by absolute window.

**Validation — unit tests:**

- tests/unit/web/test_security.py::test_verify_correct_password returns True for matching argon2 hash; test_verify_wrong_password returns False
- tests/unit/web/test_security.py::test_session_roundtrip make->read returns the username within windows
- tests/unit/web/test_security.py::test_session_idle_expiry read returns None when now - last_seen > idle_s
- tests/unit/web/test_security.py::test_session_absolute_expiry read returns None when now - issued_at > absolute_s
- tests/unit/web/test_security.py::test_session_tampered_returns_none mutating a char yields None
- tests/unit/web/test_security.py::test_lockout_after_max_attempts is_locked True after LOGIN_MAX_ATTEMPTS failures; clears after lockout window (advanced now)
- tests/unit/web/test_security.py::test_csrf_validate accepts a valid token and rejects a forged one

**Validation — manual:**

- Run: poetry run python -c "from argon2 import PasswordHasher; print(PasswordHasher().hash('s3cret'))" to generate a hash, then poetry run pytest tests/unit/web/test_security.py -q ; expect all pass

**Deliverable.** Tested auth primitives (argon2id verify, signed stateless session, CSRF, lockout) with an injected clock.

**Depends on:** M7.2

#### M7.4 — Auth middleware + login/logout routes + login page

**Goal.** Wire the auth primitives into request handling: a dependency that guards all monitoring routes, plus GET/POST /login and POST /logout with CSRF, lockout, and secure cookie flags.

**Build (files):**

- `src/trader/web/auth.py` *(create)* — FastAPI dependency `require_session(request) -> str` that reads the session cookie, validates via read_session_token (injected clock=now()), refreshes last_seen (re-sets cookie), and on failure raises a redirect to /login (303) or 401 for API/HTMX requests. Helpers to set/clear the session cookie with httpOnly=True, secure=True, samesite='strict', path='/'.
- `src/trader/web/routes/auth_routes.py` *(create)* — Router: GET /login renders login.html with a fresh CSRF token; POST /login (form: username, password, csrf) -> verify CSRF, check LoginThrottle.is_locked, verify_password against WEB_ADMIN_USER/HASH (constant-time even for unknown user), on success set session cookie + record_success + redirect 303 to /, on failure record_failure + re-render with generic error; POST /logout -> verify CSRF, clear cookie, redirect to /login. Logs auth events (success/failure/lockout) to the web logger.
- `src/trader/web/templates/login.html` *(create)* — Minimal Jinja2 login form (username, password, hidden csrf) with no external assets; generic error message area.
- `tests/unit/web/test_auth_routes.py` *(create)* — TestClient-based auth flow tests with an injected clock and known admin hash.

**Libraries:** `python-multipart`

**Details.** python-multipart is required for FastAPI form parsing. The require_session dependency is applied to EVERY monitoring router (via router-level dependencies) so there is exactly one auth chokepoint. Generic error on bad login (do not reveal whether the user or password was wrong). Always run verify_password even for an unknown username (compare against a dummy hash) to avoid user-enumeration timing. Cookie flags: Secure (TLS-only, set always since prod is TLS; tests can override for http TestClient via a setting), HttpOnly, SameSite=strict, no Domain, Path=/. Idle-timeout refresh: each authenticated response re-issues the cookie with updated last_seen. CSRF: token rendered into the login form and validated on POST; same for logout. Log fields: event, username, client_ip, outcome — to stdout, never to the trading DB. Edge: locked-out attempt returns the login page with a lockout message and does NOT check the password.

**Validation — unit tests:**

- tests/unit/web/test_auth_routes.py::test_login_success sets a session cookie and redirects 303 to /
- tests/unit/web/test_auth_routes.py::test_login_wrong_password re-renders login with generic error, no cookie set
- tests/unit/web/test_auth_routes.py::test_login_missing_csrf is rejected (403)
- tests/unit/web/test_auth_routes.py::test_protected_route_redirects_when_unauthenticated GET / without cookie -> 303 to /login (or 401 for HTMX header)
- tests/unit/web/test_auth_routes.py::test_logout_clears_cookie POST /logout with CSRF clears the session cookie
- tests/unit/web/test_auth_routes.py::test_lockout_blocks_after_threshold N bad logins then is_locked path returns lockout response without verifying password
- ⚙ *(added in plan review)* test_auth_routes.py::test_idle_window_slides (request at t0 ok; at t0+idle−1 ok and re-issues cookie; later within idle still ok; gap>idle → 401/redirect) and ::test_absolute_timeout_caps_sliding

**Validation — manual:**

- Run: poetry run pytest tests/unit/web/test_auth_routes.py -q ; expect all pass. Then start the app, open /login, submit the admin password generated in M7.3 and confirm redirect to / (manual, read-only, no money side-effects).

**Deliverable.** Working single-admin login/logout with CSRF, lockout, secure stateless session cookies, and a guard dependency protecting all routes.

**Depends on:** M7.3

#### M7.5 — Read-only repository/query layer + secret-scrubbing serializers

**Goal.** Centralize all monitoring SQL behind a read-only repository that returns plain dicts with secrets (tokens, app secret, password hash) structurally excluded — so routes/templates can never surface credentials.

**Build (files):**

- `src/trader/web/repository.py` *(create)* — `MonitoringRepo(db: ReadOnlyStateDB, config_loader)` with read methods returning safe dicts: system_status(), schedule_status(), strategy_list()/strategy_detail(strategy_id), recent_decisions(strategy_id?, limit), positions_account(), positions_attributed(), account_summary(), pnl_summary(), recent_orders(limit)/order_fills(order_id), recent_alerts(limit), token_status() (issue/expiry timestamps + computed countdown — NEVER the token value), config_view(). Every method selects explicit columns (never SELECT *) so token/secret columns are never read. A module-level `SECRET_KEYS` denylist + `scrub(d)` helper redacts any stray key matching (token, refresh_token, access_token, secret, password, hash, app_key) as '***' for defense in depth.
- `tests/unit/web/test_repository.py` *(create)* — Tests over a seeded temp DB mirroring the M0/M3/M4 state schema (tokens table with a fake token value, orders, fills, positions, audit, daily counters, ledger).

**Libraries:** —

**Details.** Schema reference (from §12): tokens (access/refresh + issue/expiry timestamps), positions (account-level + per-strategy attributed), orders (intent, client_order_id, strategy_id, broker id, status, fees), fills, decision/trigger audit log (cycle id, strategy_id, inputs->signal->order->verdict->fill), start-of-day equity + realized/unrealized P&L (combined + per strategy), daily counters (trades/loss, account + per strategy), kill-switch flag, fired-slot ledger (slot_date, strategy_id, slot_id, drift, seed), strategy binding snapshot, heartbeat. token_status() reads ONLY the issue/expiry timestamp columns and computes `days_until_expiry` from an injected now — it must NOT read the token columns at all. config_view() reads the mounted config.yaml (read-only) via the config loader and scrubs secrets_ref details / never shows env secret values. All timestamps returned tz-aware/ISO. Keep SQL parameterized. This layer is the single place SQL lives so route handlers stay thin and the no-write guarantee is easy to audit.

**Validation — unit tests:**

- tests/unit/web/test_repository.py::test_token_status_excludes_token_value asserts the returned dict has days_until_expiry but NO access_token/refresh_token keys and the raw token string never appears
- tests/unit/web/test_repository.py::test_recent_orders_shape returns rows with strategy_id/status/fees and no secret columns
- tests/unit/web/test_repository.py::test_positions_account_vs_attributed returns both account total and per-strategy attribution
- tests/unit/web/test_repository.py::test_scrub_redacts_secret_keys scrub({'refresh_token':'x'}) -> '***'
- tests/unit/web/test_repository.py::test_config_view_no_secret_values config_view never contains the app secret/password hash value
- ⚙ *(added in plan review)* test_repository.py::test_token_status_no_token_columns — assert the executed SQL does not reference access_token/refresh_token columns AND recursively crawl the returned dict for a seeded sentinel token value (catch nested/serialized leakage)

**Validation — manual:**

- Run: poetry run pytest tests/unit/web/test_repository.py -q ; expect all pass, confirming no secret value is ever returned by any repo method

**Deliverable.** A read-only repository that supplies all monitoring data as safe dicts with secrets structurally excluded.

**Depends on:** M7.1

#### M7.6 — Base templates + HTMX auto-refresh layout + static assets

**Goal.** Establish the shared Jinja2 layout, nav, HTMX include with periodic auto-refresh fragments, and minimal CSS, so each monitoring view is a small partial.

**Build (files):**

- `src/trader/web/templates/base.html` *(create)* — Base layout: <head> loads vendored htmx.min.js from /static (no CDN), minimal CSS; top nav linking System/Schedule/Strategies/Account/Orders/Alerts/Config; shows logged-in user + logout button (POST form with CSRF); a global 'mode' and kill-switch badge in the header; content block.
- `src/trader/web/templates/_partials/refresh_wrapper.html` *(create)* — Reusable partial pattern: a div with hx-get to the fragment endpoint, hx-trigger='every {{ refresh_seconds }}s', hx-swap='innerHTML', showing 'last updated' timestamp.
- `src/trader/web/static/htmx.min.js` *(create)* — Vendored HTMX library (pinned version) served locally — no external CDN dependency (smaller attack surface).
- `src/trader/web/static/app.css` *(create)* — Minimal stylesheet: tables, badges (green=ok/red=alert/amber=warning for token countdown & kill switch), responsive layout.
- `tests/unit/web/test_templates_render.py` *(create)* — Tests that base.html and the refresh wrapper render without error given a minimal context.

**Libraries:** —

**Details.** Vendor HTMX locally (pin the version in pyproject notes) so no third-party CDN is contacted — consistent with the minimal-attack-surface posture. Auto-refresh uses hx-trigger='every Ns' driven by AUTO_REFRESH_SECONDS; each monitoring view renders a full page when loaded directly and an inner fragment when requested via the HX-Request header. Provide a Jinja helper/filter to format timestamps in America/New_York and to render badges. No inline secrets anywhere. CSRF token available to templates for the logout form. Keep CSS tiny and self-contained.

**Validation — unit tests:**

- tests/unit/web/test_templates_render.py::test_base_renders renders base.html with a minimal context and asserts nav links + logout form (with csrf) are present and no exception
- tests/unit/web/test_templates_render.py::test_refresh_wrapper_has_hx_attrs asserts hx-get and hx-trigger='every Ns' appear in output
- tests/unit/web/test_templates_render.py::test_static_htmx_served via TestClient GET /static/htmx.min.js returns 200

**Validation — manual:**

- Run: poetry run pytest tests/unit/web/test_templates_render.py -q ; expect pass. Manually load any page in a browser and confirm it loads htmx from /static (check devtools network: no external CDN call).

**Deliverable.** Shared template layout with local HTMX, auto-refresh fragment pattern, and styling — the chrome every view reuses.

**Depends on:** M7.4

#### M7.7 — System status + Schedule + Token/re-auth views

**Goal.** Deliver the headline monitoring page: mode, daemon heartbeat/health, market-open, kill-switch state, per-strategy next/last fire + realized drift + skipped slots, and the prominent refresh-token expiry countdown / re-auth status.

**Build (files):**

- `src/trader/web/routes/system_routes.py` *(create)* — Router (require_session): GET / and GET /system render system.html (mode, heartbeat age vs threshold -> healthy/stale badge, market_open, kill_switch on/off, token countdown). GET /system/fragment returns the inner partial for HTMX auto-refresh. GET /schedule renders schedule.html (per-strategy next/last fire, realized drift, skipped slots) + /schedule/fragment. GET /reauth renders token/re-auth status (countdown, amber/red badge, the §16.4 CLI runbook text + deep-link label — display only, executes nothing).
- `src/trader/web/templates/system.html` *(create)* — System dashboard extending base + refresh_wrapper; includes _partials/system_body.html.
- `src/trader/web/templates/_partials/system_body.html` *(create)* — Renders status fields and badges (refreshable fragment).
- `src/trader/web/templates/schedule.html` *(create)* — Schedule view + its refreshable partial (inline or _partials/schedule_body.html).
- `tests/unit/web/test_system_views.py` *(create)* — TestClient tests with an authenticated session and a seeded DB.

**Libraries:** —

**Details.** Heartbeat: repo reads the latest heartbeat timestamp; the view computes age vs a threshold using injected now and shows healthy/stale. Token countdown is the §8.2/§16.4 weekly re-auth reminder surfaced prominently (amber within 2 days, red if expired) — value only, never the token. The /reauth page explicitly states 'Re-auth is performed via CLI (option A/B in runbook)' and shows the command text but has NO button that executes anything (read-only, §19.4). Fragment endpoints detect HX-Request header to return the partial only. All views require an authenticated session (router-level dependency). Skipped-slots and realized-drift come from the fired-slot ledger.

**Validation — unit tests:**

- tests/unit/web/test_system_views.py::test_system_requires_auth GET / without session -> redirect/401
- tests/unit/web/test_system_views.py::test_system_shows_mode_and_killswitch authenticated GET /system shows the seeded mode and kill-switch state
- tests/unit/web/test_system_views.py::test_heartbeat_stale_badge with an old heartbeat timestamp + fixed now, response marks daemon stale
- tests/unit/web/test_system_views.py::test_token_countdown_no_token_value response shows days remaining but contains no token string
- tests/unit/web/test_system_views.py::test_system_fragment_returns_partial GET /system/fragment with HX-Request header returns only the inner partial (no <html>)
- tests/unit/web/test_system_views.py::test_schedule_shows_drift_and_skips schedule view lists per-strategy fire times, realized drift, skipped slots
- ⚙ *(added in plan review)* test_system_views.py::test_token_badge_thresholds — parametrized at amber boundary (≤ lead days), green (below), and red (expired) using injected now

**Validation — manual:**

- Run: poetry run pytest tests/unit/web/test_system_views.py -q ; expect pass. Manually log in and view /system: confirm mode, heartbeat, kill-switch, and a prominent token countdown render (no money side-effects).

**Deliverable.** Authenticated System, Schedule, and Re-auth/token monitoring pages with HTMX auto-refresh.

**Depends on:** M7.5, M7.6

#### M7.8 — Per-strategy + Account + P&L views

**Goal.** Deliver per-strategy monitoring (enabled state, params, universe, recent decision audit chain, attributed positions + P&L, trades-today vs limits) and the account view (broker-truth positions, cash/buying power/equity, daily P&L, daily-loss vs limit, gross exposure).

**Build (files):**

- `src/trader/web/routes/strategy_routes.py` *(create)* — Router (require_session): GET /strategies (list with enabled/params/universe/trades-today vs limit) + fragment; GET /strategies/{strategy_id} (detail: recent decisions audit chain inputs->decision->risk verdict->order->fill, attributed positions, per-strategy P&L) + fragment. 404 for unknown strategy_id.
- `src/trader/web/routes/account_routes.py` *(create)* — Router (require_session): GET /account renders broker-truth positions, cash/buying power/equity, daily P&L, daily-loss vs limit (with amber/red proximity badge), gross exposure vs limit + fragment.
- `src/trader/web/templates/strategies.html` *(create)* — Strategy list + detail templates (+ partials) extending base/refresh_wrapper.
- `src/trader/web/templates/account.html` *(create)* — Account dashboard template + refreshable partial.
- `tests/unit/web/test_strategy_account_views.py` *(create)* — TestClient tests (authenticated) over the seeded DB.

**Libraries:** —

**Details.** Per-strategy data uses the attributed sub-position ledger and per-strategy P&L/counters from §12. The decision audit chain renders the full per-cycle row (correlation/cycle id, inputs summary, decision, risk verdict + reasons, sized order, fill) — read from the audit table; truncate long rationale. Trades-today vs limits and daily-loss vs limit are display comparisons (no enforcement here — read-only). Account view shows broker-truth positions distinctly from attributed sums and flags any 'manual/unknown' bucket if present (§10). All numbers formatted from Decimal-safe strings (avoid float surprises). Limits come from the read-only config_view. Unknown strategy_id -> 404 (not 500). All routes require a session.

**Validation — unit tests:**

- tests/unit/web/test_strategy_account_views.py::test_strategy_list_shows_universe_and_params lists seeded strategies with universe/params and trades-today vs limit
- tests/unit/web/test_strategy_account_views.py::test_strategy_detail_renders_audit_chain detail page shows a seeded decision's inputs->verdict->order->fill
- tests/unit/web/test_strategy_account_views.py::test_unknown_strategy_404 GET /strategies/nope -> 404
- tests/unit/web/test_strategy_account_views.py::test_account_shows_positions_and_pnl account page shows broker-truth positions, equity, daily P&L, gross exposure
- tests/unit/web/test_strategy_account_views.py::test_daily_loss_proximity_badge near-limit loss renders an amber/red badge
- tests/unit/web/test_strategy_account_views.py::test_strategy_views_require_auth unauthenticated -> redirect/401

**Validation — manual:**

- Run: poetry run pytest tests/unit/web/test_strategy_account_views.py -q ; expect pass. Log in, open /strategies and a strategy detail, confirm the audit chain renders; open /account and confirm positions/P&L (read-only).

**Deliverable.** Authenticated per-strategy and account/P&L monitoring pages with auto-refresh.

**Depends on:** M7.7

#### M7.9 — Orders/Fills + Alerts + Config view

**Goal.** Deliver the remaining monitoring surfaces: recent orders/fills with status and rejection reasons, the alerts/log tail, and the read-only config view (with secrets scrubbed).

**Build (files):**

- `src/trader/web/routes/orders_routes.py` *(create)* — Router (require_session): GET /orders (recent orders/fills: client_order_id, strategy_id, side/qty, status, fees, rejection reason) + fragment; GET /orders/{order_id} (status transitions + fills) with 404 for unknown id.
- `src/trader/web/routes/alerts_routes.py` *(create)* — Router (require_session): GET /alerts (recent alerts: kill-switch trips, daily-loss breaches, reconciliation mismatches, stale-data halts, skipped slots, re-auth reminders, crashes) + fragment.
- `src/trader/web/routes/config_routes.py` *(create)* — Router (require_session): GET /config renders the current effective config (mode, schedule, strategy bindings, risk limits, execution, alerting channels) read from the mounted config.yaml — secrets/token/app-key/password-hash values scrubbed; labels relevant CLI commands (kill, reload, reauth) as text only.
- `src/trader/web/templates/orders.html` *(create)* — Orders/fills template + detail + refreshable partials.
- `src/trader/web/templates/alerts.html` *(create)* — Alerts/log-tail template + refreshable partial.
- `src/trader/web/templates/config.html` *(create)* — Config view template (scrubbed).
- `tests/unit/web/test_orders_alerts_config_views.py` *(create)* — TestClient tests (authenticated) over seeded DB + a temp config.yaml.

**Libraries:** —

**Details.** Orders view shows rejections WITH reasons (from the risk verdict / broker status). Alerts view tails the alert/audit rows (most recent first, bounded limit). Config view reads the mounted /config/config.yaml through the config loader and renders it with secrets removed — the secrets_ref/env values and any token/app_key/password_hash are scrubbed (reuse M7.5 scrub + explicit denylist), and it shows ONLY the structure/limits an operator monitors. The config page explicitly notes that changes are made via CLI/config file (§19.4) and shows the command labels but executes nothing. All routes session-guarded; unknown order id -> 404.

**Validation — unit tests:**

- tests/unit/web/test_orders_alerts_config_views.py::test_orders_lists_status_and_rejection orders page shows a seeded FILLED order and a REJECTED order with its reason
- tests/unit/web/test_orders_alerts_config_views.py::test_order_detail_shows_fills order detail shows fills + status transitions; unknown id -> 404
- tests/unit/web/test_orders_alerts_config_views.py::test_alerts_lists_recent shows seeded alerts (e.g. skipped slot, re-auth reminder)
- tests/unit/web/test_orders_alerts_config_views.py::test_config_view_scrubs_secrets config page contains risk limits/strategies but NOT the app secret, token, or password hash value
- tests/unit/web/test_orders_alerts_config_views.py::test_config_view_requires_auth unauthenticated -> redirect/401

**Validation — manual:**

- Run: poetry run pytest tests/unit/web/test_orders_alerts_config_views.py -q ; expect pass. Log in, open /orders, /alerts, /config and confirm data renders with no secret values visible.

**Deliverable.** Authenticated Orders/Fills, Alerts, and read-only Config monitoring pages completing the §19.3 surface.

**Depends on:** M7.8

#### M7.10 — Safety invariant tests: no-write, no-broker-import, no-secrets-leak

**Goal.** Add the M7 exit-criteria guard tests that assert the entire service is read-only, has no broker code path, exposes only GET + login/logout, and never leaks secrets in any response.

**Build (files):**

- `tests/unit/web/test_no_write_endpoints.py` *(create)* — Iterate app.routes and assert the ONLY non-GET routes are POST /login and POST /logout (and HEAD/OPTIONS); fail if any other POST/PUT/PATCH/DELETE exists. Assert /healthz is GET.
- `tests/unit/web/test_no_broker_import.py` *(create)* — Import trader.web and walk sys.modules / use importlib to assert no module under trader.web transitively imports trader.broker.SchwabBroker, trader.schwab, trader.execution, or trader.auth (the OAuth/credential module). Also assert the DB handle the app holds is read-only (attempt a write -> raises).
- `tests/unit/web/test_no_secret_leak.py` *(create)* — Seed the DB with sentinel secret values (refresh_token='SENTINEL_RT', app_secret='SENTINEL_SECRET', password_hash sentinel); crawl EVERY GET route while authenticated and assert none of the sentinels appears in any response body; also assert the password hash from settings never appears.

**Libraries:** —

**Details.** These three tests encode the §17 M7 exit criteria and §19.6 testing requirements. test_no_write_endpoints walks app.router.routes inspecting route.methods. test_no_broker_import uses importlib.util.find_spec / ast scan or imports trader.web in a subprocess and inspects sys.modules for forbidden prefixes (trader.broker, trader.schwab, trader.execution, trader.auth) — the web package must be importable WITHOUT those. test_no_secret_leak enumerates routes (skip ones needing path params or supply seeded ids) and checks bodies; combine with the read-only-handle assertion (write attempt raises). Keep the forbidden-module list in one place so future routes can't silently add a broker path. The crash-isolation requirement is covered by M7.2's exception-handler test plus the separate-container compose design in M7.11.

**Validation — unit tests:**

- tests/unit/web/test_no_write_endpoints.py::test_only_login_logout_are_post asserts the set of mutating routes == {POST /login, POST /logout}
- tests/unit/web/test_no_broker_import.py::test_web_does_not_import_broker_or_schwab asserts no trader.web module pulls in trader.broker/trader.schwab/trader.execution/trader.auth
- tests/unit/web/test_no_broker_import.py::test_app_db_handle_is_readonly attempting INSERT via the app's db raises
- tests/unit/web/test_no_secret_leak.py::test_no_sentinel_secret_in_any_response crawls all GET routes and asserts no sentinel token/secret/hash appears
- ⚙ *(added in plan review)* test_no_secret_leak.py::test_crawl_covers_all_get_routes — supply valid seeded ids for ALL path-param routes (strategy_id, order_id) so detail pages are scanned; assert covered route set == full GET route set (fail if any GET skipped)
- ⚙ *(added in plan review)* test_no_write_endpoints.py::test_single_readonly_handle — assert the app constructs exactly one read-only DB handle and no other sqlite3.connect without mode=ro

**Validation — manual:**

- Run: poetry run pytest tests/unit/web/test_no_write_endpoints.py tests/unit/web/test_no_broker_import.py tests/unit/web/test_no_secret_leak.py -q ; expect all pass — proving read-only, no broker path, no secret leak

**Deliverable.** Passing guard tests that codify the M7 read-only / no-broker / no-secret exit criteria.

**Depends on:** M7.2, M7.9

#### M7.11 — trader-web entrypoint + compose web + Caddy reverse proxy (TLS, internal network)

**Goal.** Wire the deployment: a `trader-web` uvicorn entrypoint, the compose `web` + `proxy` services on internal/edge networks with the state volume mounted (read-only handle), and a Caddyfile terminating TLS with only :443 exposed.

**Build (files):**

- `src/trader/app/web_main.py` *(create)* — `main()` console entrypoint `trader-web`: builds WebSettings from env, runs uvicorn.run(create_app(settings), host='0.0.0.0', port=8000, ...). No reload in prod.
- `pyproject.toml` *(update)* — Add web deps (fastapi, uvicorn[standard], jinja2, itsdangerous, argon2-cffi, python-multipart) and the `trader-web` console script entry point; keep lockfile pinned/hash-locked.
- `deploy/docker-compose.yml` *(update)* — Add `web` service (command ['trader-web'], env_file secrets/.env with WEB_ADMIN_USER/WEB_ADMIN_PASSWORD_HASH/SESSION_SECRET, mounts config.yaml:ro and trader_state, expose 8000 internal only, networks [internal, edge], healthcheck curl /healthz) and `proxy` service (caddy:2, ports ['443:443'] as the ONLY published port, Caddyfile:ro, caddy_data volume, network edge). Add networks internal{internal:true}+edge and caddy_data volume. trader stays on internal only.
- `deploy/Caddyfile` *(create)* — Reverse proxy config: site block terminating TLS (auto-HTTPS), reverse_proxy web:8000, security headers (HSTS, X-Content-Type-Options, X-Frame-Options DENY, Referrer-Policy), optional IP allowlist comment for VPN/Tailscale defense-in-depth.

**Libraries:** `fastapi`, `uvicorn[standard]`, `jinja2`, `itsdangerous`, `argon2-cffi`, `python-multipart`

**Details.** Per §16.6: web exposes only 8000 internally (NOT published), proxy publishes only 443. trader_state volume mounted RW so SQLite can use -wal/-shm, but the web connection itself is mode=ro (the read-only guarantee is at the connection level, M7.1). web shares no broker code (separate import surface, verified by M7.10). internal network (internal:true) keeps trader<->web private; edge network carries proxy<->web. Caddy provides auto-HTTPS (or a local self-signed/internal CA for testing). Secrets come only from env_file, never the image (§13). Add HSTS + clickjacking/MIME headers in Caddy. Document the VPN/IP-allowlist recommendation. Keep the trader service unchanged from §16.2 except network membership.

**Validation — unit tests:**

- ⚙ *(added in plan review)* test_compose_exposure.py::test_only_proxy_publishes_443 — load deploy/docker-compose.yml and assert proxy publishes only 443:443, web has expose:[8000] and NO ports:, trader is internal-only, and network internal has internal:true (CI gate, not manual)

**Validation — manual:**

- Run: docker compose -f deploy/docker-compose.yml config ; expect valid config with web (expose 8000, no host port), proxy (ports 443:443 only), internal+edge networks, trader_state shared
- Run: docker compose up -d web proxy ; then from the host curl -k https://localhost/healthz through the proxy -> {"status":"ok"}; confirm `docker compose port web 8000` shows NO host publication (internal only)
- Generate an argon2 hash, set WEB_ADMIN_* + SESSION_SECRET in secrets/.env, browse https://<host>/ over TLS, log in, and monitor live data — confirm read-only (no buttons that act), and that stopping/crashing the web container leaves `docker compose ps trader` healthy and trading unaffected (no real-money side-effects; M7 is read-only)

**Deliverable.** Deployable read-only web UI: trader-web entrypoint, isolated compose web service, and Caddy TLS proxy exposing only :443 on an internal network sharing the state volume read-only.

**Depends on:** M7.10

> ⚙ **Plan-review note.** SPLIT WHEN BUILDING into 2 baby steps — M7.11a trader-web uvicorn entrypoint + pyproject deps/console-script (locally launchable); M7.11b compose web+proxy services + Caddyfile + networks/volumes (validated by the static exposure test + through-proxy curl). Add an automated kill-web-container check that asserts `docker compose ps trader` stays healthy.


---

## Cross-cutting: build order & dependencies

**Milestone order (hard):** M0 → M1 → M2 → M3 → M4 → M5, with M6 (strategies/reporting) after M3 and M7 (read-only web) after M3/M4. M6 and M7 can proceed in parallel with later work once their prerequisites are met.

```
M0 ─┬─► M1 ──────────────► M5 (needs M1 client + M4 gates)
    ├─► M2 ─► M3 ─► M4 ─► M5
    │         └─► M6 (strategies, reports)
    └────────────► (M3,M4) ─► M7 (read-only web)
```

**Key cross-milestone couplings (made explicit per the review):**
- `backtest/engine.py` and `backtest/report.py` are **created in M2** (M2.8/M2.10); **M3.10 and M6.6 UPDATE them** — never re-create.
- `FakeBroker`/`FakeClock`/`FakeMarketData` are **created in M0.8** and reused everywhere.
- The migration runner is **M0.7**; each later milestone adds its own numbered migration (tokens=M1, fired-slot ledger + attributed_position=M3, …).
- The `StrategyRegistry` (M3.6) + bindings loader (M3.7) are the integration points M6 strategies plug into.
- M7 is **read-only** over the consolidated §12 schema produced by M0/M3/M4.

## Cross-cutting: test infrastructure

- `tests/fakes/` — FakeClock, FakeBroker, FakeMarketData (M0.8).
- `tests/fixtures/schwab/` — recorded/sanitized Schwab JSON for contract tests (M1/M5).
- `tests/backtest/golden/` — committed golden runs for reproducibility (M2.10, M6.8; consider a minimal multi-strategy golden at M3).
- Markers: `unit` (CI default) · `integration` · `network` (opt-in, real TLS/credentials, never in CI).

## Cross-cutting: safety checkpoints

1. **Pre-M5 gate (CI-enforced):** daemon refuses `mode=live`; only Sim/Fake brokers wired (M3.11, M4.7).
2. **M5 go-live gate:** double-confirm (config + env) + `live_preflight` (allowlist, small caps, clean reconcile, valid token, kill switch off) — unit-tested (M5.6).
3. **First real orders:** M5.7 only, manual, smallest size, behind a one-symbol allowlist, watching audit + reconciliation + kill switch.
4. **Idempotency:** property/fuzz-tested at-most-once before any live deploy (M5.3).

## Plan-review summary

The adversarial review raised **52 findings** across three lenses; all **high-severity** items are folded in (marked ⚙):
- **Granularity:** M1.6, M3.9, M3.11, M7.11 carry **split-when-building** notes (each bundles several concerns); thin steps (M3.8 sizing) flagged as merge candidates.
- **Validation completeness:** added deterministic concurrency tests (no thread-race flakiness), OAuth code-reuse / malformed-response tests, no-lookahead + calendar/DST boundary tests, ledger crash-mid-cycle recovery, the risk-gate single-chokepoint spy test, idempotency write-ahead ordering, read-only write-matrix, sliding-session, secret-leak crawl over all routes, and the compose-exposure CI gate.
- **Build-order/coverage:** reconciled `report.py`/`engine.py` ownership (created in M2; updated by M3/M6), made cross-milestone prerequisites explicit, and noted price-sanity is enforced at the data boundary (M1.9) + the M5 risk gate.

Remaining **medium/low** suggestions are captured inline as ⚙ notes to apply during implementation.
