"""Admission identity, replacement epochs and safe post-denial proof."""
# ruff: noqa: F811 - pytest injects fixtures using the imported names
import asyncio
from unittest.mock import AsyncMock

import pytest
from test_stream_admission import (
    client,  # noqa: F401 - pytest fixture
    headers,
    row,
    stack,  # noqa: F401 - pytest fixture
)

from app.main import app
from app.modules.streams import Admission, StreamAdmission


def issue(play):
    return AsyncMock(return_value=(200, {"PlaySessionId": play}))


@pytest.mark.parametrize("snapshot_play", [None, "old"])
@pytest.mark.parametrize("restart", [False, True])
def test_old_snapshot_cannot_observe_replacement_or_release_its_pending_seat(stack, monkeypatch, snapshot_play, restart):
    members, emby, guard = stack
    members.set_overrides("u1", {"max_streams": 1})
    clock = [1000.0]
    monkeypatch.setattr("app.modules.streams.time.time", lambda: clock[0])

    async def check():
        await guard.issue_info("u1", "a", "", issue("old"))
        old_row = row("a", playing=True)
        if snapshot_play:
            old_row["PlaySessionId"] = snapshot_play
        emby.set_sessions([old_row, row("b")])
        await guard.issue_info("u1", "a", "", issue("new"))
        current = StreamAdmission(members, emby) if restart else guard
        assert (await current.inspect("u1", "b")).reason == "over-limit"
        lease = current._db.one("SELECT * FROM stream_leases")
        assert lease["play_id"] == "new" and lease["observed"] == 0
        assert lease["snapshot_guard"] == 1
        assert await current.report_stopped("u1", "a", "tok:u1", {"PlaySessionId": "old"}) == 204
        clock[0] = 1119
        denied = await current.inspect("u1", "b")
        assert denied.reason == "over-limit" and not denied.punishable
        assert denied.live_count == 0 and denied.pending_count == 1
        clock[0] = 1120
        assert (await current.inspect("u1", "b")).allowed

    asyncio.run(check())


@pytest.mark.parametrize("observation", ["activity", "snapshot"])
def test_current_replacement_observation_allows_idle_recovery(stack, observation):
    members, emby, guard = stack
    members.set_overrides("u1", {"max_streams": 1})

    async def check():
        await guard.issue_info("u1", "a", "", issue("old"))
        emby.set_sessions([row("a", playing=True), row("b")])
        await guard.issue_info("u1", "a", "", issue("new"))
        if observation == "activity":
            await guard.report_activity("u1", {"PlaySessionId": "new"}, AsyncMock(return_value=204))
        else:
            emby.set_sessions([{**row("a", playing=True), "PlayState": {"PlaySessionId": "new"}}, row("b")])
            assert not (await guard.inspect("u1", "b")).allowed
        assert guard._db.one("SELECT * FROM stream_leases")["observed"] == 1
        emby.set_sessions([row("a"), row("b")])
        assert (await guard.inspect("u1", "b")).allowed

    asyncio.run(check())


def test_initial_bound_play_still_observes_plain_snapshot_and_recovers_without_stop(stack):
    members, emby, guard = stack
    members.set_overrides("u1", {"max_streams": 1})

    async def check():
        await guard.issue_info("u1", "a", "", issue("first"))
        emby.set_sessions([row("a", playing=True), row("b")])
        assert not (await guard.inspect("u1", "b")).allowed
        assert guard._db.one("SELECT * FROM stream_leases")["observed"] == 1
        emby.set_sessions([row("a"), row("b")])
        assert (await guard.inspect("u1", "b")).allowed

    asyncio.run(check())


def test_stopped_resolves_unique_bound_play_on_duplicate_device(client):
    app.state.members.set_overrides("u1", {"max_streams": 1})
    app.state.emby.set_sessions([{**row("a", "shared"), "Client": "A"},
                                 {**row("b", "shared"), "Client": "B"}, row("c")])
    hdr = {**headers("shared"), "X-Emby-Client": "A"}
    info = client.post("/api/playback/info/item42", headers=hdr, json={})
    assert info.status_code == 200
    play = info.json()["PlaySessionId"]
    assert client.post("/api/playback/stopped", headers=hdr, json={"PlaySessionId": play}).status_code == 204
    assert app.state.db.query("SELECT * FROM stream_leases") == []
    assert client.post("/api/playback/info/item42", headers=headers("c"), json={}).status_code == 200


