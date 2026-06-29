"""The trader-web entrypoint + console script (M7.11)."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"


def test_web_main_is_callable_and_lazy() -> None:
    # Importing the entrypoint must NOT import uvicorn (it's imported inside main()) or start
    # a server — keeps import cheap and side-effect-free.
    sys.modules.pop("uvicorn", None)
    from trader.app import web_main

    assert callable(web_main.main)
    assert "uvicorn" not in sys.modules  # uvicorn import is deferred to main()


def test_console_script_declared() -> None:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert data["project"]["scripts"]["trader-web"] == "trader.app.web_main:main"


def test_web_main_imports_no_broker() -> None:
    # The entrypoint must not pull a broker/credential path at import. Run in a CLEAN
    # subprocess — the shared in-process sys.modules is polluted by other tests.
    import subprocess

    probe = (
        "import sys, trader.app.web_main\n"
        "bad = [m for m in sys.modules if m.startswith("
        "('trader.broker','trader.schwab','trader.execution','trader.auth'))]\n"
        "print(';'.join(sorted(bad)))\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    leaked = [m for m in out.stdout.strip().split(";") if m]
    assert leaked == [], leaked
