"""Malformed collectors and integrations must degrade, not fabricate healthy zeroes."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.modules.downloaders import QbittorrentClient
from app.modules.intake import FsReader, IntakeCollector, IntakePaths
from app.modules.intake_plugin import IntakePipelinePlugin, IntakeStore
from app.modules.mounts import MountsReader
from app.modules.pipeline import PipelineReader
from app.modules.tasks import TasksReader


@pytest.mark.parametrize("reader", [MountsReader, PipelineReader, TasksReader])
@pytest.mark.parametrize("content", [b"\xff", b"[]", b"null", b"broken"])
def test_snapshot_readers_degrade_invalid_data(reader, content, tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_bytes(content)
    assert reader(str(path)).snapshot()["available"] is False


@pytest.mark.parametrize("reader", [MountsReader, PipelineReader, TasksReader])
def test_snapshot_file_removed_between_read_and_stat_is_not_fatal(reader, monkeypatch, tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text("{}")
    original = Path.read_text

    def remove_after_read(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if self == path:
            self.unlink()
        return value

    monkeypatch.setattr(Path, "read_text", remove_after_read)
    assert reader(str(path)).snapshot()["available"] is False


@pytest.mark.asyncio
async def test_bad_emby_payload_degrades_only_its_subsection():
    emby = SimpleNamespace(scheduled_tasks=AsyncMock(return_value=[{"Key": "RefreshLibrary", "LastExecutionResult": "bad"}]),
                           latest_created=AsyncMock(return_value={"Items": {"bad": 1}}),
                           server_log_tail=AsyncMock(return_value=123))
    snapshot = await IntakeCollector(emby=emby).snapshot()
    for name in ("scan", "latest", "probe"):
        assert snapshot["emby"][name]["available"] is False
    assert "cloud" in snapshot


def test_malformed_refresh_entry_does_not_break_other_entries(tmp_path):
    (tmp_path / "bad.json").write_text(json.dumps({"paths": 1, "event_count": float("inf")}))
    (tmp_path / "good.json").write_text(json.dumps({"paths": ["test-file"], "event_count": 2}))
    result = IntakeCollector(paths=IntakePaths(refresh_queue_dir=str(tmp_path))).collect_refresh()
    assert result["total"] == 2
    assert result["unreadable"] == 1
    assert result["parsed"] == 1


def test_intake_store_copies_and_failure_keeps_good_snapshot():
    store = IntakeStore()
    original = {"health": {"level": "ok"}}
    store.put(original)
    original["health"]["level"] = "bad"
    assert store.get()["data"]["health"]["level"] == "ok"
    store.get()["data"]["health"]["level"] = "bad"
    store.fail("test failure")
    assert store.get()["data"]["health"]["level"] == "ok"
    assert store.get()["error"] == "test failure"


@pytest.mark.asyncio
async def test_intake_filesystem_work_does_not_block_event_loop(monkeypatch):
    import time
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    original = IntakeCollector.collect_refresh

    def slow(self):
        loop.call_soon_threadsafe(entered.set)
        time.sleep(.08)
        return original(self)

    monkeypatch.setattr(IntakeCollector, "collect_refresh", slow)
    store = IntakeStore()
    task = asyncio.create_task(IntakePipelinePlugin(SimpleNamespace(intake_store=store)).run({}))
    await entered.wait()
    assert not task.done(), "filesystem read blocked all request handling until completion"
    await task


@pytest.mark.asyncio
async def test_downloader_invalid_http_payload_is_unavailable_not_empty(monkeypatch):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"error": "bad gateway payload"}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    data = await IntakeCollector().collect_downloader([QbittorrentClient("test", "https://download.invalid")])
    assert data["clients"][0]["available"] is False


@pytest.mark.asyncio
async def test_downloader_login_retry_and_credentials_never_in_summary(monkeypatch):
    original = httpx.AsyncClient
    seen = []

    def handle(request):
        seen.append(request.url.path)
        if len(seen) == 1:
            return httpx.Response(403)
        if request.url.path.endswith("login"):
            assert b"password=test-only-password" in request.content
            return httpx.Response(200, text="Ok.", headers={"set-cookie": "SID=test-session; Path=/"})
        assert request.headers["cookie"] == "SID=test-session"
        return httpx.Response(200, json=[{"progress": 1, "total_size": 12}])

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    result = await QbittorrentClient("test", "https://download.invalid", "test-user", "test-only-password").summary()
    assert result["completed_bytes"] == 12 and len(seen) == 3
    assert "test-only-password" not in str(result)


def test_tail_caps_even_a_single_unterminated_line(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text("a" * 10000)
    assert len(FsReader().tail(path, 100)) <= 100
