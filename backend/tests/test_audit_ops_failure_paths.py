"""Direct regression probes for boundary failures and recovery; no live services."""
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.core.errors import ConfigError
from app.core.store import SettingsStore
from app.modules.downloaders import QbittorrentClient
from app.modules.events import EventStream
from app.modules.intake import FsReader, IntakeCollector
from app.modules.intake_plugin import IntakePipelinePlugin, IntakeStore
from app.modules.mounts import MountsReader
from app.modules.pipeline import PipelineReader
from app.modules.plugins import Field
from app.modules.plugins_builtin import GroupAuditPlugin, PluginContext
from app.modules.settings import SettingsService
from app.modules.storage import StorageManager
from app.modules.tasks import TasksReader
from app.modules.updater import Updater


@pytest.mark.parametrize("reader", [MountsReader, PipelineReader, TasksReader])
def test_reader_permission_error_and_stale_state(reader, tmp_path, monkeypatch):
    path = tmp_path / "snapshot.json"
    path.write_text("{}")
    os.utime(path, (time.time() - 900, time.time() - 900))
    assert reader(str(path)).snapshot()["stale"] is True
    monkeypatch.setattr(Path, "is_file", Mock(side_effect=PermissionError("private-path")))
    result = reader(str(path)).snapshot()
    assert result["available"] is False
    assert "private-path" not in str(result)


@pytest.mark.parametrize("reader,key", [(MountsReader, "mounts"), (PipelineReader, "queues"), (TasksReader, "tasks")])
def test_reader_rejects_wrong_known_collection_shape(reader, key, tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({key: {"bad": 1}}))
    assert reader(str(path)).snapshot()["available"] is False


def test_filesystem_count_never_asserts_a_capped_total(tmp_path):
    (tmp_path / "a").write_text("a")
    (tmp_path / "b").write_text("b")
    assert FsReader().walk_files(str(tmp_path), limit=1) is None
    assert FsReader().walk_files(str(tmp_path), limit=2) == (2, 2)
    (tmp_path / "cycle").symlink_to(tmp_path, target_is_directory=True)
    assert FsReader().walk_files(str(tmp_path), limit=4) == (2, 2)