@pytest.mark.parametrize("payload,device,status", [
    ({"PlaySessionId": "current", "SessionId": "b"}, "shared", 204),
    ({"PlaySessionId": "old"}, "shared", 204),
    ({"PlaySessionId": "current"}, "other-device", 204),
    ({"PlaySessionId": "current"}, "shared", 503),
])
def test_duplicate_device_stop_never_releases_on_mismatch_or_failed_delivery(stack, payload, device, status):
    members, emby, guard = stack
    members.set_overrides("u1", {"max_streams": 1})
    emby.set_sessions([{**row("a", "shared"), "Client": "A"}, {**row("b", "shared"), "Client": "B"}])
    emby.report_stopped = AsyncMock(return_value=status)

    async def check():
        await guard.issue_info("u1", "shared", "", issue("current"), session_profile={"Client": "A"})
        assert await guard.report_stopped("u1", device, "tok:u1", payload) == status
        assert guard._db.one("SELECT * FROM stream_leases")["play_id"] == "current"

    asyncio.run(check())


def test_token_and_asserted_session_without_device_cannot_alias_occupied_seat(client):
    app.state.members.set_overrides("u1", {"max_streams": 1})
    info = client.post("/api/playback/info/item42", headers=headers("a"), json={})
    assert info.status_code == 200
    r = client.get("/Videos/item42/stream.mkv?SessionId=a&Static=true",
                   headers={"X-Emby-Token": "tok:u1"}, follow_redirects=False)
    assert r.status_code == 503
    assert r.headers["X-Mediadeck-Decision"] == "session-unresolved" and "location" not in r.headers
    # A genuine server-issued current play binding still supports device-less HLS.
    hls = client.get("/api/playback/admit", headers={"X-Emby-Token": "tok:u1",
                     "X-Original-URI": "/Videos/item42/master.m3u8?PlaySessionId=" + info.json()["PlaySessionId"]})
    assert hls.status_code == 204


def test_bound_play_cannot_be_used_for_a_different_asserted_session(stack):
    _, _, guard = stack

    async def check():
        await guard.issue_info("u1", "a", "", issue("a-play"))
        r = await guard.inspect("u1", session_id="b", play_id="a-play")
        assert r.reason == "session-unresolved" and not r.punishable

    asyncio.run(check())


def test_admin_exempt_but_auth_snapshot_failures_remain_closed(stack):
    members, emby, guard = stack
    members.set_roles("u1", ["admin"])
    members.set_overrides("u1", {"max_streams": 1})
    for sid in "abc":
        r = asyncio.run(guard.inspect("u1", sid))
        assert r.allowed and r.reason == "admin-exempt" and not r.punishable
    emby.active_sessions_raw = AsyncMock(side_effect=RuntimeError("offline"))
    r = asyncio.run(guard.inspect("u1", "a"))
    assert not r.allowed and not r.punishable


def test_whitelist_keeps_its_own_effective_cap(stack):
    members, _, guard = stack
    members.upsert("u1", "demo", {"group_id": "whitelist"})
    members.set_overrides("u1", {"max_streams": 1})
    assert asyncio.run(guard.inspect("u1", "a")).allowed
    r = asyncio.run(guard.inspect("u1", "b"))
    assert r.reason == "over-limit" and r.cap == 1 and not r.punishable


@pytest.mark.parametrize("paused", [False, True])
def test_newcomer_against_real_baseline_is_actionable_and_confirm_is_non_mutating(stack, paused):
    members, emby, guard = stack
    members.set_overrides("u1", {"max_streams": 1})
    emby.set_sessions([row("a", playing=True, paused=paused), row("b")])

    async def check():
        r = await guard.inspect("u1", "b")
        assert r.reason == "over-limit" and r.punishable
        assert (r.user_id, r.session_id, r.device_id, r.cap) == ("u1", "b", "b", 1)
        assert r.playing_count == int(not paused) and r.paused_count == int(paused)
        assert r.live_count == 1 and r.pending_count == 0
        before = [dict(s) for s in guard._db.query("SELECT * FROM stream_leases")]
        proof = await guard.confirm_violation(r)
        assert proof["user_id"] == "u1" and proof["session_id"] == "b"
        assert proof["other_live_count"] == 1 and proof["punishable"]
        assert before == [dict(s) for s in guard._db.query("SELECT * FROM stream_leases")]
        assert (await guard.inspect("u1", "a")).allowed  # existing legal play is untouched

    asyncio.run(check())


