"""Read-only monitoring web UI (design §19, M7).

A strictly READ-ONLY, password-gated monitoring service that runs as a SEPARATE process /
container from the trader. It opens the shared SQLite state DB read-only and writes NOTHING
to the trading system; it exposes only GET endpoints plus login/logout. It has NO broker
code path — nothing under ``trader.web`` may import ``trader.broker``, ``trader.schwab``,
``trader.execution``, or ``trader.auth`` (enforced by the M7 import-isolation tests). All
control (kill switch, enable/disable, config) is via the CLI + config file, never the UI.
"""

from trader.web.app import create_app
from trader.web.settings import WebSettings

__all__ = ["WebSettings", "create_app"]
