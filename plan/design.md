# Automated Equity Trading Program — Design Document

- **Status:** Draft for review (no implementation yet)
- **Date:** 2026-06-27
- **Scope:** Design for a scheduled, configurable, backtestable equity auto-trader that executes through the Charles Schwab Trader API.

> ⚠️ **Verification note.** Several Schwab API facts below (exact token TTLs, the ~120 req/min rate limit, intraday price-history retention, the 201/`Location` order-response behavior) were gathered from community libraries and prior knowledge because Schwab's developer docs sit behind a login. Every such claim is tagged **[VERIFY]** and must be confirmed against the live Schwab developer portal before the relevant code is relied upon. The design is intentionally structured so these facts are isolated inside one adapter and do not ripple through the rest of the system.

---

## 1. Overview & Goals

A single-user, long-lived Python service that wakes up on a configurable schedule (~3 times per trading day, each fire time jittered by a bounded random drift), pulls current prices for a configurable set of tickers, runs a **pluggable strategy** to produce buy/sell/hold decisions (ticker + quantity), passes those decisions through a **non-bypassable risk gate**, and submits orders to Charles Schwab. The exact same decision code runs unchanged against historical data in a **backtest** harness — that live/backtest parity is the central design goal.

### Goals

- Trade US equities via the Schwab Trader API, with credentials the user registers manually.
- Run continuously and trigger on a configurable intraday schedule with a configurable random drift (≤ 30 min) on each fire.
- **Support multiple strategies, each on its own configurable schedule**; the orchestrator dispatches the correct strategy at each trigger (a strategy may even fire ~3×/day at its own times, independent of the others).
- On each trigger: fetch quotes → run the *bound* strategy → decide (action, ticker, quantity) → risk-check → execute.
- Keep each strategy a clean, swappable interface (the actual calculation is a stub now, refined later).
- Be fully testable on historical data, with the **same strategy/decision code path** used live and in backtest (with all strategies interleaved on the simulated clock in fire-time order).
- Default to safe (paper / dry-run); make going live an explicit, loud, hard-to-trip action.
- **Ship as a Docker image, deployed via docker compose on a server**, with durable state on mounted volumes.
- **Provide a password-gated, read-only production web UI** to monitor strategies, positions, P&L, orders, configurables, and system health — an isolated service that writes nothing to the trading system and can **never place orders or change config** (§19). Operational changes stay on the config file + CLI.

### Non-goals (for now)

- A specific profitable strategy (the calculation is deliberately a placeholder).
- Multi-user / multi-tenant SaaS or multi-account fleet management. (Running **multiple strategies on a single account** *is* in scope; a **single-admin, password-gated, read-only monitoring web UI** for production *is* in scope — see §19; a UI that controls/edits/trades, multiple brokerage accounts, and full user-management are not.)
- High-frequency / sub-second / market-microstructure trading (the cadence is ~3×/day).
- Options, futures, crypto, or non-US markets (equities only; design leaves room to extend).
- Fully unattended 24×7 operation **without any human** — Schwab's weekly re-auth (see §8) makes a periodic human touch unavoidable.

---

## 2. Requirements traceability

| # | User requirement | Where satisfied |
|---|------------------|-----------------|
| 1 | Trade equities via Charles Schwab API; user registers credentials manually | §8 Schwab integration; §13 Secrets; manual onboarding steps in §8.1 |
| 2a | Runs 24×7 (long-lived) | §7 Scheduler (APScheduler daemon under systemd/launchd); §16 Deployment |
| 2b | Triggered ~3×/day on a **configurable** schedule | §7 Scheduler config schema; §11 Configuration |
| 3 | Trigger time = fixed schedule + random drift within 30 min (configurable) | §7.2 Jitter design (bounded, configurable distribution/direction/max, seeded) |
| 4 | On trigger: fetch prices for configurable tickers → calculate → decide action/ticker/qty | §5 Abstractions; §6 Strategy interface; §7.3 trigger cycle |
| 4b | Calculation logic is a placeholder, refined later | §6 Strategy interface (pluggable, stub example) |
| 5 | Testable on historical data; live & backtest share the same code | §5 Interfaces (Clock/Data/Broker injection); §9 Backtesting; §15 Testing |
| 6 | **Multiple strategies, each on its own schedule; orchestrator triggers the corresponding strategy** | §6 Strategy registry & bindings; §7 per-strategy scheduling; §7.5 concurrency & ledger; §10 cross-strategy risk; Appendix C |
| 7 | **Deployed as a Docker image via docker compose on a server** | §3 stack; §14 layout (`deploy/`); §16 Deployment (compose, volumes, healthcheck, headless re-auth) |
| 8 | **Password-gated read-only web UI to monitor strategies & configurables** (production) | §19 Web UI (isolated service, TLS + auth, **read-only monitoring**, no write path); §4 boundary rule 6; §16.6 deployment |

---

## 3. Recommended tech stack

