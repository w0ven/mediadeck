"""Per-user serialization must isolate latency, not weaken admission/lifecycle."""
import asyncio
import gc
from unittest.mock import AsyncMock

import pytest
from test_stream_admission import client as client  # noqa: PLC0414 - fixture re-export
from test_stream_admission import headers, row
from test_stream_admission import stack as stack  # noqa: PLC0414 - fixture re-export

from app.main import app


def issue(play):
    return AsyncMock(return_value=(200, {"PlaySessionId": play}))


@pytest.mark.parametrize("operation,stage", [
    ("inspect", "snapshot"), ("inspect", "policy"),
    ("info", "snapshot"), ("info", "policy"), ("info", "issue"),
    ("stop", "snapshot"), ("stop", "stop"),
    ("confirm", "snapshot"), ("confirm", "policy"),
    ("activity", "issue"),
])
def test_slow_user_never_blocks_another_users_admission(stack, monkeypatch, operation, stage):
    members, emby, guard = stack
    members.set_overrides("u1", {"max_streams": 1})
    members.upsert("u2", "second", {"group_id": "standard"})
    members.set_overrides("u2", {"max_streams": 1})
    emby.set_sessions([row("a", playing=True), row("b"), {**row("other"), "UserId": "u2"}])

    async def check():
        denied = await guard.inspect("u1", "b")
        assert denied.punishable
        entered, release = asyncio.Event(), asyncio.Event()
        slow_task = None
        snapshot, policy = emby.active_sessions_raw, emby.list_users

        async def pause(value):
            if asyncio.current_task() is slow_task:
                entered.set()
                await release.wait()
            return value

        async def read_snapshot():
            return await pause(await snapshot())

        async def read_policy():
            return await pause(await policy())

        if stage == "snapshot":
            monkeypatch.setattr(emby, "active_sessions_raw", read_snapshot)
        elif stage == "policy":
            monkeypatch.setattr(emby, "list_users", read_policy)
        elif stage == "stop":
            monkeypatch.setattr(emby, "report_stopped", lambda *_: pause(204))

        if operation == "inspect":
            call = guard.inspect("u1", "b")
        elif operation == "info":
            call = guard.issue_info("u1", "b" if stage == "policy" else "a", "",
                                    lambda: pause((200, {"PlaySessionId": "new"})))
        elif operation == "stop":
            call = guard.report_stopped("u1", "a", "tok:u1", {"PlaySessionId": "old"})
        elif operation == "confirm":
            call = guard.confirm_violation(denied)
        else:
            call = guard.report_activity("u1", {"PlaySessionId": "old"}, lambda: pause(204))
        slow_task = asyncio.create_task(call)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            other = await asyncio.wait_for(guard.inspect("u2", "other"), 1)
            assert other.allowed and other.user_id == "u2"
            assert not slow_task.done()  # no need to finish A's network I/O first
        finally:
            release.set()
            await slow_task

    asyncio.run(check())


@pytest.mark.parametrize("entrance", ["inspect", "admit", "info"])
def test_same_user_mixed_entrances_cannot_take_reserved_last_seat(stack, entrance):
    members, emby, guard = stack
    members.set_overrides("u1", {"max_streams": 1})

    async def check():
        entered, release, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        rejected_issue = issue("must-not-issue")

        async def slow_issue():
            entered.set()
            await release.wait()
            return 200, {"PlaySessionId": "current"}

        async def contender():
            attempted.set()
            if entrance == "inspect":
                return await guard.inspect(" u1 ", "b")  # same canonical user
            if entrance == "admit":
                return await guard.admit(user_id="u1", device_id="b", sessions=emby._sessions)
            return (await guard.issue_info("u1", "b", "", rejected_issue))[0]

        owner = asyncio.create_task(guard.issue_info("u1", "a", "", slow_issue))
        await asyncio.wait_for(entered.wait(), 1)
        waiter = asyncio.create_task(contender())
        try:
            await asyncio.wait_for(attempted.wait(), 1)
            assert not waiter.done()
        finally:
            release.set()
            await owner
        denied = await waiter
        assert not denied.allowed and denied.reason == "over-limit" and not denied.punishable
        rejected_issue.assert_not_awaited()
        assert guard._db.one("SELECT play_id FROM stream_leases")["play_id"] == "current"
        assert len(guard._db.query("SELECT * FROM stream_leases")) == 1

    asyncio.run(check())


