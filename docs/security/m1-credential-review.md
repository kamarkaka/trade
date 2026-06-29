# M1 Credential-Handling Security Review

**Scope:** all code that reads, stores, transmits, or could log Schwab credentials
and tokens, as built across M1.1–M1.9. This document is the formal M1 exit-gate
artifact for the criterion *"security review of credential-handling code done"*
(design §13).

**Status:** ✅ Complete. No blocking issues. Residual risks are enumerated with
mitigations below. Schwab wire facts still to confirm against the live developer
portal are listed as `[VERIFY]`.

**Date:** 2026-06-28 · **Reviewer:** automated implementation review (per-PR
adversarial agent + this audit).

---

## 1. Credential surface (what secrets exist and where they live)

| Secret | Type in code | At rest | In transit | Lifetime |
| --- | --- | --- | --- | --- |
| `app_key` | `str` in `SchwabClientConfig` | env only (`SCHWAB_APP_KEY`) | inside HTTP Basic header to the token endpoint; also a non-secret `client_id` URL param on the authorize URL | process |
| `app_secret` | `SecretStr` in `SchwabClientConfig` | env only (`SCHWAB_APP_SECRET`) | inside HTTP Basic header to the token endpoint | process |
| authorization `code` | `str` (function-local) | never persisted | POST body to token endpoint | single-use, seconds |
| `access_token` | `str` in `TokenSet` | SQLite token store (0600) | `Authorization: Bearer` header on API calls | ~30 min |
| `refresh_token` | `str` in `TokenSet` | SQLite token store (0600) | POST body to token endpoint | hard 7-day cap (§8.2) |
| raw `accountNumber` | `str` in `AccountNumberMapping` | not persisted | response body from `/accounts/accountNumbers`; resolved to `hashValue` for all downstream use | process |

There is exactly **one** secrets-input boundary (`schwab_config_from_env`, the env)
and **one** token-persistence component (`auth.token_store.TokenStore`), satisfying
the §13 single-secrets-component rule.

## 2. Confidentiality controls (and where they're enforced)

### 2.1 `app_secret` is a `SecretStr`
- Declared `app_secret: SecretStr` in `schwab/config.py`. Pydantic renders it as
  `**********` in `repr()`/`str()`/`model_dump()`. Verified by
  `tests/unit/schwab/test_config.py::test_app_secret_not_in_repr` and
  `::test_from_env_secret_not_in_repr`.
- `.get_secret_value()` is called in **exactly one place**:
  `auth/oauth.py::_basic_auth_header`, at the moment of building the Basic auth
  header for the token POST. Confirmed by grep (`get_secret_value` → 1 hit). No
  other module unwraps it.

### 2.2 Tokens live only in the token store, with restrictive permissions
- `TokenStore` (`auth/token_store.py`) is the only persistence of tokens: its own
  single-row SQLite file, **separate** from the trading state DB, rollback-journal
  mode (single file), `chmod 0600` applied on create and after every write
  (best-effort via `contextlib.suppress(OSError)` for non-POSIX platforms).
- The file path defaults under `/state/` (gitignored). `schwab_config_from_env`
  allows an override via `SCHWAB_TOKEN_STORE_PATH` but never points at a tracked
  path by default. Verified by `tests/unit/auth/test_token_store_perms.py`.
- `TokenSet` has a masking `__repr__` (`repr=False` + custom) so neither the access
  nor refresh token appears if the object is logged/printed. (M1.2 finding/fix.)

### 2.3 Central log scrubbing wraps every log path
- `observability/logging.py` installs `_scrub_processor` in the structlog pipeline,
  applied to **every** event before rendering. It redacts:
  - **By key** (case-insensitive): `access_token`, `refresh_token`, `token`,
    `app_secret`, `client_secret`, `secret`, `authorization`, `password`,
    `api_key`, `account_number`, … — covering both the **Basic** and **Bearer**
    `Authorization` header values if ever placed in a field named `authorization`.
  - **By literal**: tokens and the auth code are passed to `register_secret()` at
    the moment they are obtained (`oauth.py`: `exchange_code` registers the `code`;
    `_parse_token_response` registers the access and refresh tokens), so the literal
    strings are scrubbed wherever they subsequently appear — including free-text
    messages and nested structures.
- The transport (`schwab/http.py`) logs only `method`+`url` (at DEBUG) and a
  safe-mode `reason` string (at WARNING). **Request/response bodies and headers are
  never logged**, so the Bearer token (header-only) and token-endpoint bodies never
  enter a log record even before scrubbing.

### 2.4 The market-data adapter and CLI never expose tokens or the raw account number
- `data/SchwabMarketData` handles only quotes/bars; it never reads tokens and never
  touches account numbers.
- `app/cli.py` `status` reports the refresh-token **age** (a derived number), never
  the token. It is read-only and does not even create the token store file when
  absent. `reauth` prints only progress text; on missing credentials it emits a
  clean `reauth error:` message and exits non-zero (no stack trace, no secret).