**Language: Python 3.11+.** Rationale: the quant/backtest ecosystem the design leans on is Python-native (exchange_calendars, APScheduler, pandas/pyarrow/DuckDB, pydantic), and the open-source Schwab clients — which we will **not** import (§8.7) — are written in Python, giving us a same-language parity reference for our own client. It maximizes reuse and minimizes the custom surface area. (Alternatives — Rust/Go for a tighter daemon, C# for QuantConnect's Lean — are viable but would forfeit the backtest tooling and that parity reference; not worth it at ~3 triggers/day.)

| Concern | Choice | Why / alternatives |
|---|---|---|
| Schwab API client | **First-party, in-house client** (no third-party broker SDK) | **Security:** an unaudited third-party library would handle our OAuth app secret, refresh token, and real-money order placement — an unacceptable supply-chain risk. We build a minimal, audited client on one vetted HTTP dependency (`httpx` recommended). **schwab-py** (`alexgolec/schwab-py`) and **Schwabdev** (`tylerebowers/Schwabdev`) are referenced **only as a functional spec / parity checklist** (endpoints, OAuth dance, token refresh, order payloads, streaming) — **not imported**. See §8.7. |
| Scheduler | **APScheduler 3.x** (`CronTrigger`, timezone-aware) | In-process scheduling with `misfire_grace_time`, `coalesce`, `max_instances`, persistent jobstore. Pin to 3.x (4.x is a rewrite). |
| Packaging & supervision | **Docker image + docker compose** (`restart: unless-stopped`) | Primary deployment: one container running the daemon, supervised by the Docker engine (replaces systemd as the restart layer). systemd/launchd remain valid for non-container hosts. |
| Trading calendar | **exchange_calendars** (`XNYS`) | Holidays, early closes, half days. Alt: `pandas_market_calendars`. |
| Config | **pydantic / pydantic-settings** | Typed, validated, layered (defaults < file < env < CLI). |
| Timezones | stdlib **zoneinfo** (`America/New_York`) | Avoid `pytz` localize/normalize pitfalls. |
| Durable state | **SQLite** (WAL mode) | ACID, single-file, easy backup; tokens, orders, positions, audit log, P&L, fired-slot ledger. |
| Bulk historical data | **Parquet** (+ DuckDB to query) | Columnar, compact, fast range scans for backtests. |
| Data analysis | pandas + pyarrow + (optional) Polars/DuckDB | Standard. |
| Logging | **structlog** (or stdlib JSON formatter) | Structured/queryable audit trail. |
| Alerting | Telegram bot + email (SMTP) | Two redundant channels + heartbeat. |
| Web UI backend | **FastAPI + uvicorn** | Async, Python-native (coexists with the rest of the stack); serves the monitoring API + server-rendered pages. Runs as a **separate service** (§19). |
| Web UI frontend | **Jinja2 server-rendered + HTMX** (recommended) | Minimal JS, small attack surface, fast to build for a single-user dashboard; a lightweight SPA is the alternative if richer interactivity is wanted. |
| Web auth | session cookies + **argon2id** password hash + CSRF | Single admin login; httpOnly/Secure/SameSite cookies; login rate-limit/lockout. |
| TLS / reverse proxy | **Caddy** (auto-HTTPS) or nginx/Traefik | Terminates TLS in front of the web service; only the proxy port is exposed. |
| HTTP resilience | token-bucket rate limiter + backoff (e.g. `tenacity`) | Stay under rate limits; retry 429/5xx. |
| Testing | pytest + hypothesis | Unit, integration, property/fuzz (idempotency). |
| Packaging | Poetry or uv (lockfile) | Reproducible deps for deterministic backtests. |

**Build the Schwab client in-house.** For the security reasons in §8.7, the broker client is also first-party — we do not take a runtime dependency on any third-party Schwab/broker SDK; the open-source clients are read only as a parity reference.

**Build vs. buy the engine.** Given the hard live/backtest-parity requirement, the low trigger frequency, and the need to control the clock/scheduler/jitter and fill semantics exactly, the recommendation is a **thin custom event-driven engine** (a few hundred lines: clock, data handler, sim broker, portfolio, strategy interface, risk gate) rather than adopting a heavyweight framework. If we later prefer batteries-included, **lumibot** is the closest off-the-shelf match for "same code live and backtest" and **backtrader** is the most flexible mature alternative; **vectorbt** is useful only as a separate offline parameter-research tool, not for the production/parity path.

---

## 4. High-level architecture

### 4.1 Component map

```
                         ┌──────────────────────────────────────────────┐
                         │                CONFIG (pydantic)              │
                         │  defaults < file(yaml) < env < CLI ; validated │
                         └───────────────┬──────────────────────────────┘
                                         │ injected everywhere
        ┌────────────────────────────────┼─────────────────────────────────────┐
        │                                 │                                      │
┌───────▼────────┐   ┌─────────────┐  ┌──▼───────────┐   ┌──────────────┐  ┌────▼────────┐
│   SCHEDULER    │   │  SECRETS    │  │   CLOCK /     │   │  MARKET DATA  │  │   STATE /    │
│ APScheduler +  │   │  keychain / │  │  TIMESOURCE   │   │   PROVIDER    │  │  PERSISTENCE │
│ jitter + cal.  │   │  enc. file  │  │ (real|virtual)│   │ (live|hist.)  │  │  (SQLite +   │
│ PER-STRATEGY   │   └──────┬──────┘  └──────┬────────┘   └──────┬───────┘  │   Parquet)   │
│ slots + ledger │          │                │                   │          └────┬─────────┘
        │ fires trigger     │ tokens         │ now()             │ quotes/bars    │ read/write
        │                   │                ▼                   │                │
        │            ┌──────▼──────────────────────────────────────────┐         │
        └───────────►│              TRADE CYCLE ORCHESTRATOR             │◄────────┘
                     │  snapshot mkt+portfolio → Strategy → Sizing →     │
                     │  RISK GATE → Broker.submit → poll fills → persist │
                     └───────┬───────────────────────────────┬──────────┘
                             │ Decision[]                     │ Order
                     ┌───────▼────────┐               ┌───────▼────────────────┐
                     │ STRATEGY        │               │     RISK GATE          │
                     │ REGISTRY        │               │ (single chokepoint,    │
                     │ (N pure,        │               │  fail-closed,          │
                     │  schedule-bound │               │  account-wide + per-   │
                     │  strategies)    │               │  strategy limits)      │
                     └─────────────────┘               └───────┬────────────────┘
                                                               │ approved Order
                                                       ┌───────▼────────────────┐
                                                       │   BROKER / EXECUTION    │
                                                       │  SchwabBroker (live)    │
                                                       │  SimBroker (paper/bt)   │
                                                       │  FakeBroker (tests)     │
                                                       └───────┬────────────────┘
                                                               │ orders / fills
                                              ┌────────────────▼───────────────────┐
                                              │  OBSERVABILITY: structured logs,    │
                                              │  audit trail, metrics, ALERTING     │
                                              │  (Telegram + email + heartbeat)     │
                                              └─────────────────────────────────────┘
```

**Boundary rules (the load-bearing invariants):**

1. **Each Strategy is pure and broker-agnostic** — it reads only the injected `Clock`, `MarketDataProvider`, and a read-only portfolio snapshot; it never calls `datetime.now()`, opens sockets, or touches whole pandas arrays.
2. **Every** outbound order passes through the **Risk Gate** — there is exactly one code path to the broker, and it goes through the gate, regardless of which strategy produced the order.
3. **Only** the `SchwabBroker` adapter talks to Schwab. Everything else uses the abstract `Broker` interface.
4. The **same orchestrator + strategies + risk gate** run in live and backtest; only the injected `Clock`, `MarketDataProvider`, and `Broker` differ.
5. **Each trigger carries the id of the strategy it is bound to.** The scheduler owns *which* strategy fires *when*; the orchestrator just runs the strategy named on the trigger. Concurrent fires from different strategies are **serialized through a single global cycle lock** so position/risk/day-state is read-modify-written atomically (see §7.5).
6. **The web UI is a separate, read-only service** (its own container). It only *reads* durable state for monitoring — it writes **nothing** to the trading state DB, has **no code path to the broker**, and never places orders or changes config. Operational changes go through the config file + CLI, never the UI. Details in §19.

### 4.2 Sequence — one live scheduled trigger

```
docker engine ── keeps container alive (restart: unless-stopped) ──► daemon's APScheduler
   │   fires trigger (strategy_id="momentum", slot_id="midday") at 12:07:23 ET (12:00 + 7m23s drift)
   │
   ├─► Acquire GLOBAL CYCLE LOCK (serializes overlapping fires from other strategies)
   │
   ├─► Slot ledger: INSERT (date, strategy_id="momentum", slot_id="midday") UNIQUE ─ present? ─► ABORT (no double-fire)
   │
   ├─► Calendar check: is 12:07:23 inside today's XNYS session? ── no ─► skip + alert
   │
   ├─► strategy = registry["momentum"]; universe = strategy binding's tickers
   ├─► Orchestrator.run_cycle(strategy, universe, now=12:07:23):
   │        ├─ MarketDataProvider.get_quote(sym, asof=now) for each ticker in universe
   │        ├─ price-sanity (non-stale, >0, spread ok)         ── bad ─► reject + alert
   │        ├─ Broker.get_positions() / get_account()  (+ reconcile vs local intent)
   │        ├─ strategy.decide(snapshot) ─► [Decision(BUY, AAPL, 10), Decision(HOLD, ...)]
   │        ├─ Sizing: Decision ─► Order(client_order_id=uuid, strategy_id, qty, side, type, limit?)
   │        ├─ RiskGate.check(order): account-wide limits + this strategy's per-strategy limits
   │        │         ── violation ─► REJECT (logged with full context)
   │        ├─ persist Order as 'pending' (write-ahead, tagged strategy_id) BEFORE network call
   │        ├─ Broker.submit_order(order)  ─► 201 + Location header order id
   │        ├─ poll Broker.get_order(id) until FILLED/PARTIAL/REJECTED (bounded)
   │        └─ persist fills (attributed to strategy_id), update positions, P&L, audit row
   │
   └─► Slot ledger: UPDATE status='done'; record realized drift + seed; emit metrics; RELEASE LOCK
```

### 4.3 Sequence — one backtest run

```
backtest CLI: config(mode=backtest, start, end, base_seed, data_vendor) 
   │
   ├─ build run manifest: hash(config) + hash(data snapshot) + git commit + seed
   ├─ wire: VirtualClock, HistoricalDataProvider(asof-bound), SimBroker(fees+slippage)
   │
   └─ for each trading_date in calendar.sessions(start, end):
          # build the MERGED, time-sorted set of triggers across ALL enabled strategies for the day:
          triggers = []
          for strat in config.strategies (enabled):
              for slot in strat.schedule.slots:
                  drift   = seeded_rng(base_seed, date, strat.id, slot.id).uniform(0, slot.drift_max)
                  fire_ts = localize(date, slot.time) + drift      # SAME jitter code as live
                  if in session (clamp/skip per policy):
                      triggers.append((fire_ts, strat.id, slot.id))
          for (fire_ts, strat_id, slot_id) in sorted(triggers):    # chronological interleave
              VirtualClock.advance_to(fire_ts)
              Orchestrator.run_cycle(registry[strat_id], strat.universe, now=fire_ts)  # SAME orchestrator+risk gate
                  HistoricalDataProvider returns rows with ts <= fire_ts ONLY  (no lookahead)
                  SimBroker fills next-quote/next-bar +/- slippage, models partials/commissions
   │
   └─ emit report: per-strategy AND combined equity curve, trades, P&L, drawdown, hit-rate, turnover + manifest
```

The only difference between the two diagrams is the three injected implementations (and live fires asynchronously while backtest walks the merged trigger list in order). That is the parity guarantee made concrete.

---

## 5. Core abstractions / interfaces

These interfaces are what make live/backtest parity structural rather than aspirational. Signatures only (Python typing); no logic.

```python
from typing import Protocol, Sequence, Optional, Literal
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

Side    = Literal["BUY", "SELL"]
Action  = Literal["BUY", "SELL", "HOLD"]
OrdType = Literal["MARKET", "LIMIT"]

@dataclass(frozen=True)
class Quote:
    symbol: str
    ts: datetime            # quote timestamp (tz-aware UTC); used for staleness checks
    last: Decimal
    bid: Decimal
    ask: Decimal
    volume: int
    prev_close: Optional[Decimal] = None

@dataclass(frozen=True)
class Bar:
    symbol: str
    ts: datetime            # bar close time (tz-aware UTC)
    open: Decimal; high: Decimal; low: Decimal; close: Decimal; volume: int

@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: int           # signed (negative = short)
    avg_price: Decimal
    market_value: Decimal

@dataclass(frozen=True)
class Account:
    cash: Decimal
    buying_power: Decimal
    equity: Decimal

# ---- Time -------------------------------------------------------------------
class Clock(Protocol):
    def now(self) -> datetime: ...                 # tz-aware; wall-clock live, virtual in backtest
    def is_market_open(self, at: Optional[datetime] = None) -> bool: ...

# ---- Market data ------------------------------------------------------------
# CRITICAL: every method takes `asof` (bound to the Clock) and returns ONLY
# data available at-or-before asof minus configured latency — structural no-lookahead.
class MarketDataProvider(Protocol):
    def get_quote(self, symbol: str, asof: datetime) -> Quote: ...
    def get_bars(self, symbol: str, start: datetime, end: datetime,
                 freq: str, asof: datetime) -> Sequence[Bar]: ...

# ---- Broker / execution -----------------------------------------------------
@dataclass(frozen=True)
class Order:
    client_order_id: str    # generated & persisted BEFORE submit; reused on retry (idempotency)
    strategy_id: str        # which strategy produced this order (attribution + per-strategy risk)
    symbol: str
    side: Side
    quantity: int
    order_type: OrdType
    limit_price: Optional[Decimal] = None
    tif: Literal["DAY", "GTC", "FOK"] = "DAY"

@dataclass(frozen=True)
class Fill:
    client_order_id: str
    broker_order_id: str
    symbol: str
    quantity: int
    price: Decimal
    fees: Decimal
    ts: datetime
    status: Literal["FILLED", "PARTIAL_FILL", "WORKING", "REJECTED", "CANCELED", "EXPIRED"]

class Broker(Protocol):
    def submit_order(self, order: Order) -> str: ...           # returns broker_order_id
    def get_order(self, broker_order_id: str) -> Fill: ...
    def cancel_order(self, broker_order_id: str) -> None: ...
    def get_positions(self) -> Sequence[Position]: ...
    def get_account(self) -> Account: ...
# Implementations: SchwabBroker (live), SimBroker (paper/backtest), FakeBroker (tests).

# ---- Strategy (the pluggable calculation — see §6) --------------------------
@dataclass(frozen=True)
class MarketSnapshot:
    asof: datetime
    quotes: dict[str, Quote]
    # strategies may pull more history via the injected MarketDataProvider (asof-bound)

@dataclass(frozen=True)
class Decision:
    action: Action
    symbol: str
    quantity: int                       # desired absolute share delta for BUY/SELL; 0 for HOLD
    limit_price: Optional[Decimal] = None
    rationale: str = ""                 # logged into the audit trail

class Strategy(Protocol):
    def decide(self, snapshot: MarketSnapshot, positions: Sequence[Position],
               account: Account, data: MarketDataProvider, clock: Clock) -> Sequence[Decision]: ...

# ---- Risk gate (single non-bypassable chokepoint) ---------------------------
@dataclass(frozen=True)
class RiskVerdict:
    approved: bool
    adjusted_order: Optional[Order]     # may clamp quantity instead of rejecting
    reasons: Sequence[str]

class RiskManager(Protocol):
    def check(self, order: Order, positions: Sequence[Position],
              account: Account, quote: Quote, day_state: "DayState") -> RiskVerdict: ...

# ---- Strategy binding (a strategy + its own schedule + its own universe) -----
@dataclass(frozen=True)
class StrategyBinding:
    strategy_id: str                 # unique id, e.g. "momentum"
    strategy_name: str               # resolved via StrategyRegistry
    params: dict                     # strategy constructor params
    universe: Sequence[str]          # this strategy's tickers
    slots: Sequence["SlotSpec"]      # this strategy's own schedule (times + drift policy)
    enabled: bool = True
    risk_overrides: Optional[dict] = None   # optional per-strategy risk limits

# ---- Scheduler --------------------------------------------------------------
@dataclass(frozen=True)
class TriggerSlot:
    strategy_id: str        # the strategy this trigger is bound to (orchestrator dispatches it)
    slot_id: str
    fire_ts: datetime       # scheduled local time + realized drift, tz-aware
    drift_seconds: int
    seed: Optional[int]

class Scheduler(Protocol):
    # Returns the MERGED, time-sorted triggers across ALL enabled strategy bindings for the date.
    # Used identically live (registered as APScheduler jobs) and in backtest (walked in order).
    def triggers_for(self, on_date) -> Sequence[TriggerSlot]: ...
```

---

## 6. Strategy interface (the pluggable calculation)

The "calculation" the user will refine later lives entirely behind `Strategy.decide(...)`. It is a **pure function of injected inputs** so it behaves identically in live and backtest.

**Inputs it receives:**

- `snapshot.quotes` — current per-ticker quotes (last/bid/ask/volume/prev_close) as of the trigger instant.
- `positions` — current holdings (symbol, signed quantity, avg price, market value).
- `account` — cash, buying power, equity.
- `data: MarketDataProvider` — to pull additional history **bounded by `asof`** (e.g. trailing N daily bars for a moving average) without risking lookahead.
- `clock` — read-only time (never `datetime.now()`).

**Output:** a list of `Decision(action, symbol, quantity, limit_price?, rationale)`. Sizing/risk happen *after* in the orchestrator + risk gate, so the strategy can express intent simply.

**Trivial placeholder example (to be replaced):**

```python
class ThresholdStrategy:
    """Stub. Buys when last < prev_close*(1-band), sells when last > prev_close*(1+band)."""
    def __init__(self, band: float = 0.02, lot: int = 10): ...
    def decide(self, snapshot, positions, account, data, clock):
        out = []
        for sym, q in snapshot.quotes.items():
            if q.prev_close is None:
                continue
            if q.last < q.prev_close * Decimal(1 - self.band):
                out.append(Decision("BUY", sym, self.lot, rationale="dip"))
            elif q.last > q.prev_close * Decimal(1 + self.band):
                out.append(Decision("SELL", sym, self.lot, rationale="pop"))
            else:
                out.append(Decision("HOLD", sym, 0))
        return out
```

A `StrategyRegistry` maps strategy **names** → classes; **no strategy logic is hardcoded in the engine**.

### 6.1 Multiple strategies, each on its own schedule

The program runs a list of **strategy bindings** (`StrategyBinding`, see §5 and the config in §7.1/§11). Each binding is an independent unit with:

- a unique `strategy_id` (used for trigger dispatch, order attribution, per-strategy risk and P&L),
- a `strategy_name` + `params` (resolved/constructed via the registry),
- its **own `universe`** of tickers (the `snapshot.quotes` passed to that strategy contains only its universe), and
- its **own `slots`** (times + drift policy) — so e.g. a "momentum" strategy can fire at 09:45/12:30/15:30 while a "mean-reversion" strategy fires at 10:15/14:00, fully independently.

At each trigger the scheduler emits a `TriggerSlot` tagged with the `strategy_id`; the orchestrator looks up `registry[strategy_id]`, builds the snapshot from that binding's universe, and runs `decide(...)`. Adding or rescheduling a strategy is a config change (binding entry) plus, if it's a new calculation, one class. Two strategies may legitimately want to trade the same ticker — see §10 (cross-strategy attribution & conflict policy) and Appendix C.

---

## 7. Scheduler & jitter design

### 7.1 Config schema (per-strategy schedules; explicit times + tz, not raw cron)

Raw cron cannot express bounded per-slot jitter, trading-calendar gating, drift direction, or seeding, and is ambiguous across DST. Use explicit local times. **Each strategy owns its own slots**; shared scheduling concerns (timezone, calendar, seed, catch-up) live in a global `schedule` block and are inherited by every strategy:

```yaml
schedule:                            # global scheduling defaults (shared by all strategies)
  timezone: America/New_York         # IANA zone (carries DST), via stdlib zoneinfo
  market_calendar: XNYS              # exchange_calendars code
  base_seed: null                    # null => OS entropy (live); int => reproducible (backtest)
  catch_up: false                    # default: skip a missed slot, don't fire it late
  misfire_grace_seconds: 120

strategies:                          # the list of strategy bindings (see §6.1)
  - id: momentum                     # unique strategy_id (dispatch + attribution + per-strat risk)
    name: threshold                  # resolved via StrategyRegistry
    enabled: true
    params: { band: 0.02, lot: 10 }
    universe: [AAPL, MSFT]           # this strategy's tickers
    slots:                           # this strategy's OWN schedule (~3×/day here)
      - { id: morning, time: "09:45", drift_max_minutes: 30, drift_direction: forward, distribution: uniform, on_overshoot: clamp }
      - { id: midday,  time: "12:30", drift_max_minutes: 30, drift_direction: forward, distribution: uniform, on_overshoot: clamp }
      - { id: close,   time: "15:30", drift_max_minutes: 30, drift_direction: forward, distribution: uniform, on_overshoot: clamp }
    risk_overrides: { max_order_notional_usd: 3000 }   # optional; merged over global risk

  - id: meanrev                      # a second, independently-scheduled strategy
    name: zscore_revert
    enabled: true
    params: { lookback: 20, z_entry: 2.0 }
    universe: [SPY, QQQ]
    slots:                           # different times from "momentum"
      - { id: am, time: "10:15", drift_max_minutes: 20, drift_direction: forward, distribution: uniform, on_overshoot: clamp }
      - { id: pm, time: "14:00", drift_max_minutes: 20, drift_direction: forward, distribution: uniform, on_overshoot: clamp }
```

At startup each `(strategy_id, slot)` parses into an APScheduler `CronTrigger(hour=, minute=, timezone=ZoneInfo("America/New_York"))` whose callback is bound to that `strategy_id`. "~3×/day" is just a per-strategy default; both the strategy list and each strategy's slot list are fully configurable. A strategy with `enabled: false` registers no triggers.

### 7.2 Jitter / random drift

- `fire_ts = scheduled_local_time + drift`, where `drift = round(rng.uniform(lo, hi))` seconds.
- **Direction** (configurable): `forward` `[0, +max]` **(default — never act before a window opens)**, `symmetric` `[-max, +max]`, or `backward` `[-max, 0]`.
- **Max** configurable (default 30 min), enforced ≤ a hard ceiling.
- **Distribution** configurable: `uniform` (default; maximal timing entropy), optionally `truncnorm`/`triangular` (cluster near nominal time).
- **Seeded RNG, isolated:** a dedicated `numpy.random.default_rng(seed)` (or `random.Random`), **never the global RNG**. Per-slot seed derived from stable inputs **including the strategy id** so each strategy's drift is independent and reproducible: `seed = stable_hash(base_seed, slot_date.isoformat(), strategy_id, slot_id)`. → In **backtest** (`base_seed` set) the same day/strategy/slot re-derives the identical drift (bit-reproducible). In **live** (`base_seed=null`) it's seeded from OS entropy for fresh unpredictability. The realized `drift_seconds` + seed are persisted per trigger so any live day can be replayed.

This is the single jitter module used identically in live and backtest — satisfying requirement 3 *and* parity.

### 7.3 Trading-calendar awareness

Use `exchange_calendars` (`XNYS`) to gate every fire:

- Whole-day closure (weekend/holiday) → **skip** the slot (+ alert).
- Early-close half day where `fire_ts > market_close` (e.g. forward drift off a 15:30 slot overshoots a 13:00 close) → apply `on_overshoot`: **clamp** to `market_close − epsilon` (default) or **skip**.
- With symmetric drift, also guard the early edge (`fire_ts ≥ market_open + epsilon`).
- Validate at both **planning time** and **fire time** (day status can change). Allow a manual holiday-override list for ad-hoc closures the library lags on.

### 7.4 Runtime model: daemon in a container

**Recommended:** a persistent APScheduler daemon (in-process drift + calendar + warm connections + dynamic reschedule) running as **PID 1 in a Docker container**, with the **Docker engine as the supervisor** for true 24×7 liveness — `restart: unless-stopped` in docker compose (replaces systemd `Restart=always`; see §16). APScheduler config: small `misfire_grace_time`, `coalesce=True`, **`max_instances=1` per (strategy_id, slot)** job, persistent jobstore. (Pure OS-cron/timer short-lived processes were considered but can't natively do jitter or calendar gating and have DST edge cases — rejected for the trigger logic.) Because multiple strategies can fire at overlapping drifted times, per-job `max_instances=1` is *not* sufficient for cross-strategy safety — a **single global cycle lock** serializes the actual decision→risk→execute step (see §7.5).

### 7.5 Exactly-once, concurrency & missed-trigger handling

- **Durable fired-slot ledger** keyed `(slot_date, strategy_id, slot_id)` with a UNIQUE constraint. Fire path: `BEGIN; INSERT(...status='claimed')` — if the unique insert fails, this strategy's slot already fired today → **abort, no double fire**; else do the work; `UPDATE status='done'`. This — not the scheduler — is the real exactly-once guarantee, surviving crashes, restarts, and accidental double-scheduling. Keying by `strategy_id` means each strategy fires its own slot exactly once per day, independently.
- **Cross-strategy concurrency (the multi-strategy safety rule):** two strategies can resolve to overlapping drifted fire times. The orchestrator's decision→risk→execute critical section runs under a **single global cycle lock** (an in-process mutex/asyncio lock, backed by a row-level DB lock for defense in depth) so account state (positions, buying power, daily counters, daily loss) is read-modify-written **atomically** for one strategy at a time. Without this, two near-simultaneous fires could both pass the risk gate against the same stale balance and over-trade. Holding the lock for the (short) cycle is fine at ~3×/day per strategy; if a fire can't acquire it within a grace window, it queues then re-checks the calendar/ledger before running.
- **Missed triggers:** default `catch_up=false` — if the machine slept/crashed past a slot by more than the grace window, **skip** (a stale market action is usually worse than a missed one); the next day's slot is the natural recovery. Configurable per slot. **Always alert on a skip** so missed triggers are visible.
- Persist `planned_fire_ts` as tz-aware UTC; rely on the host clock being NTP-synced (the container inherits it); add explicit DST spring-forward/fall-back tests (daytime equity slots sit clear of the 01:00–03:00 DST hours, so the residual risk is APScheduler's own date math — see risks).

---

## 8. Charles Schwab integration

> All endpoint paths, token TTLs, rate limits and response behaviors in this section are **[VERIFY]** against the live Schwab developer portal. They are isolated inside the `SchwabBroker` + `SchwabMarketData` adapters.

### 8.1 Manual registration (one-time, done by the user)

1. Have a funded Schwab brokerage account.
2. Create a developer account at `developer.schwab.com`.
3. Register an app; request the API products: **Accounts and Trading – Production** and **Market Data – Production**.
4. Wait for the app to move from *Approved – Pending* to *Ready For Use* (minutes to several days — out of our control).
5. Record the **App Key** (client_id) and **Secret**.
6. Configure the **callback/redirect URL** — must be **HTTPS** (TLS required even for loopback); a common individual-dev pattern is `https://127.0.0.1:8182` with a self-signed cert. The `redirect_uri` used in code must exactly match the registered one. **[VERIFY]**

### 8.2 OAuth & token lifecycle (the dominant operational constraint)

- **Flow:** three-legged authorization-code grant. Redirect user to `https://api.schwabapi.com/v1/oauth/authorize?client_id=...&redirect_uri=...` → browser login & account-link approval → callback returns single-use `code` (valid ~30s) → `POST https://api.schwabapi.com/v1/oauth/token` (grant_type=authorization_code, `Authorization: Basic base64(key:secret)`) → `access_token` + `refresh_token`. The first leg **requires a human browser session** (cannot be fully headless). **[VERIFY]**
- **Access token:** ~30 min TTL; auto-refreshed via `grant_type=refresh_token`. Our first-party client does this transparently in its HTTP layer (§8.7). **[VERIFY]**
- **Refresh token: hard 7-day cap, NOT programmatically renewable. [VERIFY — but treat as true].** After 7 days the only recovery is repeating the interactive browser login. **This makes truly unattended 24×7 impossible and is the #1 operational risk.**

**Design response (first-class, not an edge case):**

1. Persist tokens + their issue/expiry timestamps in SQLite.
2. Auto-refresh the access token before expiry.
3. **Proactively alert** the operator 1–2 days before refresh-token expiry, via the redundant channels, with a one-command/one-click link to perform the interactive re-auth. (We track the refresh-token issue time ourselves and compute its age — mirroring the token-age detection schwab-py added, but in our own code.)
4. **Graceful degradation:** on `401/invalid_token`, attempt refresh; if the refresh token is dead, flip the system into **READ-ONLY / no-new-orders safe mode**, leave existing positions per policy (do **not** auto-flatten by default), and alert loudly — never crash-loop, never silently stop.
5. A **weekly re-auth runbook** (§16) names the human operator and channel.

### 8.3 No sandbox → simulated broker is mandatory

Schwab provides **no developer sandbox / paper-trading API** — every authenticated call hits the real account with real money. (thinkorswim paperMoney is a GUI with no public API.) Therefore the `SimBroker` (which also powers backtests) is the required testing surface, and live order placement is gated behind an explicit, hard-to-trip flag (§10). **[VERIFY availability — but assume none.]**

### 8.4 Market data

- Quotes: `GET /marketdata/v1/quotes?symbols=AAPL,MSFT&fields=quote` (batched) or `/marketdata/v1/{symbol}/quotes`. **Real-time vs delayed depends on the account's market-data agreements** — verify entitlement before trusting prices for execution. **[VERIFY]**
- Price history (candles, for backtest sourcing): `GET /marketdata/v1/pricehistory?symbol=&periodType=&period=&frequencyType=&frequency=&startDate=&endDate=` → candles `{datetime(epoch ms), o,h,l,c,v}`. **Daily** history goes back years; **intraday/minute** lookback is limited (legacy TDA was ~6 months, frequencies 1/5/10/15/30) and Schwab's exact retention is undocumented — **test empirically** before planning minute-bar backtests. **[VERIFY]**
- **Streaming:** Schwab has a WebSocket streamer (auth via `/trader/v1/userPreference`). Prefer it over REST polling for live quotes to stay well under rate limits. **[VERIFY]**

### 8.5 Accounts & order placement

- Resolve hashed account id at startup: `GET /trader/v1/accounts/accountNumbers` (raw number → hashed id). **All trading calls use the hashed id**, never the raw number. **[VERIFY]**
- Place order: `POST /trader/v1/accounts/{hashed}/orders` with e.g.
  ```json
  {"orderType":"MARKET","session":"NORMAL","duration":"DAY","orderStrategyType":"SINGLE",
   "orderLegCollection":[{"instruction":"BUY","quantity":10,
     "instrument":{"symbol":"AAPL","assetType":"EQUITY"}}]}
  ```
  Limit adds `"orderType":"LIMIT","price":"123.45"`. Success returns **HTTP 201 with the new order id in the `Location` header** (not the body) — read it from there, then poll. **[VERIFY]**
- Status/replace/cancel: `GET/PUT/DELETE /trader/v1/accounts/{hashed}/orders[/{id}]`; statuses include `WORKING, FILLED, PARTIAL_FILL, CANCELED, REJECTED, EXPIRED`. Don't assume synchronous fills — poll. **[VERIFY]**

### 8.6 Idempotency, rate limits, retries

- **Idempotency:** generate `client_order_id` and persist the order as `pending` **before** the network call (write-ahead). On timeout/unknown response, **reconcile first** (query order status) before any re-send; reuse the same id. **[VERIFY whether Schwab accepts/echoes a client-supplied order id; if not, dedupe by capturing the `Location` order id at submit and querying status.]** This is the highest-severity correctness concern (a naive retry can double a real position).
- **Rate limits:** plan around ~120 req/min per app (returns 429 over) — **[VERIFY; treat as a planning ceiling, not a guarantee].** Implement a centralized token-bucket limiter + exponential backoff with jitter on 429/5xx; prefer streaming over polling.
- **First-party client (no third-party broker SDK):** we own all the client code, so a Schwab API change is something we track and fix ourselves rather than waiting on an unofficial library; the whole client sits behind our `Broker` interface so changes stay contained. Security rationale & parity approach in §8.7.

---

### 8.7 First-party Schwab client (security rationale & parity)

**Why in-house (no third-party broker SDK).** The Schwab client is the one component that holds our OAuth app secret + refresh token and that places real-money orders. Importing an unofficial, community-maintained library here means trusting unaudited code — and its transitive dependencies — with credential handling and order placement. A supply-chain compromise (malicious release, typosquat, dependency confusion, or an unnoticed bug) could exfiltrate tokens or alter/insert orders. These clients are also unofficial with no SLA. We therefore build a **minimal, fully-owned, auditable client** and keep its dependency surface tiny and vetted.

**What we build (scope):**

- OAuth 2.0 authorization-code flow + token exchange (in `auth/`).
- Token store + automatic access-token refresh + 7-day refresh-token age tracking & alerting.
- A thin HTTP layer over **one** well-known dependency (`httpx` recommended, or `requests`): centralized token-bucket rate limiting (~120/min), exponential backoff + jitter on 429/5xx, auth-header injection, `401 → refresh → retry`, and structured request/response logging with **token scrubbing**.
- Typed request/response models for only the endpoints we use: quotes, pricehistory, accounts/accountNumbers (hashed id resolution), positions/balances, place/replace/cancel order, order/transaction status.
- (Later, optional) the WebSocket streaming client — polling first.

**How we reach parity without taking the dependency.** Treat `schwab-py` (MIT) and `Schwabdev` (MIT) as a **functional specification and test oracle**, not a runtime dependency: read them to confirm exact endpoint paths, query params, the OAuth/refresh sequence, order JSON shapes, the 201/`Location` behavior, and status enums — then **write our own implementation** and verify it against real Schwab responses. We do **not** copy their source or vendor their packages; even though MIT permits reuse, we want clean-room, owned code. The endpoint/payload details in §8.1–8.6 are our parity checklist, and all of them remain **[VERIFY]** against the live portal.

**Security controls on the client:** pin + hash-lock the few dependencies; isolate all credential reads to the secrets component (§13); scrub tokens from logs; treat the client as security-sensitive code that requires review (a milestone exit criterion, §17); and keep it behind the `Broker` interface so the rest of the system is insulated from client internals.

## 9. Backtesting design

The backtest is the historical-data test harness and the embodiment of requirement 5.

### 9.1 Parity mechanism

The backtest reuses the **same orchestrator, strategy, sizing, and risk gate**. Only three injected things change:

| Component | Live | Backtest |
|---|---|---|
| `Clock` | wall-clock | `VirtualClock` advanced to each `fire_ts` |
| `MarketDataProvider` | Schwab (live quotes/streaming) | `HistoricalDataProvider` (asof-bound reads from Parquet cache) |
| `Broker` | `SchwabBroker` | `SimBroker` (models fills/slippage/fees/partials) |

Event-driven engine (not vectorized): a loop advances the virtual clock to each drifted trigger timestamp and runs one cycle — mirroring live exactly and naturally preventing lookahead. (Vectorized libs like vectorbt are kept only as an optional offline parameter-sweep tool, never the parity path.)

### 9.2 No-lookahead / no-survivorship discipline

- Every data read takes `asof` bound to the clock and returns **only rows with `ts ≤ asof − latency`**. Strategies never call `.shift(-1)` or full-series forward-rolling.
- Decisions on a quote execute on the **next** available quote/bar, never the same instant (signal-to-execution latency).
- Key instruments by a stable security id, not ticker (tickers get reused). For long-horizon backtests, prefer a **delisted-inclusive** dataset; **flag explicitly** that free sources (yfinance/stooq) lack delisted names and bias results.

### 9.3 SimBroker fill model (configurable, calibratable)

- **Commissions/fees:** Schwab equities are $0 commission, but model SEC/TAF/regulatory fees so backtest P&L matches live.
- **Slippage:** fixed bps / fixed cash / volume-or-volatility-proportional (configurable). Conservative default: fill market orders at next quote/bar ± half-spread + slippage.
- **Fill timing:** never at the signal instant; next available quote/bar.
- **Partial fills:** cap fill qty at a fraction of bar volume (e.g. ≤1–10% ADV); carry the remainder as a working order. Limit orders fill only if the bar's range crosses the limit.
- These are *assumptions*; **calibrate against real Schwab fills once live** and note that mis-calibration is the main residual backtest/live divergence even with identical decision code.

### 9.4 Historical data sourcing & caching

- **Preferred for parity:** pull from the same vendor you trade through — Schwab `/pricehistory` (daily = deep history; minute = limited, verify).
- **Prototyping:** yfinance (free, unstable, no delisted, intraday ≤~60 days) / stooq (daily CSV) / Alpha Vantage (tight free limits).
- **Serious long-horizon:** a paid delisted-inclusive vendor (Polygon, Norgate, QuantConnect/CRSP).
- **Cache:** partitioned **Parquet** (by symbol/date) + a small **SQLite/DuckDB catalog** tracking cached ranges, ingest timestamps, and content hashes. The `HistoricalDataProvider` checks the cache, append-only-fetches missing ranges, then serves from disk → repeat backtests are fast, offline, and reproducible.

### 9.5 Determinism & reproducibility

- Snapshot a single config object (params, dates, seed, fee/slippage model, data + library versions, git commit) and emit a **run manifest** with `config_hash` + `data_hash` so any result is re-derivable.
- Seed every RNG via explicit Generator instances (including scheduler jitter); record seeds.
- Pin dependencies (lockfile); compute in UTC; avoid nondeterministic float reductions and dict-order-dependent sums.
- (Optional research hygiene) track number of trials to estimate overfitting / deflated metrics (Bailey & López de Prado).

### 9.6 Backtest report outputs

Equity curve, trade blotter (with rationale per decision), realized/unrealized P&L, max drawdown, hit rate, turnover, exposure, per-slot fire log (with realized drift) — plus the run manifest. Same audit schema as live, so live and backtest results are directly comparable.

---

## 10. Risk management & safety

The risk gate is a **single, non-bypassable, fail-closed** function: `Order in → Verdict out`. Every path to the broker traverses it. On missing/uncertain data it **rejects**.

**Default-safe posture**

- **Default mode = `paper`/dry-run.** Going live requires **two** signals: `mode: live` in config **plus** an env var / CLI confirmation. Live state is logged and alerted at startup so it is never silent.
- **Kill switch:** persisted flag (survives restarts), checked at the start of every cycle and immediately before every submit; manual CLI to flip; auto-trips on daily-loss breach, repeated broker errors, stale-data, or reconciliation mismatch. On trip: halt new orders + alert. **Auto-flatten is OFF by default** (flattening in disorderly markets is itself risky); it's an explicit opt-in.

**Per-order / per-day rails (all config-driven, evaluated on the *resulting* position, not the order in isolation)**

- `max_position_size` (shares or % equity per symbol)
- `max_order_notional` (USD/order)
- `max_gross_exposure` (Σ|positions|)
- `daily_loss_limit` (USD or % of start-of-day equity, vs a persisted open snapshot)
- `max_trades_per_day` (persisted counter, reset at session start)
- **Allowlist** (default-deny: only trade listed symbols) and/or denylist
- **Price sanity:** reject zero/negative/NaN; reject quotes older than `max_staleness_seconds`; reject if spread % exceeds a bound (illiquid/halted); optionally reject if price deviates from prev close beyond a band (bad ticks). Halted/locked symbols → no-trade.
- **Duplicate-order guard** via the client-order-id idempotency pattern + pre-resend reconciliation.

**Cross-strategy scope, attribution & conflicts (multi-strategy):**

- **Two limit scopes.** *Account-wide* limits (`max_gross_exposure`, `daily_loss_limit`, `max_trades_per_day`, buying-power) are enforced across **all** strategies combined and are the hard guardrail. *Per-strategy* limits (via a binding's `risk_overrides`, e.g. `max_order_notional`, `max_position_size`, per-strategy trade count / loss budget) are checked first and merged over the global defaults. An order must pass **both** scopes. The global cycle lock (§7.5) guarantees these account-wide checks see a consistent, serialized view of state.
- **Position attribution.** Schwab holds one commingled position per symbol (broker truth). Locally we additionally track an **attributed sub-position per `strategy_id`** (from fills tagged with the originating strategy) for per-strategy P&L, position caps, and reporting. The account-level reconciliation (below) always trues up to broker totals; attribution is a local ledger whose per-strategy sum is reconciled to the broker total (any unattributed delta is parked in a `manual/unknown` bucket and alerted).
- **Same-ticker conflict policy** (config `risk.conflict_policy`, default **`net`**): when two strategies trade the same symbol in one session —
  - `net` (default): decisions are netted at the account level before submission (strategy A's +10 AAPL and strategy B's −4 AAPL → one +6 order), so we never cross our own spread; attribution splits the fill back to each strategy pro-rata.
  - `independent`: each strategy's order is sent on its own (simpler attribution, but can self-trade/churn).
  - `priority`: a configured strategy ordering wins ties; lower-priority conflicting orders on the same symbol in the same cycle are dropped + logged.
  This is an explicit open decision (see §18); `net` is the safe, cost-minimizing default.

**Reconciliation:** on startup, after each submit, and at EOD — pull authoritative positions/orders from Schwab and diff against local intent (both the account total and the sum of per-strategy attributed positions). Broker = source of truth for positions/fills; local = source of truth for intent. Unexplained divergence → update to broker truth, log, and consider tripping the kill switch.

**PDT / regulatory:** implement a **configurable** pattern-day-trader check (rolling 5-day day-trade count, $25k equity threshold) in the risk layer. **[VERIFY]** A 2026 SEC-approved amendment to FINRA Rule 4210 may change the day-trade-counting regime — do **not** hardcode the old thresholds; make them config + verify current rules. Note margin (PDT) vs cash (T+1 settlement / good-faith violations) account choice changes which constraints apply.

---

## 11. Configuration

Layered precedence: **defaults < config file (YAML) < environment variables < CLI overrides**, merged then **pydantic-validated** (range checks, e.g. `daily_loss_limit > 0`, `drift_max_minutes ≤ ceiling`). Strategy params are separated from system params. The **same validated config drives live and backtest** — only the injected broker/data/clock differ.

```yaml
mode: paper                 # paper | live | backtest  (default paper; live needs extra confirmation)

account:
  broker: schwab
  account_ref: primary      # maps to hashed account id resolved at runtime
  secrets_ref: keychain     # where to read credentials (see §13); never inline

schedule:                   # GLOBAL scheduling defaults, inherited by every strategy (see §7.1)
  timezone: America/New_York
  market_calendar: XNYS
  base_seed: null           # int for reproducible backtest; null => entropy (live)
  catch_up: false
  misfire_grace_seconds: 120

strategies:                 # MULTIPLE strategies, each with its own universe + schedule (§6.1, §7.1)
  - id: momentum
    name: threshold         # resolved via StrategyRegistry
    enabled: true
    params: { band: 0.02, lot: 10 }
    universe: [AAPL, MSFT]
    slots:
      - { id: morning, time: "09:45", drift_max_minutes: 30, drift_direction: forward, distribution: uniform, on_overshoot: clamp }
      - { id: midday,  time: "12:30", drift_max_minutes: 30, drift_direction: forward, distribution: uniform, on_overshoot: clamp }
      - { id: close,   time: "15:30", drift_max_minutes: 30, drift_direction: forward, distribution: uniform, on_overshoot: clamp }
    risk_overrides: { max_order_notional_usd: 3000 }   # optional; merged over global risk

  - id: meanrev
    name: zscore_revert
    enabled: true
    params: { lookback: 20, z_entry: 2.0 }
    universe: [SPY, QQQ]
    slots:
      - { id: am, time: "10:15", drift_max_minutes: 20, drift_direction: forward, distribution: uniform, on_overshoot: clamp }
      - { id: pm, time: "14:00", drift_max_minutes: 20, drift_direction: forward, distribution: uniform, on_overshoot: clamp }

risk:                       # ACCOUNT-WIDE limits (apply across all strategies combined; see §10)
  max_position_size_pct: 10
  max_order_notional_usd: 5000
  max_gross_exposure_usd: 25000
  daily_loss_limit_pct: 2
  max_trades_per_day: 6
  max_staleness_seconds: 60
  max_spread_pct: 1.0
  allowlist: [AAPL, MSFT, SPY, QQQ]
  enforce_pdt: true
  auto_flatten_on_kill: false
  conflict_policy: net      # net | independent | priority  (same-ticker across strategies; §10)

execution:
  order_type: market        # or limit
  poll_timeout_seconds: 60
  rate_limit_per_min: 100    # under the ~120 ceiling

backtest:
  start: "2022-01-01"
  end: "2024-12-31"
  data_vendor: schwab        # schwab | yfinance | csv | polygon ...
  fees_model: { commission: 0, regulatory_bps: 0.2 }
  slippage_model: { type: bps, value: 2 }
  base_seed: 12345

alerting:
  channels: [telegram, email]
  heartbeat_minutes: 60

observability:
  log_format: json
  db_path: /state/trader.sqlite    # container path; mapped to a Docker volume (see §16)
  data_cache: /data/               # container path; mapped to a Docker volume (see §16)
```

> Paths like `/state` and `/data` are **container mount points** backed by Docker volumes so state and the data cache survive container recreation (§16). The config file itself is mounted read-only at `/config/config.yaml`.

---

## 12. State, persistence & observability

**Durable state (SQLite, WAL mode):**

- OAuth access + refresh tokens and expiry timestamps
- Current positions snapshot — **account-level (broker truth) + per-`strategy_id` attributed sub-positions**
- Full order history: intent, `client_order_id`, **`strategy_id`**, broker id, status transitions, fills, fees
- Decision/trigger audit log (every cycle's `strategy_id`, inputs → signal → sized order → risk verdict → submission → fill, with a correlation/cycle id)
- Start-of-day equity; realized/unrealized P&L — **combined and per strategy**
- Daily counters (trades today, loss today) — **account-wide and per strategy**
- Kill-switch flag (global; optionally a per-strategy disable flag)
- Fired-slot ledger `(slot_date, strategy_id, slot_id)` with realized drift + seed
- Strategy registry/binding snapshot per run (so audit rows resolve to the exact params used)
- **Web UI (§19):** the web service writes **nothing** to this DB — it opens the state DB read-only for monitoring. Sessions are signed stateless cookies; login/access events go to the web service's **own** logs (not the trading DB). No control or config-override tables exist.

**Bulk historical bars/quotes:** Parquet (+ DuckDB/SQLite catalog). Config is human-edited YAML/TOML only — never mutable state in flat JSON (partial writes corrupt).

**Observability:**

- **Structured JSON logs** (structlog) — queryable; append-only + rotated.
- **Audit trail** persisted in SQLite in addition to logs (full per-decision chain).
- **Metrics:** orders placed/filled/rejected, P&L, latency, error counts, data staleness, **token-expiry countdown**.
- **Alerting** (Telegram + email, ≥2 channels so one failure isn't silent) on: unhandled exceptions/crashes, broker/auth errors, kill-switch trips, daily-loss breaches, reconciliation mismatches, stale-data halts, skipped slots, and the **weekly token re-auth reminder**. Plus a **heartbeat/"still alive" ping** to catch silent death.

---

## 13. Security & secrets

- **Never** commit client id/secret or tokens. Add `.env`, token files, `*.sqlite`, and `data/` to `.gitignore`.
- Storage, best → acceptable: **OS keychain** (macOS Keychain / Windows Credential Manager / libsecret via Python `keyring`) for client secret + tokens; **OR** an encrypted file (age/sops, or Fernet with a key from env/keychain); **OR** at minimum a gitignored `.env` with `chmod 600`.
- Keep the static client id/secret **separate** from the rotating OAuth tokens.
- A single **secrets component** is the only code that reads credentials; everything else receives them via injection.
- Scrub tokens from logs.
- **Minimal, audited dependencies (supply-chain):** no third-party broker SDK touches credentials or orders — the Schwab client is first-party (§8.7). Prefer a single, well-known HTTP library; **pin + hash-lock** all dependencies; review the credential-handling code; watch for typosquat / dependency-confusion on the (few) deps we do use.
- **Web UI hardening (§19):** the UI is **read-only** (no write path to trading state or config), so its attack surface is just authenticated reads. TLS-only behind a reverse proxy; single-admin login with an **argon2id** password **hash** (never plaintext; stored via the secrets layer); httpOnly/Secure/SameSite session cookies + CSRF on the login/logout POST; login rate-limit/lockout; never render OAuth tokens or the app secret in the UI/API; expose only the proxy port (ideally behind a VPN/IP allowlist); log auth events. The UI cannot place orders or change config; going live and all operational changes remain a deliberate CLI/config action.

---

## 14. Project layout

```
trade/
├── plan/
│   └── design.md                  # this document
├── pyproject.toml                 # deps + lockfile (Poetry/uv)
├── config/
│   ├── default.yaml
│   └── live.yaml                  # gitignored if it contains anything sensitive
├── src/trader/
│   ├── config/                    # pydantic models + layered loader
│   ├── core/                      # interfaces: Clock, MarketDataProvider, Broker, Strategy, RiskManager, types
│   ├── clock/                     # RealClock, VirtualClock
│   ├── scheduler/                 # slot generation, jitter (seeded RNG), calendar gating, slot ledger
│   ├── data/                      # SchwabMarketData, HistoricalDataProvider, Parquet cache + catalog
│   ├── broker/                    # Broker impls: SchwabBroker, SimBroker, FakeBroker
│   ├── schwab/                    # FIRST-PARTY Schwab client: http (rate-limit+retry), endpoints, typed models, streaming
│   ├── strategy/                  # StrategyRegistry + bindings loader + strategies/ (threshold.py, zscore_revert.py, ...)
│   ├── sizing/                    # Decision -> Order
│   ├── risk/                      # RiskManager gate + individual rules
│   ├── orchestrator/              # run_cycle (shared live & backtest)
│   ├── execution/                 # order submit/poll, idempotency, reconciliation
│   ├── state/                     # SQLite repositories, migrations
│   ├── observability/            # logging, metrics, audit, alerting (telegram/email/heartbeat)
│   ├── auth/                      # OAuth authorization-code flow + token store/refresh + re-auth alerting (part of first-party client)
│   ├── backtest/                  # event-driven engine + report
│   ├── web/                       # SEPARATE web service: FastAPI app, auth (session+argon2+CSRF), READ-ONLY routes/api, templates/ + static/ (opens state DB read-only; no write path; never trades)
│   └── app/                       # daemon entrypoint, CLI (run/backtest/reauth/kill/status); `web` entrypoint serves the UI
├── tests/
│   ├── unit/                      # strategy (synthetic data), risk rules, jitter, calendar
│   ├── integration/               # FakeBroker: retries, timeouts, partials, idempotency, reconcile
│   └── backtest/                  # golden-run reproducibility
├── data/                          # Parquet cache (gitignored; mounted as a Docker volume)
├── state/                         # SQLite db + tokens (gitignored; mounted as a Docker volume)
└── deploy/
    ├── Dockerfile                 # slim python base + tzdata; installs the package; non-root user
    ├── docker-compose.yml         # services: trader, web, reverse-proxy; volumes, env_file, restart, healthcheck, limits
    ├── .dockerignore
    ├── entrypoint.sh              # exec `trader run` as PID 1 (signal-forwarding)
    ├── Caddyfile                  # reverse proxy: TLS termination in front of the web service (§16.6)
    └── supervisors/               # optional systemd unit / launchd plist for non-container hosts
```

---

## 15. Testing strategy

The backtest is the historical-data test harness, but it sits in a wider pyramid:

1. **Unit — strategy on synthetic/handcrafted data:** deterministic signals for known inputs; edge cases (gaps, zero volume, missing prev_close).
2. **Unit — risk gate (highest value for real money):** each rail rejects/clamps correctly; fail-closed on missing data; resulting-position math.
3. **Unit — scheduler/jitter/calendar:** seeded drift is reproducible; forward/symmetric bounds; clamp/skip on overshoot; DST spring-forward & fall-back days; holiday & half-day handling.
4. **Integration — FakeBroker (same `Broker` interface):** retries, timeouts, unknown responses, partial fills, **duplicate-submit idempotency** (same client id never double-fills), reconciliation mismatches → kill-switch.
5. **Contract — first-party Schwab client (recorded/mocked HTTP):** against recorded Schwab responses (and a local mock server), verify OAuth token exchange + auto-refresh, `401 → refresh → retry`, 429 backoff + rate limiting, hashed-account resolution, order-payload serialization, 201/`Location`-header parsing, status-enum mapping, and token scrubbing in logs. No live calls in CI.
6. **Property/fuzz:** idempotency invariant under arbitrary retry/crash interleavings.
7. **Backtest golden runs:** a fixed config + cached data snapshot must reproduce a recorded equity curve bit-for-bit (guards against accidental lookahead/non-determinism regressions).
8. **Paper soak:** a multi-week live `paper` run against real Schwab market data before any real money — exercises the full pipeline (incl. the 7-day re-auth) without placing real orders.

---

## 16. Deployment & operations

**Target: a Docker image deployed via `docker compose` on a small always-on server** (cloud VPS, ~1 vCPU/1 GB, preferred over a home server/laptop for uptime/power/network and to avoid sleep silently skipping slots).

### 16.1 The image

- Slim Python base (e.g. `python:3.11-slim`); install **`tzdata`** (the daemon needs `America/New_York`) and any build deps, then the package from the pinned lockfile (reproducible deps — important for deterministic backtests).
- Run as a **non-root user**; copy only the package + entrypoint (use `.dockerignore` to keep `state/`, `data/`, `.env`, `.git` out of the build context).
- **Exec-form entrypoint** so signals (SIGTERM on `compose stop`) reach the daemon for a clean shutdown (finish/῾abort the current cycle, release the cycle lock, flush state).
- A **HEALTHCHECK** that checks daemon liveness (e.g. a heartbeat file / `trader status --healthcheck` touched each scheduler tick) so Docker can report/restart an unhealthy container.
- **No secrets baked into the image** (see §13); they come in at runtime via env/volume.

### 16.2 docker compose (illustrative)

```yaml
services:
  trader:
    build: ./deploy            # or image: trader:<pinned-tag>
    restart: unless-stopped    # Docker engine is the supervisor (replaces systemd Restart=always)
    environment:
      - TZ=America/New_York
    env_file:
      - ./secrets/.env         # SCHWAB_APP_KEY/SECRET, alert tokens — gitignored, chmod 600
    volumes:
      - ./config/config.yaml:/config/config.yaml:ro   # config in, read-only
      - trader_state:/state                            # SQLite db + OAuth tokens (durable)
      - trader_data:/data                              # Parquet history cache (durable)
    healthcheck:
      test: ["CMD", "trader", "status", "--healthcheck"]
      interval: 60s
      timeout: 10s
      retries: 3
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "5" }      # rotate; structured JSON logs
    deploy:
      resources:
        limits: { cpus: "1.0", memory: "1g" }

volumes:
  trader_state:
  trader_data:
```

- **Durability:** `/state` (SQLite incl. tokens, ledger, positions, audit) and `/data` (Parquet cache) are **named volumes** so they survive `docker compose pull && up -d` redeploys. Without this the token file and slot ledger would be lost on every recreate.
- **Time:** set `TZ` and ship `tzdata`; the container inherits the **host** clock, so keep the **host** NTP-synced (chrony/systemd-timesyncd) — clock drift breaks both fire timing and DST math.
- **Logs:** `json-file` driver with size/rotation; structured JSON lines are shippable to a log stack later.
- **Resource limits** keep the box predictable.

### 16.3 Operating it

- **Start the daemon:** `docker compose up -d`. **Update:** rebuild/pull then `docker compose up -d` (volumes persist; on boot the daemon **reconciles with Schwab before acting**, never blindly replays intents, and honors the persisted kill switch + slot ledger — restart-safe).
- **One-off commands** run in the same image against the same volumes:
  - `docker compose run --rm trader backtest --config /config/config.yaml`
  - `docker compose run --rm trader kill --on` / `--off`
  - `docker compose run --rm trader status` / `reconcile`
- **Backups:** snapshot the `trader_state` volume on a schedule (e.g. `docker run --rm -v trader_state:/v -v "$PWD":/b alpine tar czf /b/state-backup.tgz /v`), plus copy off-box.

### 16.4 Headless re-auth runbook (mandatory — the weekly Schwab gotcha)

The Schwab OAuth login is interactive and needs a **browser**, which a headless server doesn't have. The HTTPS callback also needs to be reachable. Two supported flows (pick one; **option A is simplest**):

- **Option A — auth locally, ship the token.** Run `trader reauth` on your laptop (it opens the Schwab browser login, captures the callback on `https://127.0.0.1:8182`, writes the token), then copy the resulting token file into the server's `trader_state` volume (e.g. `docker cp` / scp into the volume) and the daemon picks it up. No inbound port on the server.
- **Option B — port-forward the callback.** `ssh -L 8182:127.0.0.1:8182 server`, then `docker compose run --rm -p 8182:8182 trader reauth` and complete the login in your local browser through the tunnel.

Runbook:
1. Bot alerts **1–2 days before** refresh-token expiry (Telegram + email), with the chosen procedure linked.
2. Operator performs option A or B → new refresh token lands in the `/state` volume → daemon resumes normally.
3. **If missed:** the daemon flips to **READ-ONLY safe mode** (no new orders), keeps existing positions (no auto-liquidate by default), and keeps alerting until re-auth — it does **not** silently die.

### 16.5 CLI surface

`run` (daemon; the container default), `backtest`, `reauth`, `kill --on/--off`, `status [--healthcheck]`, `reconcile`. All accept `--config /config/config.yaml` and operate on the mounted volumes. The web UI is launched by a separate `web` entrypoint (uvicorn), not the trader daemon.

### 16.6 Web UI & reverse-proxy services (§19)

The web UI runs as **its own service in the same compose project**, isolated from the trader so UI load or a crash never affects trading. A reverse proxy terminates TLS and is the only thing exposed.

```yaml
services:
  trader:       # ... as in §16.2 (unchanged)

  web:
    build: ./deploy
    command: ["trader-web"]          # uvicorn serving the FastAPI app
    restart: unless-stopped
    environment:
      - TZ=America/New_York
    env_file:
      - ./secrets/.env               # WEB_ADMIN_USER, WEB_ADMIN_PASSWORD_HASH (argon2id), SESSION_SECRET
    volumes:
      - ./config/config.yaml:/config/config.yaml:ro
      - trader_state:/state          # SAME state volume — opened READ-ONLY (connection mode=ro); web writes nothing to it
    expose: ["8000"]                 # internal only; NOT published to the host
    networks: [internal, edge]
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/healthz"]
      interval: 60s

  proxy:
    image: caddy:2                   # auto-HTTPS (or nginx/Traefik)
    restart: unless-stopped
    ports: ["443:443"]               # the ONLY published port
    volumes:
      - ./deploy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
    networks: [edge]

networks:
  internal: { internal: true }       # trader <-> web; no outbound exposure
  edge: {}

volumes:
  trader_state:
  trader_data:
  caddy_data:
```

- **Isolation:** `trader` and `web` are separate containers; the `web` service has **no broker code path** (it never imports the Schwab client / `SchwabBroker`). The `internal` network keeps trader↔web private; only `proxy:443` is published.
- **Shared state, read-only:** `web` opens the SQLite state DB with a **read-only connection** (`mode=ro` / `PRAGMA query_only=ON`) for monitoring — WAL allows the reader alongside the daemon's writes (mount the volume RW so SQLite can use the `-wal`/`-shm` files, but the connection itself is read-only). The **trader daemon remains the sole writer of trading state and the sole caller of Schwab.** The web service performs **no writes at all** (sessions are signed cookies; its own access logs go to stdout/its own log).
- **Defense in depth:** prefer fronting `proxy` with a VPN/Tailscale or IP allowlist for a single operator; admin creds + session secret come from `env_file` (never the image).

---

## 17. Milestones / phased build plan

| Milestone | Deliverable | Exit criteria |
|---|---|---|
| **M0 — Skeleton** | Repo layout, pydantic config (layered+validated), interfaces in `core/`, SQLite schema, structured logging, CLI stub | `config` loads/validates; interfaces compile; `status` runs |
| **M1 — First-party Schwab client (read-only)** | **In-house** client built parity-checked vs schwab-py/Schwabdev (not imported): OAuth authorization-code flow, token store + auto-refresh + 7-day age alert, HTTP layer (rate-limit + retry + token scrubbing), quotes/pricehistory endpoints; recorded-HTTP contract tests | Can authenticate and fetch live quotes & daily candles for the universe; token-age alert fires in test; client contract tests pass; **security review of credential-handling code done** |
| **M2 — Backtest engine** | `VirtualClock`, `HistoricalDataProvider` (asof-bound) + Parquet cache, `SimBroker` (fees/slippage/partials), event-driven loop, report + run manifest | A trivial strategy backtests over cached history; golden run reproduces bit-for-bit |
| **M3 — Multi-strategy + scheduler** | `StrategyRegistry` + binding loader + ≥2 stub strategies; per-strategy seeded jitter + calendar gating + merged time-sorted trigger interleave; ledger keyed `(date, strategy_id, slot)`; **global cycle lock**; orchestrator `run_cycle(strategy, universe, now)` shared by both paths | **Two strategies on different schedules** both run in backtest **and** in live `paper` cycles, dispatched to the correct strategy; overlapping fires serialize correctly; per-strategy attribution in audit |
| **M4 — Paper trading + Dockerize** | Full pipeline in `paper` mode (SimBroker fills against live quotes), reconciliation (account + per-strategy), alerting, heartbeat; **Dockerfile + docker-compose with volumes + healthcheck**; runs the soak in-container | Multi-day in-container paper soak with no manual intervention except weekly re-auth; state survives container recreate |
| **M5 — Live (guarded)** | `SchwabBroker` order submit/poll, idempotency + reconciliation, full risk gate (**account-wide + per-strategy limits + conflict policy**), kill switch, double-confirm to go live | Small-size live trades match intent; risk rails + conflict netting verified; reconcile clean; **deployed via `docker compose up -d`** |
| **M6 — Refine calculation** | Real strategy/strategies behind the stable interface; parameter research (optional vectorized harness) | New strategies are pure config (binding) + class swaps; backtests reproducible per strategy and combined |
| **M7 — Web UI (read-only monitoring)** | Separate `web` service (FastAPI) + reverse-proxy TLS; password login (argon2 + sessions + CSRF); read-only dashboards (status, per-strategy decisions/positions/P&L, orders, token-expiry, alerts, config view) | Admin logs in over TLS and monitors live; UI opens the state DB read-only with **no write path** and no broker code path; a UI crash never affects the trader; tests assert no write/secret endpoints exist |

---

## 18. Open questions / decisions for the user

Grouped; each is a decision that shapes the build. Recommended defaults in **bold**.

**Strategy & data**
1. Asset universe & history horizon (few names vs broad; intraday vs daily; years)? This decides whether free data suffices or a paid delisted-inclusive vendor is needed.
2. What granularity do the ~3×/day triggers consume — **last quote at the trigger instant** vs minute bars? (Affects the data interface and fill modeling.)
3. Must backtest data come from **Schwab's price-history API for parity**, or is a separate research vendor acceptable (and tolerate divergence)?
4. Does the strategy use only price/volume, or also fundamentals (which add lookahead/restatement risk)?

**Scheduling**
5. Drift direction: **forward `[0, +30m]`** (never act early) vs symmetric? Distribution: **uniform** vs center-weighted?
6. Missed-slot policy: **skip (no catch-up)** globally, or per-slot same-session catch-up with a grace window?

**Risk & account**
7. Account type: **margin** (PDT applies) vs **cash** (T+1 / good-faith violations)? Confirm current post-2026 FINRA Rule 4210 rules.
8. Should the kill switch **only halt new entries (default)** or optionally auto-flatten?
9. Starting risk-limit values (position/notional/loss/trade-count)?

**Ops & infra**
10. Hosting: **cloud VPS** vs home server; tolerance for downtime; backup expectations.
11. Re-auth operator + alert channels (Telegram/email/SMS)? Who performs the weekly login?
12. Reproducibility scope: **bit-identical within a code version** vs stable across versions (affects pinning the seed-derivation hash).
13. Engine choice: **thin custom event-driven engine (recommended)** vs off-the-shelf (lumibot/backtrader)?

**Multi-strategy**
14. ✅ **DECIDED — same-ticker conflict policy = `net`.** Same-ticker overlap across strategies is expected to be rare for now, so the netting path is low-traffic; `net` is kept as the safe, cost-minimizing default and still correct when overlap does occur.
16. ✅ **DECIDED — strictly separate attributed sub-positions** (each strategy manages its own logical sub-portfolio; sell decisions act on that strategy's attributed shares). Broker total remains the reconciliation truth; the per-strategy ledger sums to it.
15. Risk-limit scoping: which caps are account-wide vs per-strategy, and what starting per-strategy budgets (notional / position / loss / trade-count)?
17. Runtime control: is a single global kill switch enough, or do you want **per-strategy enable/disable** without a redeploy?

**Deployment (Docker)**
18. Headless re-auth: **Option A — auth on laptop, copy token into the volume (recommended)** vs Option B — SSH port-forward the callback?
19. Secrets in compose: `env_file` (gitignored `.env`) vs Docker secrets vs a host keychain mounted in?
20. Backup target & cadence for the `trader_state` volume (and where the off-box copy lives)?
21. First-party Schwab client: HTTP library (**`httpx` recommended**) and dependency-audit/pin policy; include the WebSocket streaming client in v1 or ship polling-only first (recommended)?

**Web UI (§19)**
22. Frontend approach: **server-rendered Jinja2 + HTMX (recommended)** vs a lightweight SPA?
23. Process model: **separate `web` container sharing the state DB (recommended)** vs in-process with the daemon?
24. ✅ **DECIDED — the UI is monitoring-only (read-only).** No control or config edits via the UI; kill switch, per-strategy enable/disable, and config changes are done via the config file + CLI (and applied by the daemon). This removes the UI's write path entirely.
25. Exposure: public TLS endpoint vs **behind a VPN/Tailscale or IP allowlist (recommended)**; one admin account vs a small fixed set?

**Schwab API facts to verify before M1/M5** (all **[VERIFY]** above): exact access/refresh token TTLs and whether any non-interactive refresh renewal now exists; official rate limit (number, per-app vs per-account, separate market-data vs trading); intraday price-history retention & supported (periodType, period, frequencyType, frequency) combos; whether real-time equity quotes need a market-data agreement; precise `redirect_uri` rules; the 201/`Location`-header order behavior and current status enums; whether a client-supplied order id is accepted for idempotency.

---

## 19. Production web UI (read-only monitoring)

A password-gated, **read-only** web UI for production to monitor the strategies and the various configurables. It is designed around one overriding principle: **the UI is an observer only — it never writes to the trading system, never changes config, and never trades.** Operational changes (kill switch, enabling/disabling strategies, config edits) are made through the config file + CLI (§16), not the UI.

### 19.1 Principles & blast radius

- **Isolated service.** The UI runs as its own container (its own process), so UI traffic, bugs, or crashes cannot stall or kill the trading daemon.
- **Read-only.** The web service writes **nothing** to the trading state DB — it opens it with a read-only connection. There are no control or config-override tables. Sessions are signed stateless cookies; login/access events go to the web service's own logs, not the trading DB.
- **No order path.** The web service does not import the Schwab client or `SchwabBroker`; there is no code path from the UI to an order — or to any write at all.
- **Safe by default.** Because the UI has no write path, its attack surface is just authenticated reads. Going live and every operational change remain deliberate CLI/config actions (§10, §16).
- **TLS + auth, always; log all logins.**

### 19.2 Architecture & process model

```
 browser ──TLS──► reverse proxy (Caddy/Traefik :443, auto-HTTPS) ── only exposed port
                       │  internal compose network
                       ▼
                 web service (FastAPI / uvicorn)
                   ├─ auth: session cookie (signed) + argon2id password hash + CSRF on login
                   └─ READS (read-only) ────► SQLite state DB (WAL, mode=ro): status, positions,
                                                P&L, orders/fills, audit, token-expiry, heartbeats
                                                          ▲ (writes)
   trading daemon (separate container) ───────────────────┘ sole writer of trading state; sole Schwab
                                                            caller. The web service writes nothing.

   shared: trader_state volume (SQLite, WAL) ; trader & web on an internal network
```

**Recommended: separate container, read-only on the shared state volume.** The UI reads SQLite directly through a read-only connection (`mode=ro` / `PRAGMA query_only=ON`); WAL lets it read concurrently with the daemon's writes. It exposes only GET endpoints (plus login/logout POST) and has no code that writes the trading DB or calls the broker. *Alternative considered:* running the UI in-process with the daemon — rejected because a separate process gives crash/load isolation and a smaller, clearly read-only surface.

### 19.3 What it shows (monitoring — read-only)

- **System:** mode (paper/live/backtest), daemon health/heartbeat, market-open status, kill-switch state, and the **refresh-token expiry countdown** (the weekly re-auth reminder, surfaced prominently).
- **Schedule:** per-strategy next/last fire times, realized drift, and any skipped slots.
- **Per strategy:** enabled state, params, universe, recent **decisions** (the full audit chain: inputs → decision → risk verdict → order → fill), attributed positions + P&L, trades today vs limits.
- **Account:** positions (broker truth), cash / buying power / equity, daily P&L, daily-loss vs limit, gross exposure.
- **Orders:** recent orders/fills with status; rejections with reasons.
- **Alerts & log tail;** optional backtest-report browser (equity curves, metrics, run manifest).

### 19.4 No control actions (intentionally)

The UI performs **no** control actions — a deliberate scope decision (§18 #24) that keeps it entirely write-free. Equivalent operations are done outside the UI:

- **Kill switch:** `docker compose run --rm trader kill --on` / `--off` (persisted flag, §10).
- **Enable/disable a strategy or edit configurables** (schedule, universe, params, risk limits): edit the mounted `config.yaml` and have the daemon reload it (CLI reload command or `docker compose up -d`); all changes still pass the daemon's pydantic validation.
- **Re-auth:** the §16.4 runbook (laptop auth → copy token, or SSH port-forward).

The UI *displays* the current config, kill-switch state, token age, and re-auth status read-only — and can label/deep-link the relevant CLI command — but it never executes anything.

### 19.5 Security (see also §13)

- **TLS mandatory** via the reverse proxy (auto-HTTPS); no plain HTTP — the UI exposes login, positions, and P&L.
- **Single admin login** (or a small fixed set); password stored as an **argon2id hash** supplied via the secrets layer (`WEB_ADMIN_PASSWORD_HASH`); never plaintext, never in the repo or image.
- **Sessions:** signed, stateless `httpOnly` + `Secure` + `SameSite=strict` cookies; idle + absolute timeout; **CSRF** on the only POSTs (login/logout).
- **Brute-force protection:** login rate-limit + temporary lockout; auth events logged.
- **Exposure:** only the proxy port is published; trader↔web ride an `internal` compose network; prefer fronting it with a **VPN/Tailscale or IP allowlist** (defense in depth for one operator).
- **Never render secrets:** OAuth tokens and the app secret are never returned by the API or shown in the UI (assert scrubbing in tests).
- **No write authority:** the web service has no broker code path and no write path to the trading DB or config — it cannot place orders or change anything; going live and all changes remain CLI/config actions.

### 19.6 Testing (extends §15)

Auth (login, lockout, session expiry, CSRF on login/logout), authz (unauthenticated → no data), a test asserting the service exposes **no write endpoints** (only GET + login/logout) and that its DB handle is read-only (any write attempt raises), and a test asserting **no secrets/tokens appear** in any API response or rendered page.

---

### Appendix A — Why event-driven, not vectorized

Vectorized backtesting (numpy/pandas over the whole series) is fast for parameter sweeps but operates on full arrays — easy to accidentally reference future bars, hard to model order state/partial fills/intraday triggers, and the code does **not** match live execution. Event-driven processes one event at a time, mirroring live exactly, structurally preventing lookahead, and supporting a realistic order lifecycle. At ~3 decisions/day the speed penalty is negligible, so correctness + parity win. Keep a vectorized harness, if any, strictly for offline research — never on the live decision path.

### Appendix B — The single most important rule

The simulation clock advances to a trigger timestamp `T`, and the `MarketDataProvider` exposes **only** rows with `ts ≤ T` (minus realistic latency); fills happen on the **next** quote/bar, never at `T` itself. Encapsulating all time access in the `Clock` interface (the strategy never reads `datetime.now()`) makes no-lookahead a structural property, not a matter of discipline.

### Appendix C — Multi-strategy orchestration semantics

How the orchestrator triggers the *corresponding* strategy at the configured time, safely:

1. **Dispatch.** The scheduler is the only thing that knows *which* strategy fires *when*. It emits `TriggerSlot(strategy_id, slot_id, fire_ts, …)`. Live: each `(strategy_id, slot)` is a separate APScheduler job whose callback carries its `strategy_id`. Backtest: all bindings' slots for the day are merged and walked in chronological order. In both, the orchestrator does `strategy = registry[trigger.strategy_id]; run_cycle(strategy, binding.universe, now=trigger.fire_ts)`. There is no global "current strategy" — the strategy is a parameter of the cycle.
2. **Concurrency safety.** APScheduler's per-job `max_instances=1` only prevents a *single* job from overlapping itself; it does **not** prevent two *different* strategies firing at overlapping drifted times. The decision→risk→execute critical section therefore runs under **one global cycle lock**, so account state (positions, buying power, daily counters, daily loss) is read-modify-written atomically for exactly one strategy at a time. At ~3×/day per strategy, contention is negligible.
3. **Deterministic interleave.** When two triggers share a `fire_ts`, order them by a stable key `(fire_ts, strategy_id, slot_id)` so backtests are reproducible and live ordering is well-defined. Per-strategy jitter uses `stable_hash(base_seed, date, strategy_id, slot_id)` so each strategy's drift is independent and (in backtest) reproducible.
4. **Attribution.** Fills are tagged with `strategy_id`; local state keeps per-strategy attributed sub-positions and P&L on top of the broker's commingled truth, reconciled so the per-strategy sum equals the account total (residual → `unknown` bucket + alert).
5. **Conflict resolution.** Same-symbol decisions across strategies in one session are resolved per `risk.conflict_policy` (`net` default / `independent` / `priority`) — see §10.
6. **Failure isolation.** An exception inside one strategy's cycle is caught, logged, alerted, and the slot marked failed — **it must not crash the daemon or other strategies**. A strategy that fails repeatedly can be auto-disabled (per-strategy flag) while the rest keep running.