def test_replacement_binding_is_atomic_with_old_stop_and_activity(stack, monkeypatch):
    members, emby, guard = stack
    members.set_overrides("u1", {"max_streams": 1})
    stopped = AsyncMock(return_value=204)
    monkeypatch.setattr(emby, "report_stopped", stopped)

    async def check():
        await guard.issue_info("u1", "a", "", issue("old"))
        entered, release, activity_sent, stop_attempted = (asyncio.Event() for _ in range(4))

        async def replacement():
            entered.set()
            await release.wait()
            return 200, {"PlaySessionId": "new"}

        async def activity():
            activity_sent.set()
            return 204

        async def stop_old():
            stop_attempted.set()
            return await guard.report_stopped("u1", "a", "tok:u1", {"PlaySessionId": "old"})

        owner = asyncio.create_task(guard.issue_info("u1", "a", "", replacement))
        await asyncio.wait_for(entered.wait(), 1)
        stop = asyncio.create_task(stop_old())
        progress = asyncio.create_task(guard.report_activity("u1", {"PlaySessionId": "old"}, activity))
        try:
            await asyncio.wait_for(stop_attempted.wait(), 1)
            # Progress network I/O must remain outside even the user's lock.
            await asyncio.wait_for(activity_sent.wait(), 1)
            stopped.assert_not_awaited()
            assert not progress.done() and not stop.done()
        finally:
            release.set()
            await asyncio.gather(owner, stop, progress)
        lease = guard._db.one("SELECT * FROM stream_leases")
        assert lease["play_id"] == "new" and lease["observed"] == 0 and lease["snapshot_guard"] == 1
        denied = await guard.inspect("u1", "b")
        assert not denied.allowed and not denied.punishable
        await guard.report_stopped("u1", "a", "tok:u1", {"PlaySessionId": "new"})
        assert (await guard.inspect("u1", "b")).allowed

    asyncio.run(check())


def test_violation_confirmation_waits_for_same_user_new_binding(stack):
    members, emby, guard = stack
    members.set_overrides("u1", {"max_streams": 1})
    emby.set_sessions([row("a", playing=True), row("b")])

    async def check():
        denied = await guard.inspect("u1", "b")
        assert denied.punishable
        emby.set_sessions([row("a"), row("b")])
        entered, release, attempted = (asyncio.Event() for _ in range(3))

        async def slow_issue():
            entered.set()
            await release.wait()
            return 200, {"PlaySessionId": "legitimate"}

        async def confirm():
            attempted.set()
            return await guard.confirm_violation(denied)

        owner = asyncio.create_task(guard.issue_info("u1", "b", "", slow_issue))
        await asyncio.wait_for(entered.wait(), 1)
        proof = asyncio.create_task(confirm())
        try:
            await asyncio.wait_for(attempted.wait(), 1)
            assert not proof.done()
        finally:
            release.set()
            await owner
        assert await proof is None  # the rejected session legitimately acquired a seat
        assert guard._db.one("SELECT play_id FROM stream_leases")["play_id"] == "legitimate"

    asyncio.run(check())


def test_identical_play_ids_and_devices_remain_user_scoped(stack, monkeypatch):
    members, emby, guard = stack
    members.upsert("u2", "second", {"group_id": "standard"})
    emby.set_sessions([row("a"), {**row("other", "a"), "UserId": "u2"}])
    monkeypatch.setattr(emby, "report_stopped", AsyncMock(return_value=204))

    async def check():
        await asyncio.gather(guard.issue_info("u1", "a", "", issue("same-id")),
                             guard.issue_info("u2", "a", "", issue("same-id")))
        await guard.report_activity("u2", {"PlaySessionId": "same-id"}, AsyncMock(return_value=204))
        assert guard._db.one("SELECT observed FROM stream_leases WHERE user_id='u1'")["observed"] == 0
        assert guard._db.one("SELECT observed FROM stream_leases WHERE user_id='u2'")["observed"] == 1
        await guard.report_stopped("u2", "a", "tok:u2", {"PlaySessionId": "same-id"})
        leases = guard._db.query("SELECT * FROM stream_leases")
        assert len(leases) == 1 and leases[0]["user_id"] == "u1"
        denied = await guard.inspect("u2", play_id="same-id")
        assert not denied.allowed and denied.reason == "session-unresolved" and not denied.punishable

    asyncio.run(check())


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["status", "exception", "cancel"])
def test_failed_or_cancelled_issuance_only_releases_its_new_seat(stack, existing, failure):
    members, _, guard = stack
    members.set_overrides("u1", {"max_streams": 1})

    async def check():
        if existing:
            await guard.issue_info("u1", "a", "", issue("previous"))
        entered, release = asyncio.Event(), asyncio.Event()

        async def failing_issue():
            entered.set()
            await release.wait()
            if failure == "exception":
                raise RuntimeError("upstream unavailable")
            return 503, {}

        owner = asyncio.create_task(guard.issue_info("u1", "a", "", failing_issue))
        await asyncio.wait_for(entered.wait(), 1)
        next_start = asyncio.create_task(guard.inspect("u1", "b"))
        if failure == "cancel":
            owner.cancel()
        else:
            release.set()
        if failure == "status":
            assert (await owner)[1] == 503
        else:
            with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
                await owner
        result = await asyncio.wait_for(next_start, 1)
        assert result.allowed is not existing and not result.punishable
        leases = guard._db.query("SELECT * FROM stream_leases")
        assert len(leases) == 1
        assert leases[0]["session_id"] == ("a" if existing else "b")
        assert leases[0]["play_id"] == ("previous" if existing else "")

    asyncio.run(check())


