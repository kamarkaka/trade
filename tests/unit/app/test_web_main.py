"""``trader-web`` entrypoint: logging is configured (scrubbed, no frame locals) before the
server starts."""

import pytest
import structlog

from trader.app import web_main
from trader.observability.logging import _scrub_processor


def test_main_configures_logging_before_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    seen: dict[str, object] = {}

    def _fake_run(app: object, **kwargs: object) -> None:
        seen["processors"] = structlog.get_config()["processors"]
        seen["kwargs"] = kwargs

    structlog.reset_defaults()
    monkeypatch.setattr(uvicorn, "run", _fake_run)
    monkeypatch.setattr(web_main.WebSettings, "from_env", classmethod(lambda cls, env: object()))
    monkeypatch.setattr(web_main, "create_app", lambda settings: "app")
    web_main.main()
    assert _scrub_processor in seen["processors"]  # type: ignore[operator]
    assert seen["kwargs"] == {"host": "0.0.0.0", "port": 8000, "log_level": "info"}
