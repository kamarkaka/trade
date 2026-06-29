# Runbook: Paper soak (multi-day dress rehearsal)

**Goal.** Run the whole system in paper mode against **live Schwab quotes** with **simulated
fills (SimBroker)** for ≥3 market days, exercising the scheduler/jitter/calendar, risk gate,
attribution, durable audit, alerting, heartbeat/healthcheck, state durability, and the weekly
re-auth — with **zero real orders**. This is the gate that clears the system for guarded live
trading (M5).

> Safety: paper mode uses `SimBroker` only. The daemon **refuses `mode: live`** until M5, and
> there is no real-order code path before M5 (CI tripwire `test_no_real_order_path_pre_m5`).
> The kill switch and the `reconcile`/`kill` CLI commands land in M5 — this soak validates
> everything that exists in M4.

---

## 1. Prerequisites

- A host with Docker + the Docker Compose v2 plugin (the design target is ~1 vCPU / 1–2 GB).
- A Schwab developer app (App Key + Secret). No sandbox exists, so quotes come from the **live**
  read-only market-data API; fills are simulated.
- Config in **paper** mode. The committed `config/default.yaml` is mounted read-only at
  `/config/trader.yaml`; review its `mode`, `strategies`, `schedule`, and `risk` blocks.
  Confirm `observability.db_path` is under `/state` and `observability.data_cache` under
  `/data` (so the named volumes are actually used).
- Secrets filled in: copy the template and edit the real (gitignored) file.

```sh
cp deploy/secrets/.env.example deploy/secrets/.env
# edit deploy/secrets/.env: SCHWAB_APP_KEY / SCHWAB_APP_SECRET (required),
# and at least one alert channel (TELEGRAM_* or SMTP_*) so failures are never silent.
```

- Authenticate once before the first start (the refresh token must be valid). See
  [weekly-reauth.md](weekly-reauth.md). The token store lives on the `trader_state` volume at
  `/state/schwab_token.sqlite`, so it survives recreates.

---

## 2. Deploy

```sh
cd deploy
docker compose up -d --build
docker compose ps                 # STATUS should be "Up", health "starting" then "healthy"
docker compose logs -f trader     # watch startup
```

Expected on a clean start: the daemon loads config, registers one job per (strategy, slot),
writes the first heartbeat, and reports the number of scheduled jobs. No orders are placed.

---

## 3. What to watch (daily)

### Health / heartbeat
The container HEALTHCHECK runs `trader status --healthcheck`, which is green only when the
heartbeat is **fresh** (touched on a fixed interval by a dedicated executor).

```sh
docker inspect --format '{{.State.Health.Status}}' $(docker compose ps -q trader)   # -> healthy
docker compose exec trader trader status --healthcheck; echo "exit=$?"               # 0 = alive
```

### Auth / token age
```sh
docker compose exec trader trader status
# -> mode: paper
#    strategies: ...
#    auth: authenticated; refresh token expires in ~N day(s)
```
Watch the refresh-token countdown. A `reauth_reminder` alert should fire **before** it expires;
re-auth weekly (see weekly-reauth.md). If a refresh returns a dead token, the Schwab client
enters read-only safe mode and refuses all calls — cycles fail closed (no orders) and an alert
fires; the process stays up but cannot trade until re-authenticated.

### Triggers firing on schedule + drift
Each enabled slot fires once per session at its local time plus the seeded random drift
(≤ its `drift_max_minutes`). Confirm in the logs and via the fired-slot ledger (exactly-once).
The slim image ships the Python `sqlite3` module (not the `sqlite3` CLI), so query read-only via
`python`:

```sh
docker compose exec trader python - <<'PY'
import sqlite3
c = sqlite3.connect("file:/state/trader.sqlite?mode=ro", uri=True)
for r in c.execute("SELECT slot_date, strategy_id, slot_id, status FROM fired_slot ORDER BY claimed_at DESC LIMIT 20"):
    print(r)
PY
```

### Audit chain accumulating
Every cycle writes a correlated chain to `audit_log` (inputs→decision→risk→order→fill),
one JSON row per event keyed by `cycle_id`:

```sh
docker compose exec trader python - <<'PY'
import sqlite3
c = sqlite3.connect("file:/state/trader.sqlite?mode=ro", uri=True)
for r in c.execute("SELECT ts, cycle_id, strategy_id, kind FROM audit_log ORDER BY id DESC LIMIT 30"):
    print(r)
PY
```
You should see `order_pending`/`fill` for trades the strategies decide to make, and
`rejected` rows (with the reason in `payload`) for anything the risk gate blocks. **No real
order is ever sent** — fills come from SimBroker.

### Alerts
Confirm the configured channels actually deliver (send yourself a known event, e.g. by inducing
a skipped slot below). A `skipped_slot` (WARNING) fires on non-session days; `crash` (CRITICAL)
on a cycle exception.

---

## 4. Fault injection (run at least once during the soak)

1. **Abrupt stop / restart (exactly-once + state durability).**
   ```sh
   docker kill $(docker compose ps -q trader)   # SIGKILL, no clean shutdown
   docker compose up -d
   ```
   Expected: on restart the daemon resumes; the fired-slot ledger prevents re-firing a slot
   already claimed that session (no double-trade); previously written `audit_log`/ledger rows
   are still present (durable volume).

2. **State survives recreation (volumes).**
   ```sh
   count() { docker compose exec trader python -c \
     "import sqlite3;print(sqlite3.connect('file:/state/trader.sqlite?mode=ro',uri=True).execute('SELECT count(*) FROM audit_log').fetchone()[0])"; }
   count                                  # before
   docker compose down && docker compose up -d
   count                                  # after — must be unchanged
   ```
   Expected: the row count and the token age are **unchanged** across `down/up` (named volumes
   `trader_state` + `trader_data`).

3. **Stale / bad-data quote (price sanity).** During a soak you can't easily force a stale quote
   from the live feed; instead confirm via the unit suite that `price_sanity` rejects stale /
   wide-spread / crossed / bad-tick quotes (`tests/unit/risk/test_rules.py`), and watch for any
   `rejected` audit rows citing `price_sanity` if the live feed hiccups. No trade must occur on
   uncertain data.

4. **Token expiry (re-auth path).** Expected: a `reauth_reminder` alert fires ahead of expiry.
   When a refresh returns a dead token, the client enters read-only safe mode — all Schwab
   calls are refused, cycles fail closed (no orders), and an alert fires; the process stays up
   but cannot trade. (Removing `/state/schwab_token.sqlite` instead yields `not authenticated`
   errors + a `crash` alert each cycle — same net effect, not formal safe mode.) Recover via
   [weekly-reauth.md](weekly-reauth.md).

5. **Liveness (healthcheck).** The dedicated heartbeat executor keeps liveness independent of
   cycle work, so a healthy-but-busy daemon stays `healthy`. Note the restart semantics:
   `restart: unless-stopped` restarts the container only when the process **exits** — a hung
   but still-running container is surfaced as `unhealthy` by the healthcheck (and via alerts)
   but is **not** auto-restarted by `unless-stopped` alone (that needs Swarm or an external
   watchdog). A process that crashes/exits is restarted.

---

## 5. Success criteria (all must hold over ≥3 market days)

- [ ] Triggers fire once per session on schedule **with drift**; no slot fires twice.
- [ ] `audit_log` accumulates correlated chains; `rejected` rows carry a reason.
- [ ] Heartbeat stays fresh; `status --healthcheck` is 0 while running; container is `healthy`.
- [ ] **Zero real orders** — only SimBroker fills; no `SchwabBroker`/order endpoint exists.
- [ ] State (audit, ledger, token age) **survives** a mid-soak `compose down && up -d` and an
      abrupt `docker kill` + restart.
- [ ] Alerts deliver on at least one channel; a skipped slot / cycle error is never silent.
- [ ] The re-auth reminder fires ahead of refresh-token expiry; weekly re-auth works.

Record results (dates, anomalies, alert screenshots) in the soak log. When every box is checked,
the system is cleared to begin **guarded** live trading (M5), which still requires a separate
double-confirm and starts at the smallest size.

---

## 6. Teardown

```sh
cd deploy
docker compose down            # keeps the named volumes (state persists)
# docker compose down -v       # DANGER: also deletes trader_state/trader_data (loses tokens/state)
```
