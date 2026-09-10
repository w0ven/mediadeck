"""Failure, retry, cancellation and restart checks using a real temporary DB."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.core.db import Database
from app.core.store import SettingsStore
from app.modules.events import EventStream, safe_stream
from app.modules.imports import ImportManager, JobKind, JobState, MockExecutor
from app.modules.plugins import Plugin, PluginRegistry, Spec
from app.modules.plugins_builtin import (
    ExpiryReminderPlugin,
    GroupAuditPlugin,
    InactiveCleanupPlugin,
    PluginContext,
    RequestDigestPlugin,
    ViewingReportPlugin,
)


class Daily(Plugin):
    spec = Spec(id="audit-daily", name="test", description="", hour=0)

    async def run(self, config):
        return {"ok": True}


@pytest.fixture
def registry(tmp_path):
    db = Database(tmp_path / "ops.db")
    reg = PluginRegistry(SettingsStore(tmp_path / "settings.json"), db)
    reg.register(Daily(None))
    reg.save("audit-daily", enabled=True)
    yield reg
    db.close()


@pytest.mark.asyncio
async def test_daily_success_survives_restart_and_failure_is_retryable(registry, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr("app.modules.plugins.time.time", lambda: clock[0])
    plugin = registry.get("audit-daily")
    plugin.run = AsyncMock(side_effect=[{"ok": False}, {"ok": True}])
    await registry.tick(clock[0])
    clock[0] += 120
    assert await registry.tick(clock[0]) == ["audit-daily"]
    assert plugin.run.await_count == 2
    restored = PluginRegistry(registry._store, registry._db)
    restored.register(Daily(None))
    assert await restored.tick(clock[0] + 120) == []


@pytest.mark.asyncio
async def test_busy_manual_run_does_not_consume_daily_slot(registry):
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(config):
        entered.set()
        await release.wait()
        return {"ok": True}

    registry.get("audit-daily").run = slow
    manual = asyncio.create_task(registry.run_now("audit-daily"))
    await entered.wait()
    assert await registry.tick() == []
    assert "audit-daily" not in registry._last_daily
    release.set()
    await manual


@pytest.mark.asyncio
async def test_cancellation_records_failure_and_releases_lock(registry):
    entered = asyncio.Event()

    async def slow(config):
        entered.set()
        await asyncio.Event().wait()

    registry.get("audit-daily").run = slow
    task = asyncio.create_task(registry.run_now("audit-daily"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not registry.card("audit-daily")["running"]
    assert registry.history("audit-daily")[0]["ok"] == 0
    registry.get("audit-daily").run = AsyncMock(return_value={"ok": True})
    assert (await registry.run_now("audit-daily"))["ok"]


@pytest.mark.asyncio
async def test_one_slow_plugin_does_not_starve_another_in_tick(registry):
    release, second_ran = asyncio.Event(), asyncio.Event()

    async def slow(config):
        await release.wait()
        return {}

    class Other(Plugin):
        spec = Spec(id="audit-other", name="other", description="", interval=60)

        async def run(self, config):
            second_ran.set()
            return {}

    registry.get("audit-daily").run = slow
    registry.register(Other(None))
    registry.save("audit-other", enabled=True)
    tick = asyncio.create_task(registry.tick())
    try:
        await asyncio.wait_for(second_ran.wait(), .2)
    finally:
        release.set()
        await tick


@pytest.mark.asyncio
async def test_large_summary_remains_valid_json_and_latest_has_tie_breaker(registry):
    plugin = registry.get("audit-daily")
    plugin.run = AsyncMock(return_value={"message": "x" * 6000})
    await registry.run_now("audit-daily")
    row = registry._db.one("SELECT summary FROM plugin_runs ORDER BY id DESC LIMIT 1")
    assert isinstance(json.loads(row["summary"]), dict)
    plugin.run = AsyncMock(return_value={"newest": True})
    await registry.run_now("audit-daily")
    assert registry.last_result("audit-daily")["summary"] == {"newest": True}


@pytest.mark.asyncio
async def test_plugin_http_errors_never_store_credential_urls(registry):
    request = httpx.Request("GET", "https://service.invalid/path?api_key=test-only-credential")
    response = httpx.Response(500, request=request)
    error = httpx.HTTPStatusError(str(request.url), request=request, response=response)
    registry.get("audit-daily").run = AsyncMock(side_effect=error)
    result = await registry.run_now("audit-daily")
    assert result["ok"] is False
    assert "test-only-credential" not in str(result)
    assert "test-only-credential" not in str(registry.history("audit-daily"))


@pytest.mark.asyncio
async def test_timeout_is_visible_and_manual_retry_works(registry, monkeypatch):
    monkeypatch.setattr("app.modules.plugins.RUN_TIMEOUT", .01)
    registry.get("audit-daily").run = AsyncMock(side_effect=lambda config: None)

    async def never(config):
        await asyncio.Event().wait()

    registry.get("audit-daily").run = never
    assert not (await registry.run_now("audit-daily"))["ok"]
    assert registry.history("audit-daily")[0]["ok"] == 0
    registry.get("audit-daily").run = AsyncMock(return_value={})
    assert (await registry.run_now("audit-daily"))["ok"]


@pytest.mark.asyncio
async def test_unsent_warning_never_starts_suspension_clock():
    member = {"emby_user_id": "test-member", "tg_user_id": "123456", "status": "active",
              "last_seen_at": time.time() - 40 * 86400}
    bot = SimpleNamespace(enabled=True, notify_member=AsyncMock(return_value=False),
                          audit_group_membership=AsyncMock(return_value={"checked": 1, "left": [member]}))
    members = SimpleNamespace(list=lambda **kw: [member], set_status=Mock())
    ctx = PluginContext(telegram=bot, members=members)
    plugin = GroupAuditPlugin(ctx)
    config = {"action": "suspend", "grace_days": 0}
    result = await plugin.run(config)
    assert not ctx.state(plugin.spec.id)
    assert result["ok"] is False
    bot.notify_member.return_value = True
    await plugin.run(config)
    assert bot.notify_member.await_count == 2


@pytest.mark.asyncio
async def test_inactive_cleanup_suspends_without_member_notice():
    member = {"emby_user_id": "test-member", "tg_user_id": "123456", "status": "active",
              "last_seen_at": time.time() - 40 * 86400}
    bot = SimpleNamespace(enabled=True, notify_member=AsyncMock(return_value=False))
    members = SimpleNamespace(list=lambda **kw: [member], set_status=Mock())
    result = await InactiveCleanupPlugin(PluginContext(telegram=bot, members=members)).run({"days": 7})
    assert result["已停用"] == 1
    members.set_status.assert_called_once()
    bot.notify_member.assert_not_called()


@pytest.mark.asyncio
async def test_stop_cancels_manual_runs_even_without_scheduler_start(registry):
    entered = asyncio.Event()

    async def slow(config):
        entered.set()
        await asyncio.Event().wait()

    registry.get("audit-daily").run = slow
    task = asyncio.create_task(registry.run_now("audit-daily"))
    await entered.wait()
    await registry.stop()
    try:
        assert task.done()
        assert not registry.card("audit-daily")["running"]
        assert registry.history("audit-daily")[0]["ok"] == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_inactive_cleanup_whitelist_is_exempt():
    member = {"emby_user_id": "test-member", "tg_user_id": "123456", "status": "active",
              "group_id": "whitelist", "last_seen_at": time.time() - 120 * 86400}
    members = SimpleNamespace(list=lambda **kw: [member], set_status=Mock())
    result = await InactiveCleanupPlugin(PluginContext(members=members)).run({"days": 7})
    assert result["已停用"] == 0 and result["白名单豁免"] == 1
    members.set_status.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin_cls", [ViewingReportPlugin, ExpiryReminderPlugin, RequestDigestPlugin])
async def test_partial_delivery_retries_only_unfinished_recipients(plugin_cls, tmp_path):
    people = [{"emby_user_id": "one", "tg_user_id": "one"},
              {"emby_user_id": "two", "tg_user_id": "two"}]
    calls = []
    fail = [True]

    async def send(chat, text):
        calls.append(chat)
        return not (chat == "two" and fail[0])

    async def notify(member, text):
        return await send(member["tg_user_id"], text)

    async def expiring(members):
        return sum([await notify(member, "") for member in members])

    ctx = PluginContext(
        telegram=SimpleNamespace(enabled=True, send=send, notify_member=notify, notify_expiring=expiring),
        members=SimpleNamespace(linked_telegram=lambda: people, expiring_within=lambda days: people),
        stats=SimpleNamespace(member_detail=lambda *a, **kw: {"series": [{"plays": 1}]}),
        requests=SimpleNamespace(stats=lambda: {"open": 1}, uploaders=lambda: people))
    ctx.store = SettingsStore(tmp_path / "settings.json")
    plugin = plugin_cls(ctx)
    first = await plugin.run(plugin.defaults())
    assert first["ok"] is False
    assert calls == ["one", "two"]
    fail[0] = False
    ctx.store = SettingsStore(tmp_path / "settings.json")
    plugin = plugin_cls(ctx)  # reconstruct after the interrupted/failed batch
    assert (await plugin.run(plugin.defaults()))["ok"] is True
    assert calls == ["one", "two", "two"]


def test_viewing_report_escapes_external_titles():
    text = ViewingReportPlugin._text("周报", 7, 1, 1, 0,
                                    {"recent_plays": [{"item_name": "<b>Example & title</b>"}]})
    assert "&lt;b&gt;Example &amp; title&lt;/b&gt;" in text


def test_live_import_without_executor_is_not_a_phantom_queued_job():
    manager = ImportManager()
    with pytest.raises(ValueError, match="executor|configured"):
        manager.submit(JobKind.DRIVE_LINK, "test-reference")
    assert manager.list() == []


def test_import_start_and_refresh_failures_are_visible_and_cancel_failure_not_final():
    executor = Mock()
    executor.start.side_effect = RuntimeError("private upstream detail")
    manager = ImportManager(executor)
    job = manager.submit(JobKind.DRIVE_LINK, "test-reference")
    assert job.state == JobState.FAILED
    assert "private upstream detail" not in job.error

    executor.start.side_effect = lambda j: setattr(j, "state", JobState.RUNNING)
    job = manager.submit(JobKind.DRIVE_LINK, "test-reference-2")
    executor.refresh.side_effect = RuntimeError("private upstream detail")
    assert manager.get(job.id).error
    executor.refresh.side_effect = lambda j: None
    assert manager.get(job.id).error == ""
    executor.cancel.side_effect = RuntimeError("private upstream detail")
    with pytest.raises(RuntimeError):
        manager.cancel(job.id)
    assert job.state == JobState.RUNNING
    executor.cancel.side_effect = None
    assert manager.cancel(job.id)
    assert manager.get(job.id).state == JobState.FAILED


def test_mock_import_terminal_cancel_is_idempotent():
    manager = ImportManager(MockExecutor())
    job = manager.submit(JobKind.CLOUD_DRIVE, "test-reference")
    assert manager.cancel(job.id)
    assert not manager.cancel(job.id)
    assert manager.get(job.id).state == JobState.FAILED


@pytest.mark.asyncio
async def test_sse_slow_topic_does_not_hide_fast_topic_and_closes_producers():
    cancelled = asyncio.Event()

    async def slow():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    stream = EventStream({"slow": slow, "fast": AsyncMock(return_value={"value": 1})}, interval=.01)
    iterator = safe_stream(stream.iterate(["slow", "fast", "fast"]))
    try:
        frame = await asyncio.wait_for(anext(iterator), .2)
        assert frame.startswith("event: fast\n")
    finally:
        await iterator.aclose()
    await asyncio.wait_for(cancelled.wait(), .2)
