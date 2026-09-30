# Runbook: Go live — first real orders (M5.7) and live deployment (M5.8)

**Goal.** Place the first real-money orders at the smallest size, verify the whole live order
path end to end, then run live under docker compose.

> **REAL MONEY.** Every step is manual and deliberate. Stop at the first surprise.
> `trader kill --on` is always safe: it halts every new order and survives restarts.

---

## 0. What stands between the code and a real order

1. **`mode: live`** in the mounted config.
2. **The second signal**: `TRADER_CONFIRM_LIVE=I_UNDERSTAND` in the environment (or `--confirm-live`).
3. **`LIVE_ORDER_PATH_READY`** in `src/trader/app/live_guard.py` — `False` in the repository.
   Flipping it is the one code change of M5.7, made in a reviewed commit (§3).
4. **The guarded-rollout preflight**, before the account is touched: each enabled strategy's
   effective `max_order_notional_usd` ≤ 1,000 and `max_position_size_pct` ≤ 5,
   `max_gross_exposure_usd` ≤ 5,000, a non-empty allowlist, at least one alert channel, the
   kill switch released, and a valid Schwab token.
5. **The trading lease**: one process per state database may send orders or settle order
   rows. `trader run` holds it for its lifetime; `trader reconcile` needs it.
6. **The startup reconciliation gate**, after connecting: every open order settled and the
   positions trued to the broker. Anything unresolved, or a *new* unexplained position change,
   refuses the start and raises a `reconcile_mismatch` alert.
