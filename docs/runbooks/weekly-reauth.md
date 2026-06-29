# Runbook: Weekly Schwab re-authentication

**Why this exists.** Schwab **refresh tokens expire ~7 days** and are **not** programmatically
renewable — a human must complete the browser OAuth flow roughly weekly. The daemon cannot do
this headless. It therefore alerts ahead of expiry (`reauth_reminder`) and, if the token lapses,
degrades to **read-only safe mode** (it keeps reading quotes/serving status but places no
orders) instead of crashing. This runbook is the §16.4 procedure to refresh the token.

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
   Alternatively write into the volume via a throwaway container:
   ```sh
   docker run --rm -v trader_state:/state -v "$PWD":/in alpine \
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
loopback callback port over SSH.

1. Find the callback port the client uses (the OAuth loopback redirect URI / port; see the
   Schwab app config and `trader reauth` output).
2. Forward it from the laptop:
   ```sh
   ssh -L 8443:localhost:8443 user@server     # replace 8443 with the actual callback port
   ```
3. On the server, run the flow against the running container (or a one-off run):
   ```sh
   docker compose exec trader trader reauth
   ```
4. Complete the browser prompt on the laptop (the forwarded port reaches the server's callback).
   The token is written directly to `/state/schwab_token.sqlite` on the volume.
5. `docker compose restart trader` and verify with `trader status`.

---

## Verify success

```sh
docker compose exec trader trader status            # auth: authenticated; expires in ~7 day(s)
docker compose exec trader trader status --healthcheck; echo "exit=$?"   # 0
```
The `reauth_reminder` alert should stop firing once the fresh token is in place.

## If you miss the window

The daemon enters read-only safe mode (no orders) and keeps alerting. Re-auth with Option A/B
above and restart; the fired-slot ledger + durable state mean it resumes without double-firing.
**Never** work around expiry by disabling alerts or forcing live mode.
