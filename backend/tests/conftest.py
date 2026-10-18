"""Test isolation.

The panel persists operator settings to a JSON document on disk.  Each test
gets its own data directory so tests never inherit another test's saved Emby
connection, nodes or dispatch policy.
"""
from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from app.core.config import settings
from app.modules.economy_rules import day_bounds


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("MEDIADECK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MEDIADECK_MOCK", "1")
    monkeypatch.setenv("MEDIADECK_ADMIN_USER", "admin")
    monkeypatch.setenv("MEDIADECK_ADMIN_PASSWORD", "change-me")
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
def checkin_clock(monkeypatch):
    # A 600-second proof cannot fit inside the first ten minutes of a new day.
    noon = day_bounds(time.time())[1] + 12 * 3600
    monkeypatch.setattr(time, "time", lambda: noon)
