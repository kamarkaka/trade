"""Schwab Trader API endpoints and token lifetimes — the [VERIFY] parity checklist.

Per design §8.7 every Schwab-specific fact lives here, isolated from the rest of
the system. Each value is marked [VERIFY] because it was gathered from community
clients/prior knowledge and MUST be confirmed against the live Schwab developer
portal before being relied upon.
"""

from __future__ import annotations

API_BASE = "https://api.schwabapi.com"  # [VERIFY]

# OAuth (three-legged authorization-code flow, §8.2)
OAUTH_AUTHORIZE_URL = f"{API_BASE}/v1/oauth/authorize"  # [VERIFY]
OAUTH_TOKEN_URL = f"{API_BASE}/v1/oauth/token"  # [VERIFY]

# Product families (§8.1)
MARKETDATA_BASE = f"{API_BASE}/marketdata/v1"  # [VERIFY]
TRADER_BASE = f"{API_BASE}/trader/v1"  # [VERIFY]

# Read endpoints used in M1 (§8.4 / §8.5)
QUOTES_PATH = f"{MARKETDATA_BASE}/quotes"  # [VERIFY]
PRICEHISTORY_PATH = f"{MARKETDATA_BASE}/pricehistory"  # [VERIFY]
ACCOUNT_NUMBERS_PATH = f"{TRADER_BASE}/accounts/accountNumbers"  # [VERIFY]

# Token lifetimes (§8.2). The 7-day refresh-token cap is the dominant operational
# constraint and is NOT programmatically renewable.
ACCESS_TOKEN_TTL_SECONDS = 1800  # ~30 minutes [VERIFY]
REFRESH_TOKEN_TTL_DAYS = 7  # hard cap, requires interactive re-auth [VERIFY]

# Rate limit (§8.6) — a planning ceiling, not a guarantee.
RATE_LIMIT_PER_MIN = 120  # [VERIFY]
