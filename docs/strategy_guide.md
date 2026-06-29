# Writing a strategy

A strategy is the one place you plug in your own buy/sell/hold logic. Everything else
(scheduling, sizing, the risk gate, execution, backtest vs live) is provided. Adding a new
strategy is **a class + a config binding** — no changes to the engine. Copy
[`src/trader/strategy/strategies/template.py`](../src/trader/strategy/strategies/template.py)
and edit the signal.

## 1. The contract

A strategy implements one method (design §5/§6, `trader.core.protocols.Strategy`):

```python
def decide(
    self,
    snapshot: MarketSnapshot,      # current per-symbol quotes at the trigger instant
    positions: Sequence[Position], # your current holdings (read-only)
    account: Account,              # cash / buying_power / equity (read-only)
    data: MarketDataProvider,      # asof-bound history (the ONLY way to read bars)
    clock: Clock,                  # the ONLY time source
) -> Sequence[Decision]:
```

Return a `Decision(action, symbol, quantity, limit_price=None, rationale="")` per symbol you
want to act on. `quantity` is the **desired absolute share delta** — sizing and the risk gate
run later in the orchestrator, so you never compute dollar amounts or clamp here. Omit a
symbol (or return nothing) to HOLD.

## 2. Boundary rules you MUST obey

These are enforced by the conformance suite (`tests/unit/test_strategy_contract.py`) — CI
fails if you break them:

- **Pure.** Read only the injected `snapshot`/`positions`/`data`/`clock`/`account`. No
  globals, no I/O, no sockets.
- **No wall clock.** Never call `datetime.now()`, `time.time()`, or `date.today()`. Use
  `clock.now()` and `snapshot.asof`. (A source grep enforces this.)
- **No lookahead.** Read history only through `data.get_bars(..., asof=clock.now())`. The
  provider returns only `ts <= asof` rows (Appendix B); never reorder or peek ahead.
- **No input mutation.** Treat `positions`/`account`/`snapshot` as read-only.
- **Deterministic.** Identical inputs → identical outputs (no randomness, no ambient state).
- **No whole-array pandas in the hot path.** Use the shared
  [`indicators.py`](../src/trader/strategy/indicators.py) (pure Decimal, no-lookahead) so your
  math matches the backtest and the research harness exactly.

## 3. Anatomy of a strategy (walk through the template)

`ExampleTemplateStrategy` (`strategies/template.py`):

1. `__init__(self, lookback=20, lot=10, **params)` — store typed params from the binding.
2. `decide(...)` — for each `symbol, quote` in `snapshot.quotes`:
   - pull trailing bars: `data.get_bars(symbol, start=clock.now()-N, end=clock.now(), freq="daily", asof=clock.now())` (only `"daily"` is supported today);
   - compute an indicator: `sma(closes_from_bars(bars), self.lookback)`;
   - emit `Decision(Action.BUY/SELL, symbol, self.lot, rationale=...)`, or HOLD (emit nothing)
     when there's insufficient history or no signal.

## 4. Params

Put strategy parameters in the binding's `params:` (config §11). Today they pass through as an
unvalidated `dict[str, object]` to your `__init__`, so coerce/validate them there (the template
does `int(lookback)`, `int(lot)`). Name every param explicitly in `__init__` so a config typo
fails loudly rather than being silently ignored.

## 5. Register the class

Decorate the class so the registry (and the conformance suite + the binding loader) find it
by name:

```python
from trader.strategy.registry import REGISTRY

@REGISTRY.register("my_strategy")
class MyStrategy: ...
```

Then import the module in `src/trader/strategy/__init__.py` (next to `template`,
`threshold`, `zscore_revert`) so the decorator runs on package import.

## 6. Wire a config binding

Add a binding under `strategies:` in your config (copy from
[`config/default.yaml`](../config/default.yaml)):

```yaml
strategies:
  - id: my_strat            # unique id (attribution key)
    name: my_strategy       # the registered name
    enabled: true
    params: {lookback: 20, lot: 10}
    universe: [AAPL, MSFT]   # symbols this strategy trades
    slots:
      - {id: open, time: "09:45"}   # local-time schedule slots (drift applied)
    # risk_overrides: {max_order_notional_usd: 3000}   # optional, per-strategy
```

## 7. Test it

- It must pass the generic conformance suite — it's auto-covered once registered:
  `pytest tests/unit/test_strategy_contract.py -k my_strategy -v`.
- Add a focused unit test on **synthetic** data (§15.1) asserting exact decisions for known
  inputs (handcrafted bars/quotes), e.g. "a 3% dip below SMA → BUY".

## 8. Backtest it

Run an offline backtest over cached data and read the per-strategy report (see M6.6/M6.7):

```sh
trader backtest --start 2023-01-01 --end 2023-12-31 --config your.yaml
```

By design the SAME `decide` runs in backtest and live (only the injected broker/data/clock
differ), so a strategy that backtests cleanly behaves identically in paper/live. (The
`trader run` daemon wiring that dispatches registry-bound strategies lands in M3/M4; the
end-to-end backtest report is M6.6/M6.7.)
