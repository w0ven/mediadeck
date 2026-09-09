"""Fault-injection regressions for the durable settings contract."""
import json
from unittest.mock import patch

import pytest

from app.core.errors import ConfigError
from app.core.store import SettingsStore


def test_failed_replace_keeps_live_and_disk_settings(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.set_section("emby", {"enabled": False, "url": "http://old.invalid"})
    before = store.document()
    disk = store.path.read_bytes()
    with (patch("app.core.store.os.replace", side_effect=OSError("disk failure")),
          pytest.raises(OSError)):
        store.set_section("emby", {"enabled": True, "url": "http://new.invalid"})
    assert store.document() == before
    assert store.path.read_bytes() == disk
    assert not list(tmp_path.glob(".settings-*.tmp"))
    store.set("nodes", [{"name": "new-node"}])
    reloaded = SettingsStore(store.path)
    assert reloaded.get("nodes") == store.get("nodes")
    assert reloaded.section("emby")["url"] == "http://old.invalid"
    assert json.loads(store.path.read_text()) == store.document()


def test_failed_save_does_not_publish_timestamp(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    before = store.document()
    with (patch("app.core.store.os.replace", side_effect=OSError("disk failure")),
          pytest.raises(OSError)):
        store.save()
    assert store.document() == before
    assert not store.loaded_from_disk


@pytest.mark.parametrize("payload", ["{broken", "[]", "null", "", "123"])
def test_existing_invalid_settings_are_never_bootstrapped_over(tmp_path, payload):
    path = tmp_path / "settings.json"
    path.write_text(payload)
    with pytest.raises(ConfigError, match="设置文件"):
        SettingsStore(path)
    assert path.read_text() == payload


def test_unreadable_settings_are_not_treated_as_first_run(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}")
    with (patch("pathlib.Path.read_text", side_effect=PermissionError("private value")),
          pytest.raises(ConfigError, match="设置文件") as caught):
        SettingsStore(path)
    assert "private value" not in str(caught.value)


def test_first_run_staged_bootstrap_and_permissions_unchanged(tmp_path):
    path = tmp_path / "nested" / "settings.json"
    store = SettingsStore(path)
    assert not store.loaded_from_disk
    store.set_section("emby", {"enabled": False}, persist=False)
    store.set("nodes", [], persist=False)
    assert not path.exists()
    store.save()
    assert store.loaded_from_disk
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == store.document()
