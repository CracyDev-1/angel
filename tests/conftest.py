"""Pytest configuration.

Trailing stop reads :func:`angel_bot.config.get_settings` (lru-cached). Force it
off by default so paper/live exit tests stay deterministic regardless of a dev's
``.env``.
"""

from __future__ import annotations

import pytest

from angel_bot.config import get_settings


@pytest.fixture(autouse=True)
def _trail_stop_disabled_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAIL_STOP_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
