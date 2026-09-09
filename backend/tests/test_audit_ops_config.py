"""Operational configuration regressions; only temporary files and mocked processes."""
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.errors import ConfigError, ConflictError
from app.core.store import SettingsStore
from app.modules.settings import SettingsService
from app.modules.storage import StorageManager
from app.modules.updater import Updater


@pytest.fixture
def service(tmp_path):
    return SettingsService(SettingsStore(tmp_path / "settings.json"))


def test_node_detail_and_enrollment_preserve_pool_bandwidth(service):
    service.add_node({"name": "edge-test"})
    service.update_node_pool("edge-test", {"bandwidth_mbps": 420})
    before = service.node("edge-test")
    service.update_node("edge-test", {"cache_size": "200G"})
    service.apply_enroll_report(before.enroll_token, {"host": "test-host"})
    after = service.node("edge-test")
    assert after.bandwidth_mbps == 420
    assert after.sign_secret == before.sign_secret
    assert after.enroll_token == before.enroll_token


def test_telegram_can_pause_without_erasing_channels_or_secret(service):
    service.save_telegram({"enabled": True, "bot_token": "123456:test-only-credential",
                           "allow_invite": False})
    paused = service.save_telegram({"enabled": False})
    assert not paused["enabled"] and not paused["registration_open"]
    service.save_telegram({"menu_logo_url": "https://artwork.invalid/logo.png"})
    resumed = service.save_telegram({"enabled": True})
    assert resumed["allow_admin_grant"] and not resumed["allow_invite"]
    assert resumed["bot_token_set"]
    assert "test-only-credential" not in str(resumed)


@pytest.mark.parametrize("url", ["http://", "https://user:pw@host.invalid", "https://host.invalid:bad",
                                 "https://host.invalid\n/path", 123, "https://[broken"])
def test_emby_rejects_invalid_or_credential_bearing_urls_before_save(service, url):
    before = service._store.document()
    with pytest.raises(ConfigError):
        service.save_emby({"url": url})
    assert service._store.document() == before


@pytest.mark.parametrize("timeout", ["bad", -1, 0, 121, float("inf"), float("nan")])
def test_probe_timeout_uses_save_validation(service, timeout):
    service.save_emby({"url": "https://media.invalid"})
    with pytest.raises(ConfigError):
        service.resolve_probe_target({"timeout_seconds": timeout})


@pytest.fixture
def storage(tmp_path):
    cfg = SimpleNamespace(rclone_config_path=str(tmp_path / "rclone.conf"),
                          mount_root=str(tmp_path / "mounts"),
                          systemd_unit_dir=str(tmp_path / "units"),
                          systemd_unit_prefix="mediadeck-", rclone_binary="rclone",
                          cache_root=str(tmp_path / "cache"))
    manager = StorageManager(cfg)
    manager._run = Mock(return_value=subprocess.CompletedProcess([], 0, "inactive", ""))
    manager.add_remote("test-drive", "drive", {"token": "test-only-secret"})
    return manager


def test_rclone_file_and_replacements_are_private(storage):
    assert os.stat(storage._settings.rclone_config_path).st_mode & 0o777 == 0o600
    storage.add_remote("other", "s3", {"secret_access_key": "test-only-key"})
    assert os.stat(storage._settings.rclone_config_path).st_mode & 0o777 == 0o600
    assert "test-only-secret" not in str(storage.list_remotes())


@pytest.mark.parametrize("options", [{"token\n[other]": "x"}, {"token": "x\n[other]\ntype = drive"}])
def test_rclone_rejects_ini_injection_without_mutation(storage, options):
    from pathlib import Path
    path = Path(storage._settings.rclone_config_path)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        storage.add_remote("new", "drive", options)
    assert path.read_bytes() == before


@pytest.mark.parametrize("patch", [{"target": "safe\nExecStart=/bin/false"},
                                   {"vfs_cache_max_size": "1G\nExecStart=/bin/false"},
                                   {"remote_path": "bad\x00path"}])
