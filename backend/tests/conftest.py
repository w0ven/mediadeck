"""Test isolation.

The panel persists operator settings to a JSON document on disk.  Each test
gets its own data directory so tests never inherit another test's saved Emby
connection, nodes or dispatch policy.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("MEDIADECK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MEDIADECK_MOCK", "1")
    monkeypatch.setenv("MEDIADECK_ADMIN_USER", "admin")
    monkeypatch.setenv("MEDIADECK_ADMIN_PASSWORD", "change-me")
    settings.cache_clear()
    yield
    settings.cache_clear()
