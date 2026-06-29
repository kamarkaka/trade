"""Shared pytest configuration and fixtures.

Test markers (unit / integration / network) are registered in ``pyproject.toml``
under ``[tool.pytest.ini_options]``. Shared fixtures and the M0.8 test doubles
are added here as later milestones need them.
"""

from collections.abc import Iterator

import pytest

from trader.observability.logging import clear_secrets


@pytest.fixture(autouse=True)
def _isolate_secret_registry() -> Iterator[None]:
    """Keep the process-global scrub-literal registry from leaking across tests.

    ``register_secret`` writes to module-global state; without this, literals
    registered by one test persist for the rest of the run (a latent footgun and
    an unbounded set). Clear before and after every test for a clean slate.
    """
    clear_secrets()
    yield
    clear_secrets()