- All trading-side use of an account flows through the **hashed** id (`hashValue`,
  §8.5); the raw `accountNumber` carries a masking `__repr__` and is registered as a
  scrub literal at the point it is parsed (`SchwabClient.get_account_numbers`), so it
  is redacted everywhere — including free-text log messages — exactly like a token.
  (This gap — PII protected only by key-name redaction — was caught by the leakage
  test and hardened.)

### 2.5 Supply chain
- **No third-party broker SDK is imported.** Verified by grep for `schwab`,
  `schwabdev`, `alpaca`, `ib_insync`, `tda` in `src/` → none. schwab-py / Schwabdev
  were used as a **parity reference only** (documented in module docstrings).
- New M1 runtime deps: `httpx`, `cryptography`, `tenacity`; dev: `respx`. All are
  declared with conservative lower bounds in `pyproject.toml` / `requirements-dev.txt`.

## 3. Transmission

- All Schwab traffic is HTTPS to `api.schwabapi.com`. The OAuth loopback redirect is
  **HTTPS even on localhost** (enforced by `SchwabClientConfig`'s `redirect_uri`
  validator); the callback server (`auth/callback_server.py`) is loopback-bound,
  uses an ephemeral self-signed cert (private key written 0600 in a temp dir cleaned
  in `finally`), and never logs the request path (which carries the `code`).
- The auth `code` is registered as a secret before the exchange request is built.

## 4. Residual risks & mitigations

| Risk | Likelihood | Mitigation / status |
| --- | --- | --- |
| Refresh token reaches the 7-day cap unnoticed → trading halts | Medium | `Authenticator.check_token_age` fires a re-auth alert ahead of expiry; `status` shows the countdown; `issued_at` is **required** on refresh so the clock can't silently reset (M1.4 fix). On a dead refresh the client enters READ-ONLY safe mode rather than erroring blindly. |
| Token store file readable by another local user | Low | 0600 perms on create + after each write; separate file from state DB. On a shared host, run the container/user isolated (§16). |
| A future contributor logs a `TokenSet`/`AccountNumberMapping`/raw value | Low | Masking `__repr__` on both; key+literal scrubbing in the log pipeline; this review + the leakage test (`test_no_secret_leakage.py`) as a regression guard. |
| `app_key`/`app_secret` leak via env inspection / process listing | Low | Kept out of the repo and YAML; only in env; `app_secret` is `SecretStr`. Operator guidance: use Docker secrets / a secret manager, not a committed `.env`. |
| Dependencies not hash-pinned (supply-chain tamper) | Low–Medium | See §5 — deliberate, documented trade-off with Dependabot as the compensating control. |

## 5. Dependency pinning decision (deviation from the milestone's "hash-lock")

The milestone text called for a Poetry/uv hash-locked lockfile. This project
deliberately uses **pip + venv** (M0.1) and resolves packages through an internal
mirror whose versions differ from public PyPI (where CI installs). A frozen,
fully-hashed cross-platform lock would not be portable between the two and would
break CI (documented at the top of `requirements-dev.txt`). Decision:

- Keep conservative version **lower bounds** in `pyproject.toml` /
  `requirements-dev.txt`.
- Rely on **Dependabot** (already configured) to keep dependencies current and to
  surface known-vulnerability advisories as PRs.
- A fully-hashed lock (e.g. via `uv`) can be introduced later **without changing the
  project layout** if/when a single resolution source is standardised.

This is a knowing trade-off, not an oversight; the compensating control
(Dependabot + no third-party broker SDK + minimal dependency set) is in place.

`freezegun` (listed as an M1.10 dev dep) was intentionally **not** added: all
time-dependent tests use the injected `FakeClock`/`VirtualClock` seam, which is
stronger and deterministic without monkeypatching the clock.

## 6. `[VERIFY]` — Schwab facts to confirm against the live portal

These are marked `[VERIFY]` in code and do not affect the security posture, but
should be confirmed during first live authorization:

- Exact OAuth token/authorize URLs and the account-numbers endpoint path
  (`schwab/constants.py`).
- Quote field used for staleness (`quoteTime` vs `tradeTime`) — `schwab/models.py`.
- Price-history `periodType`/`period` interaction when explicit `startDate`/`endDate`
  are supplied (`schwab/endpoints.py`).
- The precise `error` codes returned for a dead/expired refresh token
  (`_DEAD_REFRESH_ERRORS` in `oauth.py`).

## 7. Sign-off

All five credential surfaces (app_key/secret via `SecretStr`; tokens in the 0600
SQLite store; the Basic auth header; the Bearer header; the callback code) were
audited. Confidentiality controls (SecretStr, masking reprs, 0600 perms, central
key+literal scrubbing, body/header never logged, single secrets boundary, single
token store, no broker SDK) are present and covered by tests. **The M1
credential-handling security review is complete.**
