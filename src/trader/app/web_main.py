"""``trader-web`` console entrypoint (design §16.6, M7.11).

Builds ``WebSettings`` from the environment (the compose ``env_file``) and serves the
read-only monitoring app with uvicorn. Production only — no reload, no debug. Binds
0.0.0.0:8000 INSIDE the container; it is never published to the host (only the Caddy proxy's
:443 is). A crash here cannot affect the trader (separate process/container; the read-only DB
handle and no-broker import surface are enforced by the M7.10 guard tests).
"""

from __future__ import annotations

import os

from trader.web.app import create_app
from trader.web.settings import WebSettings

_HOST = "0.0.0.0"
_PORT = 8000


def main() -> None:
    import uvicorn

    settings = WebSettings.from_env(dict(os.environ))
    uvicorn.run(create_app(settings), host=_HOST, port=_PORT, log_level="info")


if __name__ == "__main__":  # pragma: no cover - container entrypoint
    main()