7. **On every order**: the risk gate (price sanity, notional/position/gross caps, daily loss,
   trades per day, pattern-day-trader, allowlist, kill switch); at-most-once placement (one
   send per `client_order_id`; an order whose outcome is unknown is never re-sent); bounded
   polling that cancels an unfinished remainder; and kill-switch auto-trips on a daily-loss
   breach (once per session) or on any failure after an order is sent other than a definite
   rejection (an unknown or unresolved outcome, a fill that couldn't be recorded).

---

## 1. Prerequisites (all must hold)

- **The paper soak passed** ([paper-soak.md](paper-soak.md)). It exercises the same durable
  order path — write-ahead order rows, atomic completion, daily counters — against SimBroker.
- **The Schwab facts marked [VERIFY] are checked** against the live API: order placement
  returns HTTP 201 with the order id in the `Location` header; the order status strings; the
  list-orders endpoint (`fromEnteredTime`/`toEnteredTime` format and inclusivity, the ~60-day
  look-back, `maxResults`); the order JSON fields used for reconciliation (`enteredTime`
  format, `duration`, `session`, `orderStrategyType`, the leg quantity); a 4xx means the
  order was not processed; the token lifetimes; in the account response,
  `initialBalances.liquidationValue` (used as the start-of-day equity for the daily-loss rail)
  and `roundTrips` (the day-trade count used as a floor by the PDT rule). Most can be checked
  read-only (quotes, the account, listing your existing orders).
- **The account is ready.** Decide margin vs cash (the pattern-day-trader limit applies to a
  margin account under $25k). Pick a symbol you do **not** otherwise hold in this account.
  **Do not trade that symbol by hand during the verification**: an identical order entered
  while one of ours is in flight cannot be told apart from ours by reconciliation.
- **Alerts deliver** on at least one channel (send yourself a test).
- **Re-authenticated within the last day** ([weekly-reauth.md](weekly-reauth.md)).
- **The state volume is backed up.**

---

## 2. Prepare the live config

```sh
cp config/live.example.yaml config/live.yaml
```

Edit `config/live.yaml`: the canary symbol in both `universe` and `allowlist` (liquid; one
share must cost less than `max_order_notional_usd` and less than `max_position_size_pct` of
the account equity), one slot per session, your alert channels, and optionally `fees_model`.
Live state lives in its own database (`/state/live.sqlite`); the web UI follows the mounted
config. The file holds no secrets.

**Dry check** with the repository as it is (the order path still locked):

```sh
TRADER_CONFIRM_LIVE=I_UNDERSTAND trader run --config config/live.yaml
```

It must refuse with exactly one problem: `live preflight FAILED [idempotency]`. Fix anything
else it reports first.

---

## 3. Arm the order path (the one reviewed code change)

On a branch: set `LIVE_ORDER_PATH_READY = True` in `src/trader/app/live_guard.py` and update the
tests that pin the lock (`tests/unit/app/test_live_config_template.py`,
`tests/unit/app/test_live_guard.py`). Open a pull request, review it deliberately, merge it, and
build the image/venv from that commit. Nothing else changes.

---

## 4. First real order (session 1)

During market hours, after the canary's slot time:

```sh
trader reconcile --config config/live.yaml         # must end with: result: CLEAN
trader kill --off --config config/live.yaml        # the preflight requires it released
TRADER_CONFIRM_LIVE=I_UNDERSTAND trader run --config config/live.yaml --once
```

`--once` fires each slot once (the fired-slot ledger then blocks a second fire that day),
subject to the trading calendar. Expect, in order:

- `startup reconcile: … result: CLEAN`, then the CRITICAL "starting in live mode" alert;
- the canary **buys 1 share**: `audit_log` rows `order_pending` and `fill`; an `orders` row with
  status `FILLED` and the Schwab order id; one `fills` row; attributed position `canary` = 1;
- the order and the share in the Schwab app.

Then verify the books tie out:

```sh
trader reconcile --config config/live.yaml         # result: CLEAN
```

The web UI's Orders and Account pages show the same.

---

## 5. Kill-switch drill

```sh
trader kill --on --reason "go-live drill" --config config/live.yaml
TRADER_CONFIRM_LIVE=I_UNDERSTAND trader run --config config/live.yaml --once   # refuses: kill switch
trader kill --off --config config/live.yaml
```

(In a running daemon the switch halts the next cycle at its start and every order at the
gate — covered by tests.)

---

## 6. Second session: the canary sells

On the next session, repeat §4: `trader reconcile` → CLEAN, then one `--once` tick. The canary
**sells the 1 share**; the position is flat and `trader reconcile` is CLEAN. The round trip
spans two sessions, so it is not a day-trade.

---

## 7. When something goes wrong

- **An order's outcome is unknown or it stayed unresolved.** The kill switch engages itself
  and alerts. Do not restart blindly. Wait at least `execution.reconcile_window_seconds`
  (default 5 minutes), then `trader reconcile`: it adopts the order if it landed, or marks it
  not placed once that is proven. If it stays unresolved (e.g. an identical manual order was
  entered at the same time), check the Schwab order history and settle it by hand:
  `trader reconcile --adopt CID=SCHWAB_ORDER_ID` or `--mark-not-placed CID`. Then
  `trader kill --off`.
- **A daily-loss breach** engages the kill switch (source `auto`), once per session. Review,
  then `trader kill --off` lets exits through; the daily-loss rule keeps refusing new entries
  for the rest of the session, and the switch does not re-trip that session.
- **Alert "order execution halted: could not engage the kill switch …"**: an order's fate
  became uncertain and the switch could not be written (e.g. the state volume is full or
  read-only). That process refuses every order until restarted. Stop it, fix the volume,
  `trader kill --on`, `trader reconcile`, then restart.
- **Orders refused with "PDT: …"**: the account is at the pattern-day-trader limit while
  under the equity threshold. New entries and same-session exits are refused until the
  rolling window moves on; an exit of a position held overnight still goes through.
- **Start refused: "startup reconciliation is not clean".** Run `trader reconcile` to see why:
  unresolved orders (above), or a new unexplained position change. Holdings that are yours and
  unchanged are reported as *standing* and do not block; after your own manual trade, re-run
  `trader reconcile` once to confirm the new state.
- **Refresh token dead**: the client enters read-only safe mode (no orders); re-authenticate.
- **`trader reconcile` exits 3**: the daemon holds the trading lease — stop it first.

---

## 8. Roll back to paper

```sh
trader kill --on --reason "rollback" --config config/live.yaml
# stop the daemon; start it again with the paper config (unset TRADER_CONFIG_FILE and
# TRADER_CONFIRM_LIVE for compose)
```

Close any live position by hand in the Schwab app if you want to, then run `trader reconcile`
against the live config once more to leave its books settled.

---

## 9. Deploy live via compose (M5.8)

1. In `deploy/secrets/.env`: `TRADER_CONFIRM_LIVE=I_UNDERSTAND`.
2. Select the live config for **every** compose command (compose interpolation reads the shell,
   or `deploy/.env`, which is git-ignored — not `secrets/.env`):

   ```sh
   cd deploy
   TRADER_CONFIG_FILE=../config/live.yaml docker compose up -d --build
   ```

3. Watch `docker compose logs -f trader`: `startup reconcile: … CLEAN`, the CRITICAL live
   alert, the heartbeat, and `healthy` in `docker compose ps`.
4. The container restarts after a crash (`restart: unless-stopped`); every start passes the
   preflight and the startup reconciliation gate (waiting out an open consistency window
   once), and refuses — with an alert — if the account is not clean.
5. `trader reconcile` needs the lease, so stop the daemon first:

   ```sh
   docker compose stop trader
   TRADER_CONFIG_FILE=../config/live.yaml docker compose run --rm trader reconcile
   docker compose start trader
   ```

6. Keep the conservative caps until you have confidence; the weekly re-auth still applies.
