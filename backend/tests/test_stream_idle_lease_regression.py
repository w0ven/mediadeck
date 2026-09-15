"""Idle observed leases must not lock accounts after PlaybackInfo binds an id."""
import asyncio
from unittest.mock import AsyncMock

from test_stream_admission import stack, row
from app.modules.streams import StreamAdmission


def bind(guard, sid, play_id):
    guard._db.execute("UPDATE stream_leases SET play_id=? WHERE session_id=?", (play_id, sid))


def test_observed_bound_idle_releases_without_password_change(stack):
    _, emby, guard = stack
    async def check():
        for sid in "ab":
            assert (await guard.inspect("u1", sid)).allowed
            bind(guard, sid, "play-" + sid)
        emby.set_sessions([row("a", playing=True), row("b", playing=True), row("c")])
        assert not (await guard.inspect("u1", "c")).allowed
        emby.set_sessions([row("a"), row("b", playing=True), row("c")])
        assert (await guard.inspect("u1", "c")).allowed
        assert guard._db.one("SELECT * FROM stream_leases WHERE session_id='a'") is None
    asyncio.run(check())


def test_persisted_observed_bound_idle_releases_after_guard_restart(stack):
    members, emby, guard = stack
    members.set_overrides("u1", {"max_streams": 1})
    guard._db.execute("INSERT INTO stream_leases(user_id,session_id,observed,play_id) VALUES ('u1','a',1,'old-play')")
    restarted = StreamAdmission(members, emby)
    assert asyncio.run(restarted.inspect("u1", "c")).allowed


def test_bound_paused_play_and_pending_start_keep_their_seats(stack):
    _, emby, guard = stack
    async def check():
        for sid in "ab":
            assert (await guard.inspect("u1", sid)).allowed
            bind(guard, sid, "play-" + sid)
        emby.set_sessions([row("a", playing=True, paused=True), row("b"), row("c")])
        assert (await guard.inspect("u1", "c")).reason == "over-limit"
        assert (await guard.inspect("u1", "a")).allowed
        assert (await guard.inspect("u1", "b")).allowed
    asyncio.run(check())


def test_new_play_id_does_not_inherit_previous_play_observed_flag(stack):
    members, emby, guard = stack
    members.set_overrides("u1", {"max_streams": 1})
    guard._db.execute("INSERT INTO stream_leases(user_id,session_id,observed,play_id) VALUES ('u1','a',1,'old-play')")
    emby.set_sessions([row("a", playing=True), row("b"), row("c")])
    async def check():
        issue = AsyncMock(return_value=(200, {"PlaySessionId": "new-play"}))
        result, code, _ = await guard.issue_info("u1", "a", "", issue)
        assert result.allowed and code == 200
        lease = guard._db.one("SELECT * FROM stream_leases WHERE session_id='a'")
        assert lease["observed"] == 0
        emby.set_sessions([row("a"), row("b"), row("c")])
        assert (await guard.inspect("u1", "c")).reason == "over-limit"
        assert await guard.report_stopped("u1", "a", "tok:u1", {"PlaySessionId": "old-play"}) == 204
        assert (await guard.inspect("u1", "c")).reason == "over-limit"
        assert await guard.report_stopped("u1", "a", "tok:u1", {"PlaySessionId": "new-play"}) == 204
        assert (await guard.inspect("u1", "c")).allowed
    asyncio.run(check())
