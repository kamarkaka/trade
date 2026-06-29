# Runbook: Weekly Schwab re-authentication

**Why this exists.** Schwab **refresh tokens expire ~7 days** and are **not** programmatically
renewable — a human must complete the browser OAuth flow roughly weekly. The daemon cannot do
this headless. It therefore alerts ahead of expiry (`reauth_reminder`). If a refresh actually
returns a dead token, the Schwab client enters **read-only safe mode** and **refuses every
request** (so no order is ever placed — and there is no order path before M5 regardless);
cycles fail closed and an alert fires. The daemon **process does not crash**, but it cannot
trade until you re-authenticate. This runbook is the §16.4 procedure to refresh the token.

> Verify the exact refresh-token TTL against current Schwab developer docs; the code treats it as
> a configurable max age and reports the countdown in `trader status`.

The token store is a SQLite file on the durable `trader_state` volume at
**`/state/schwab_token.sqlite`** (derived from `observability.db_path`'s directory). Because it
lives on a named volume, a refreshed token survives container recreation.

---

## Check the current token age

```sh
docker compose exec trader trader status
# auth: authenticated; refresh token expires in ~N day(s)
#   (or) auth: not authenticated (run `trader reauth`)
```

Re-auth when the countdown gets low (e.g. ≤ 2 days) or when you receive the `reauth_reminder`
alert.

---

## Option A — Authenticate on a laptop, copy the token to the server (recommended)

The OAuth flow needs a browser and a loopback HTTPS callback, which the headless server lacks.
Do the interactive part on a laptop that has the **same** `SCHWAB_APP_KEY`/`SCHWAB_APP_SECRET`,
then copy the resulting token file onto the server's volume.

1. **On the laptop** (with this repo + venv, or the image), run the OAuth flow:
   ```sh
   export SCHWAB_APP_KEY=... SCHWAB_APP_SECRET=...
   # Optional: control where the token store is written.
   export SCHWAB_TOKEN_STORE_PATH=./schwab_token.sqlite
   trader reauth
   # follow the browser prompt; on success the token store is written to the path above.
   ```

2. **Copy the token store onto the server's `trader_state` volume.** With the trader container
   running, copy straight into it (the file lands on the mounted volume):
   ```sh
   # from the laptop, scp the file to the server first if needed, then on the server:
   docker compose cp ./schwab_token.sqlite trader:/state/schwab_token.sqlite
   ```
   Alternatively write into the volume via a throwaway container. **Note the real volume
   name:** Compose prefixes it with the project name (the compose directory, `deploy`), so it
   is `deploy_trader_state`, not `trader_state` — confirm with `docker volume ls`:
   ```sh
   docker run --rm -v deploy_trader_state:/state -v "$PWD":/in alpine \
     cp /in/schwab_token.sqlite /state/schwab_token.sqlite
   ```

3. **Make the daemon pick it up.** A clean restart re-reads the token store:
   ```sh
   docker compose restart trader
   docker compose exec trader trader status   # expect a fresh ~7-day countdown
   ```

4. **Delete the laptop copy** (`rm ./schwab_token.sqlite`) — it contains live credentials; never
   commit or paste it anywhere.

---

## Option B — SSH port-forward the callback to the server

Run the OAuth flow *on the server* but drive the browser from your laptop by forwarding the
loopback callback port over SSH. **Caveats:** the callback default is `https://127.0.0.1:8182`
(override via `SCHWAB_REDIRECT_URI`). A `docker compose exec` callback binds inside the
*container's* network namespace, which an SSH `-L` forward (terminating on the host) cannot
reach, and the slim container has **no browser** — so prefer running `reauth` directly on the
server host (in a venv/one-off `docker run` with the port published), or just use Option A.

1. Forward the callback port from the laptop (default `8182`):
   ```sh
   ssh -L 8182:localhost:8182 user@server     # 8182 = default; match SCHWAB_REDIRECT_URI
   ```
2. On the server host, run `trader reauth` so its loopback callback binds the forwarded port,
   and copy the printed authorize URL into the laptop browser (no browser exists server-side).
3. On success the token is written to the configured token store; copy it onto
   `deploy_trader_state` if you authed outside the volume, then `docker compose restart trader`
   and verify with `trader status`.

---

## Verify success

```sh
docker compose exec trader trader status            # auth: authenticated; expires in ~7 day(s)
docker compose exec trader trader status --healthcheck; echo "exit=$?"   # 0
```
The `reauth_reminder` alert should stop firing once the fresh token is in place.

## If you miss the window

Once a refresh returns a dead token the client enters read-only safe mode and refuses all
Schwab calls; cycles fail closed (no orders) and alerts fire. The process stays up but cannot
trade. (If the token store is missing entirely, you instead get `not authenticated` errors and
a `crash` alert each cycle — same net effect: no trading.) Re-auth with Option A/B above and
restart; the fired-slot ledger + durable state mean it resumes without double-firing.
**Never** work around expiry by disabling alerts or forcing live mode.
