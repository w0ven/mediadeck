"""Slow local database work must not occupy the ASGI event-loop thread."""
import asyncio
import contextvars
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app import main
from app.core.db import Database
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.stats import StatsService
from app.modules.usage import UsageSampler, run_usage_io


class SlowOperation:
    def __init__(self, result):
        self.result = result
        self.entered = threading.Event()
        self.release = threading.Event()
        self.thread = None

    def __call__(self, *args, **kwargs):
        self.thread = threading.get_ident()
        self.entered.set()
        self.release.wait(1)
        return self.result


async def assert_loop_free(coroutine, slow):
    loop_thread = threading.get_ident()
    task = asyncio.create_task(coroutine)
    try:
        assert await asyncio.to_thread(slow.entered.wait, 2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            assert (await client.get("/healthz")).status_code == 200
        assert slow.thread != loop_thread, "synchronous I/O still ran on the event loop"
        assert not task.done(), "blocked I/O should still be waiting while health responds"
    finally:
        slow.release.set()
        result = await task
    return result


@pytest.mark.parametrize("route,service,method,result", [
    ("/api/stats/overview", "stats", "overview", {"members": {"total": 1}}),
    ("/api/stats/daily", "stats", "daily_series", []),
    ("/api/stats/top-users", "stats", "top_users", []),
    ("/api/stats/top-titles", "stats", "top_titles", []),
    ("/api/stats/clients", "stats", "client_breakdown", []),
    ("/api/stats/nodes", "stats", "node_breakdown", []),
    ("/api/stats/play-methods", "stats", "play_method_breakdown", {}),
    ("/api/groups", "groups", "list", []),
])
def test_read_api_io_does_not_stall_health(monkeypatch, route, service, method, result):
    slow = SlowOperation(result)
    monkeypatch.setattr(main.app.state, service, SimpleNamespace(**{method: slow}), raising=False)

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test",
                                     auth=("admin", "change-me")) as client:
            response = await assert_loop_free(client.get(route), slow)
            assert response.status_code == 200 and response.json() == result
    asyncio.run(check())


def test_member_list_db_work_does_not_stall_other_pages(monkeypatch):
    slow = SlowOperation([{"emby_user_id": "u1", "username": "viewer", "state": "active"}])
    monkeypatch.setattr(main.app.state, "members", SimpleNamespace(list=slow, _db=SimpleNamespace(query=lambda *a: [{"emby_user_id": "u1"}])), raising=False)
    monkeypatch.setattr(main.app.state, "stats", SimpleNamespace(hours_this_month=lambda: {"u1": 1.0}), raising=False)
    monkeypatch.setattr(main.app.state, "points", SimpleNamespace(balances=lambda: {"u1": 2}), raising=False)
    monkeypatch.setattr(main.app.state, "ledger", SimpleNamespace(summary_for_users=dict), raising=False)
    monkeypatch.setattr(main.app.state, "emby", SimpleNamespace(list_users=AsyncMock(return_value=[{"Id": "u1", "Name": "viewer"}])), raising=False)

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test",
                                     auth=("admin", "change-me")) as client:
            response = await assert_loop_free(client.get("/api/members?limit=10"), slow)
            assert response.status_code == 200
            row = response.json()["members"][0]
            assert row["emby_user_id"] == "u1" and row["watch_hours"] == 1.0 and row["points"] == 2
    asyncio.run(check())


def test_periodic_measured_write_keeps_health_responsive_and_policy_order(monkeypatch):
    slow = SlowOperation({"ok": True, "credited": 5})
    sent = Mock()
    applied = Mock()
    monkeypatch.setattr(main, "_edge_node_or_401", lambda *a: object())
    monkeypatch.setattr(main, "_meter_policy_snapshot", lambda: {"rev": 3, "blocked_tags": []})
    monkeypatch.setattr(main.app.state, "metering", SimpleNamespace(ingest=slow, note_policy_sent=sent, note_policy_applied=applied), raising=False)

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            response = await assert_loop_free(client.post("/api/edge/node-a/measured", json={"policy_applied_rev": 2}), slow)
            assert response.status_code == 200 and response.json()["credited"] == 5
            assert response.json()["policy_rev"] == 3
            sent.assert_called_once_with("node-a", 3)
            applied.assert_called_once_with("node-a", 2)
    asyncio.run(check())


def test_usage_device_commit_does_not_stall_event_loop(tmp_path, monkeypatch):
    db = Database(tmp_path / "watch.db")
    slow = SlowOperation(True)
    sampler = UsageSampler(db, SimpleNamespace(register_device=slow), SimpleNamespace(active_sessions_raw=AsyncMock(return_value=[
        {"Id": "s", "UserId": "u1", "DeviceId": "d", "NowPlayingItem": {"Id": "item"}, "PlayState": {}}])))
    try:
        result = asyncio.run(assert_loop_free(sampler.tick(), slow))
        assert result["sessions"] == 1 and result["playing"] == 1
        assert db.one("SELECT COUNT(*) AS n FROM watch_checkpoints")["n"] == 1
    finally:
        db.close()