def test_cancelled_waiters_do_not_split_lock_and_idle_locks_are_reclaimed(stack):
    members, _, guard = stack
    members.set_overrides("u1", {"max_streams": 1})

    async def check():
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_issue():
            entered.set()
            await release.wait()
            return 200, {"PlaySessionId": "current"}

        owner = asyncio.create_task(guard.issue_info("u1", "a", "", slow_issue))
        await asyncio.wait_for(entered.wait(), 1)
        waiters = [asyncio.create_task(guard.inspect("u1", "b")) for _ in range(20)]
        await asyncio.sleep(0)  # enqueue all waiters behind the held user lock
        for task in waiters[:10]:
            task.cancel()
        await asyncio.gather(*waiters[:10], return_exceptions=True)
        last = asyncio.create_task(guard.inspect("u1", "c"))
        try:
            await asyncio.sleep(0)
            gc.collect()
            assert len(guard._user_locks) == 1
            assert all(not task.done() for task in [*waiters[10:], last])
        finally:
            release.set()
            await owner
        results = await asyncio.gather(*waiters[10:], last)
        assert all(not result.allowed and not result.punishable for result in results)

    asyncio.run(check())
    gc.collect()
    assert not guard._user_locks


@pytest.mark.parametrize("operation", ["inspect", "info", "stop"])
def test_snapshot_failure_never_issues_or_releases_existing_seat(stack, monkeypatch, operation):
    _, emby, guard = stack
    asyncio.run(guard.issue_info("u1", "a", "", issue("current")))
    monkeypatch.setattr(emby, "active_sessions_raw", AsyncMock(side_effect=RuntimeError("offline")))
    stopped = AsyncMock(return_value=204)
    monkeypatch.setattr(emby, "report_stopped", stopped)
    issuer = issue("must-not-issue")

    async def check():
        if operation == "inspect":
            result = await guard.inspect("u1", "b")
            assert not result.allowed and not result.punishable
            assert result.reason == "sessions-unavailable"
        else:
            with pytest.raises(RuntimeError):
                if operation == "info":
                    await guard.issue_info("u1", "b", "", issuer)
                else:
                    await guard.report_stopped("u1", "a", "tok:u1", {"PlaySessionId": "current"})
        issuer.assert_not_awaited()
        stopped.assert_not_awaited()
        assert guard._db.one("SELECT play_id FROM stream_leases")["play_id"] == "current"

    asyncio.run(check())


def test_cancelled_stop_keeps_current_seat_and_releases_user_lock(stack, monkeypatch):
    members, emby, guard = stack
    members.set_overrides("u1", {"max_streams": 1})

    async def check():
        await guard.issue_info("u1", "a", "", issue("current"))
        entered = asyncio.Event()

        async def slow_stop(*_):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(emby, "report_stopped", slow_stop)
        owner = asyncio.create_task(guard.report_stopped("u1", "a", "tok:u1", {"PlaySessionId": "current"}))
        await asyncio.wait_for(entered.wait(), 1)
        waiter = asyncio.create_task(guard.inspect("u1", "b"))
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        result = await asyncio.wait_for(waiter, 1)
        assert not result.allowed and not result.punishable
        assert guard._db.one("SELECT play_id FROM stream_leases")["play_id"] == "current"

    asyncio.run(check())


@pytest.mark.parametrize("change", ["token", "item", "device", "sessions"])
def test_prior_admission_and_forged_headers_cannot_skip_fresh_checks(client, monkeypatch, change):
    path = "/Videos/item42/stream.mkv?Static=true"
    caller = {**headers("a"), "X-Original-URI": path,
              "X-Mediadeck-Admitted": "1", "X-Mediadeck-User-Id": "u1"}
    assert client.get("/api/playback/admit", headers=caller).status_code == 204
    if change == "token":
        monkeypatch.setattr(app.state.emby, "user_for_token", AsyncMock(return_value=None))
    elif change == "item":
        monkeypatch.setattr(app.state.emby, "verify_item_access", AsyncMock(return_value=False))
    elif change == "device":
        monkeypatch.setattr(app.state.members, "device_blocked", lambda *_: True)
    else:
        monkeypatch.setattr(app.state.emby, "active_sessions_raw", AsyncMock(side_effect=RuntimeError("offline")))
    route = AsyncMock(side_effect=AssertionError("denied caller must not route"))
    monkeypatch.setattr(app.state.playback, "route", route)
    result = client.get(path, headers=caller, follow_redirects=False)
    assert result.status_code == (503 if change == "sessions" else 403)
    assert "location" not in result.headers
    route.assert_not_awaited()