def test_pending_only_and_mixed_pending_limits_never_authorize_punishment(stack):
    _, emby, guard = stack

    async def check():
        await guard.inspect("u1", "a")
        await guard.inspect("u1", "b")
        r = await guard.inspect("u1", "c")
        assert (r.live_count, r.pending_count, r.punishable) == (0, 2, False)
        assert await guard.confirm_violation(r) is None
        emby.set_sessions([row("a", playing=True), row("b"), row("c")])
        r = await guard.inspect("u1", "c")
        assert (r.live_count, r.pending_count, r.punishable) == (1, 1, False)
        assert await guard.confirm_violation(r) is None

    asyncio.run(check())


def test_cold_over_cap_or_already_playing_unowned_session_is_not_punishable(stack):
    _, emby, guard = stack
    emby.set_sessions([row(s, playing=True) for s in "abc"] + [row("d")])
    for sid in "abcd":
        r = asyncio.run(guard.inspect("u1", sid))
        assert r.reason == "over-limit" and not r.punishable
        assert asyncio.run(guard.confirm_violation(r)) is None


@pytest.mark.parametrize("change", ["idle", "gone", "replacement-user", "different-occupant"])
def test_exit_or_baseline_change_cancels_proof_and_does_not_punish_stale_lease(stack, change):
    members, emby, guard = stack
    members.set_overrides("u1", {"max_streams": 1})
    emby.set_sessions([row("a", playing=True), row("b"), row("c")])

    async def check():
        r = await guard.inspect("u1", "b")
        assert r.punishable
        if change == "idle":
            emby.set_sessions([row("a"), row("b"), row("c")])
        elif change == "gone":
            emby.set_sessions([row("b"), row("c")])
        elif change == "replacement-user":
            emby.set_sessions([{**row("a", playing=True), "UserId": "someone-else"}, row("b")])
        else:
            emby.set_sessions([row("a"), row("b"), row("c", playing=True)])
        assert await guard.confirm_violation(r) is None
        if change != "different-occupant":
            assert (await guard.inspect("u1", "b")).allowed

    asyncio.run(check())


@pytest.mark.parametrize("change", ["cap", "unlimited", "admin", "device", "user", "missing-session", "offline"])
def test_changed_request_identity_policy_or_dependency_cancels_proof(stack, change):
    members, emby, guard = stack
    members.set_overrides("u1", {"max_streams": 1})
    emby.set_sessions([row("a", playing=True), row("b")])

    async def check():
        r = await guard.inspect("u1", "b")
        assert r.punishable
        if change == "cap":
            members.set_overrides("u1", {"max_streams": 2})
        elif change == "unlimited":
            members.set_overrides("u1", {"max_streams": 0})
        elif change == "admin":
            members.set_roles("u1", ["admin"])
        elif change == "device":
            emby.set_sessions([row("a", playing=True), row("b", "changed")])
        elif change == "user":
            emby.set_sessions([row("a", playing=True), {**row("b"), "UserId": "other"}])
        elif change == "missing-session":
            emby.set_sessions([row("a", playing=True)])
        else:
            emby.active_sessions_raw = AsyncMock(side_effect=RuntimeError("offline"))
        assert await guard.confirm_violation(r) is None

    asyncio.run(check())


def test_legitimately_admitted_newcomer_after_rejection_cannot_be_punished(stack):
    members, emby, guard = stack
    members.set_overrides("u1", {"max_streams": 1})
    emby.set_sessions([row("a", playing=True), row("b")])

    async def check():
        r = await guard.inspect("u1", "b")
        emby.set_sessions([row("a"), row("b")])
        assert (await guard.inspect("u1", "b")).allowed
        emby.set_sessions([row("a", playing=True), row("b")])
        assert await guard.confirm_violation(r) is None

    asyncio.run(check())


@pytest.mark.parametrize("bad", [None, {}, [row("a"), row("a")],
                                   [{**row("a", playing=True), "NowPlayingItem": "invalid"}],
                                   [{**row("a", playing=True), "PlayState": {"IsPaused": "false"}}]])
def test_malformed_snapshot_cannot_produce_actionable_proof(stack, bad):
    members, emby, guard = stack
    members.set_overrides("u1", {"max_streams": 1})
    emby.set_sessions([row("a", playing=True), row("b")])

    async def check():
        r = await guard.inspect("u1", "b")
        assert r.punishable
        emby.active_sessions_raw = AsyncMock(return_value=bad)
        assert await guard.confirm_violation(r) is None
        denied = await guard.inspect("u1", "b")
        assert denied.reason == "sessions-unavailable" and not denied.punishable

    asyncio.run(check())


def test_reason_string_alone_never_authorizes_punishment(stack):
    _, _, guard = stack
    assert not Admission(False, "over-limit").punishable
    assert asyncio.run(guard.confirm_violation(Admission(False, "over-limit"))) is None