def test_mount_validation_precedes_all_effects(storage, patch):
    from pathlib import Path
    spec = {"name": "media-test", "remote": "test-drive", "target": "media-test", **patch}
    with pytest.raises(ValueError):
        storage.create_mount(spec)
    assert not Path(storage._settings.systemd_unit_dir).exists()
    assert not Path(storage._settings.mount_root).exists()
    storage._run.assert_not_called()


def test_unknown_remote_is_rejected_before_creating_mount(storage):
    with pytest.raises(ValueError, match="remote"):
        storage.create_mount({"name": "media-test", "remote": "missing", "target": "media-test"})
    storage._run.assert_not_called()


def test_unit_quotes_systemd_expansions_and_preserves_failed_stop(storage):
    from pathlib import Path
    spec = {"name": "media-test", "remote": "test-drive", "target": "media-test",
            "remote_path": "folder %i $HOME"}
    storage.create_mount(spec)
    path = Path(storage._unit_path("media-test"))
    text = path.read_text()
    command = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    assert "%%i" in command and "$$HOME" in command
    storage._run.return_value = subprocess.CompletedProcess([], 1, "", "stop failed")
    with pytest.raises(RuntimeError):
        storage.delete_mount("media-test")
    assert path.exists()


def test_mount_create_refuses_overwrite_of_existing_unit(storage):
    spec = {"name": "media-test", "remote": "test-drive", "target": "media-test"}
    storage.create_mount(spec)
    with pytest.raises(ConflictError):
        storage.create_mount(spec)


def test_updater_process_errors_are_bounded_and_safe(monkeypatch, tmp_path):
    import app.modules.updater as module
    monkeypatch.setattr(module.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("git", 1)))
    spawn = Mock()
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    updater = Updater(str(tmp_path))
    assert updater.version() == {"version": "unknown", "commit": "unknown"}
    assert updater.check()["ok"] is False
    assert updater.update()["started"] is False
    spawn.assert_not_called()


def test_settings_false_strings_cannot_enable_billing_or_dispatch(service):
    assert service.save_metering({"baseline_confirmed": "false", "cutover": "false"})["cutover"] is False
    assert service.save_playback({"enabled": "false"})["enabled"] is False
    service.save_emby({"url": "https://media.invalid", "enabled": "false", "verify_ssl": "false"})
    assert service.emby_config()["enabled"] is False
    assert service.emby_config()["verify_ssl"] is False
    with pytest.raises(ConfigError):
        service.save_metering({"cutover": "unexpected"})


def test_node_opaque_secret_keep_and_mount_ids_validation(service):
    service.add_node({"name": "test-edge", "rclone_conf": "[test]\ntype = drive\ntoken = fake-only"})
    original = service.node("test-edge").rclone_conf
    service.update_node("test-edge", {"rclone_conf": "__KEEP__"})
    assert service.node("test-edge").rclone_conf == original
    with pytest.raises(ConfigError):
        service.update_node("test-edge", {"mount_ids": "not-a-list"})


def test_updater_quotes_local_paths_and_service_and_never_forces_checkout(monkeypatch, tmp_path):
    import shlex

    import app.modules.updater as module
    updater = Updater(str(tmp_path / "repo path; literal"), "panel;literal")
    monkeypatch.setattr(updater, "check", lambda: {"ok": True, "latest": "v1.2.3", "update_available": True})
    monkeypatch.setattr(module, "_run", lambda *a, **kw: (0, ""))
    spawn = Mock()
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    assert updater.update()["started"] is True
    script = spawn.call_args.args[0][-1]
    assert shlex.quote(str(updater._root)) in script
    assert shlex.quote(updater._service) in script
    assert "--force" not in script


def test_updater_refuses_dirty_worktree_and_invalid_target_without_spawn(monkeypatch, tmp_path):
    import app.modules.updater as module
    updater = Updater(str(tmp_path))
    monkeypatch.setattr(updater, "check", lambda: {"ok": True, "latest": "v1.2.3", "update_available": True})
    monkeypatch.setattr(module, "_run", lambda *a, **kw: (0, " M backend/app/main.py"))
    spawn = Mock()
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    assert updater.update()["started"] is False
    assert updater.update("v1.2.3; false")["started"] is False
    spawn.assert_not_called()