@pytest.mark.asyncio
async def test_intake_cancel_retains_prior_good_snapshot(monkeypatch):
    entered = asyncio.Event()

    async def slow(self, clients):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(IntakeCollector, "snapshot", slow)
    store = IntakeStore()
    store.put({"prior": True})
    plugin = IntakePipelinePlugin(SimpleNamespace(intake_store=store))
    task = asyncio.create_task(plugin.run({}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get()["data"] == {"prior": True}
    assert "cancelled" in store.get()["error"]


@pytest.mark.asyncio
async def test_warning_progress_survives_mid_batch_cancellation():
    members = [{"emby_user_id": "one"}, {"emby_user_id": "two"}]
    waiting = asyncio.Event()

    async def notify(member, text):
        if member["emby_user_id"] == "one":
            return True
        waiting.set()
        await asyncio.Event().wait()

    bot = SimpleNamespace(enabled=True, notify_member=notify,
                          audit_group_membership=AsyncMock(return_value={"checked": 2, "left": members}))
    ctx = PluginContext(telegram=bot)
    task = asyncio.create_task(GroupAuditPlugin(ctx).run({"action": "notify", "grace_days": 3}))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert set(ctx.state("group_audit")) == {"one"}


@pytest.mark.asyncio
async def test_sse_no_duplicates_keepalive_and_producer_recovers(monkeypatch):
    monkeypatch.setattr("app.modules.events.KEEPALIVE_INTERVAL", .04)
    calls = 0

    async def producer():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("unavailable")
        return {"v": 1}

    stream = EventStream({"test": producer}, interval=.005).iterate(["test", "test"])
    try:
        assert (await asyncio.wait_for(anext(stream), .2)).startswith("event: test\n")
        assert await asyncio.wait_for(anext(stream), .2) == ": keepalive\n\n"
    finally:
        await stream.aclose()


def test_configuration_saves_are_independent_and_secret_safe(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    service = SettingsService(store)
    service.save_emby({"url": "https://media.invalid", "api_key": "test-only-key"})
    service.save_integration({"tmdb_api_key": "test-only-tmdb"})
    service.save_image_cache({"max_gib": 7})
    service.save_membership({"retention_days": 900})
    assert service.emby_config()["api_key"] == "test-only-key"
    assert service.integration_config()["tmdb_api_key"] == "test-only-tmdb"
    service.save_integration({"tmdb_language": "en-US"})
    assert service.integration_config()["tmdb_api_key"] == "test-only-tmdb"
    assert "test-only-tmdb" not in str(service.integration_public())
    assert service.image_cache_config()["max_gib"] == 7
    with pytest.raises(ConfigError):
        service.save_image_cache({"max_gib": float("inf")})
    assert service.image_cache_config()["max_gib"] == 7


@pytest.mark.parametrize("number", [1.5, float("inf"), float("nan")])
def test_plugin_integer_field_rejects_fractional_and_nonfinite_values(number):
    with pytest.raises(ValueError):
        Field("test", "test", kind="int").coerce(number)


@pytest.fixture
def storage(tmp_path):
    cfg = SimpleNamespace(rclone_config_path=str(tmp_path / "rclone.conf"),
                          mount_root=str(tmp_path / "mounts"), systemd_unit_dir=str(tmp_path / "units"),
                          systemd_unit_prefix="panel-", rclone_binary="rclone", cache_root="")
    manager = StorageManager(cfg)
    manager._run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    manager.add_remote("test-drive", "drive", {"token": "test-only-credential"})
    return manager


def test_storage_failed_write_preserves_document_and_removes_secret_temp(storage, monkeypatch):
    path = Path(storage._settings.rclone_config_path)
    before = path.read_bytes()
    monkeypatch.setattr("app.modules.storage.os.replace", Mock(side_effect=OSError("test failure")))
    with pytest.raises(OSError):
        storage.add_remote("new-drive", "drive", {})
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".rclone-*.tmp"))


def test_storage_corrupt_existing_config_is_not_replaced(storage):
    path = Path(storage._settings.rclone_config_path)
    path.write_text("bad syntax with test-only-credential")
    with pytest.raises(RuntimeError, match="cannot read"):
        storage.add_remote("new-drive", "drive", {})
    assert path.read_text() == "bad syntax with test-only-credential"


def test_remote_error_never_echoes_subprocess_stderr(storage):
    storage._run.return_value = subprocess.CompletedProcess([], 1, "", "token=test-only-credential")
    result = storage.test_remote("test-drive")
    assert not result["ok"]
    assert "test-only-credential" not in str(result)


def test_mount_failed_reload_is_retryable(storage):
    spec = {"name": "test-mount", "remote": "test-drive", "target": "test-mount"}
    storage._run.return_value = subprocess.CompletedProcess([], 1, "", "test failure")
    with pytest.raises(RuntimeError):
        storage.create_mount(spec)
    assert not Path(storage._unit_path("test-mount")).exists()
    storage._run.return_value = subprocess.CompletedProcess([], 0, "", "")
    assert storage.create_mount(spec)["status"] == "inactive"


@pytest.mark.asyncio
async def test_downloader_auth_rejection_and_transport_failures_are_safe(monkeypatch):
    original = httpx.AsyncClient

    def handle(request):
        if request.url.path.endswith("login"):
            return httpx.Response(200, text="Fails.")
        return httpx.Response(403)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    row = (await IntakeCollector().collect_downloader([QbittorrentClient("test", "https://download.invalid")]))["clients"][0]
    assert not row["available"] and row["reason"] == "PermissionError"


def test_updater_refuses_overlapping_helpers_and_retries_after_exit(monkeypatch, tmp_path):
    import app.modules.updater as module
    updater = Updater(str(tmp_path))
    monkeypatch.setattr(updater, "check", lambda: {"ok": True, "latest": "v1.2.3", "update_available": True})
    monkeypatch.setattr(module, "_run", lambda *a, **kw: (0, ""))
    helper = Mock()
    helper.poll.return_value = None
    spawn = Mock(return_value=helper)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    assert updater.update()["started"]
    assert not updater.update()["started"]
    assert spawn.call_count == 1
    helper.poll.return_value = 1
    assert updater.update()["started"]
    assert spawn.call_count == 2


def test_updater_helper_launch_failure_does_not_report_started(monkeypatch, tmp_path):
    import app.modules.updater as module
    updater = Updater(str(tmp_path))
    monkeypatch.setattr(updater, "check", lambda: {"ok": True, "latest": "v1.2.3", "update_available": True})
    monkeypatch.setattr(module, "_run", lambda *a, **kw: (0, ""))
    monkeypatch.setattr(module.subprocess, "Popen", Mock(side_effect=OSError("test-only-secret")))
    result = updater.update()
    assert result["started"] is False
    assert "test-only-secret" not in str(result)
