"""Verify the package imports and the console-script entry point is wired."""

import pytest


@pytest.mark.unit
def test_package_version() -> None:
    import trader

    assert isinstance(trader.__version__, str)


@pytest.mark.unit
def test_console_script_importable() -> None:
    from trader.app.cli import app

    assert callable(app)