def test_usage_cancellation_cannot_overlap_an_unfinished_commit(tmp_path, monkeypatch):
    db = Database(tmp_path / "watch.db")
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    members.upsert("u1", "viewer", {"group_id": "standard"})
    clock = [1800000000.0]
    monkeypatch.setattr("app.modules.usage.time.time", lambda: clock[0])
    emby = SimpleNamespace(active_sessions_raw=AsyncMock(return_value=[
        {"Id": "s", "UserId": "u1", "NowPlayingItem": {"Id": "item", "Bitrate": 8000000}, "PlayState": {}}]))
    sampler = UsageSampler(db, members, emby)
    slow = SlowOperation(None)
    original = sampler._record_watch

    def commit(*args):
        slow()
        return original(*args)

    async def check():
        await sampler.tick()
        monkeypatch.setattr(sampler, "_record_watch", commit)
        clock[0] += 30
        first = asyncio.create_task(sampler.tick())
        assert await asyncio.to_thread(slow.entered.wait, 2)
        first.cancel()
        clock[0] += 30
        second = asyncio.create_task(sampler.tick())
        await asyncio.sleep(0.01)
        try:
            assert not second.done()
            # Readers use a committed immutable snapshot, not a live-mutating dict.
            assert sampler.live_watch()[0]["seconds"] == 0
        finally:
            slow.release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            await second
        assert StatsService(db).watch_summary("u1", clock[0])["recorded_seconds"] == 60
        assert sampler.live_watch()[0]["seconds"] == 60
    try:
        asyncio.run(check())
    finally:
        db.close()


def test_background_usage_io_keeps_context_kwargs_and_joins_on_cancel():
    slow = SlowOperation(None)
    marker = contextvars.ContextVar("usage_test", default="missing")
    observed = []

    def work(value, *, apply):
        slow()
        observed.append((marker.get(), value, apply))

    async def check():
        marker.set("caller")
        task = asyncio.create_task(run_usage_io(work, 42, apply=True))
        assert await asyncio.to_thread(slow.entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.01)
        try:
            assert not task.done(), "shutdown must still join the background writer"
        finally:
            slow.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert observed == [("caller", 42, True)]
    asyncio.run(check())


def test_admin_cache_still_checks_active_role_without_blocking_loop(monkeypatch):
    slow = SlowOperation([{"username": "operator", "state": "active", "emby_user_id": "u1"}])
    emby = SimpleNamespace(authenticate_user=AsyncMock())
    monkeypatch.setattr(main.app.state, "members", SimpleNamespace(list=slow), raising=False)
    monkeypatch.setattr(main.app.state, "emby", emby, raising=False)
    monkeypatch.setattr(main.app.state, "cache", SimpleNamespace(get=lambda key: main._digest("local-test")), raising=False)

    async def check():
        assert await assert_loop_free(main._role_admin_auth("operator", "local-test"), slow) == "operator"
        slow.result = []
        assert await main._role_admin_auth("operator", "local-test") is None
        emby.authenticate_user.assert_not_awaited()
    asyncio.run(check())


def test_concurrent_measured_reports_keep_ingest_policy_ack_serial(monkeypatch):
    slow = SlowOperation(None)
    steps = []

    def ingest(payload):
        if payload["node"] == "node-a":
            slow()
        steps.append((payload["node"], "ingest"))
        return {"ok": True}

    def policy():
        steps.append((steps[-1][0], "policy"))
        return {"rev": 3, "blocked_tags": []}

    monkeypatch.setattr(main, "_edge_node_or_401", lambda *a: object())
    monkeypatch.setattr(main, "_meter_policy_snapshot", policy)
    monkeypatch.setattr(main.app.state, "metering", SimpleNamespace(
        ingest=ingest,
        note_policy_sent=lambda node, rev: steps.append((node, "sent")),
        note_policy_applied=lambda node, rev: steps.append((node, "applied"))), raising=False)

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            first = asyncio.create_task(client.post("/api/edge/node-a/measured", json={"policy_applied_rev": 2}))
            assert await asyncio.to_thread(slow.entered.wait, 2)
            second = asyncio.create_task(client.post("/api/edge/node-b/measured", json={"policy_applied_rev": 2}))
            try:
                await asyncio.sleep(0.02)
                assert not second.done() and steps == []
                assert (await client.get("/healthz")).status_code == 200
            finally:
                slow.release.set()
                results = await asyncio.gather(first, second)
            assert all(r.status_code == 200 for r in results)
            assert steps == [(node, step) for node in ("node-a", "node-b")
                             for step in ("ingest", "policy", "sent", "applied")]
    asyncio.run(check())
