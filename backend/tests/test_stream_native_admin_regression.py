"""Native Emby admin protection and exact target playing evidence."""
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


def real_limit(stack):
    members, emby, guard = stack
    members.set_overrides("u1", {"max_streams": 1})
    emby.set_sessions([row("a", playing=True), row("b")])
    return members, emby, guard


@pytest.mark.parametrize("entrance", ["inspect", "info"])
def test_native_admin_exempts_only_over_limit_without_deck_role(stack, entrance):
    members, emby, guard = real_limit(stack)
    assert "admin" not in members.get("u1")["roles"]
    emby._users["u1"]["Policy"]["IsAdministrator"] = True
    emby.list_users = AsyncMock(wraps=emby.list_users)
    issue = AsyncMock(return_value=(200, {"PlaySessionId": "native-admin-play"}))

    async def check():
        if entrance == "inspect":
            result = await guard.inspect("u1", "b")
        else:
            result, code, data = await guard.issue_info("u1", "b", "", issue)
            assert code == 200 and data["PlaySessionId"] == "native-admin-play"
            issue.assert_awaited_once()
        assert result.allowed and result.reason == "admin-exempt" and not result.punishable
        assert result.session_id == "b" and result.live_count == 1
        emby.list_users.assert_awaited_once()

    asyncio.run(check())


@pytest.mark.parametrize("entrance", ["inspect", "info"])
@pytest.mark.parametrize("response", [RuntimeError("offline"), [], {},
                                       [{"Id": "u1"}],
                                       [{"Id": "u1", "Policy": {"IsAdministrator": "true"}}],
                                       [{"Id": "u1", "Policy": {"IsAdministrator": False}},
                                        {"Id": "u1", "Policy": {"IsAdministrator": True}}]])
def test_unknown_native_admin_policy_preserves_denial_but_never_punishes(stack, entrance, response):
    _, emby, guard = real_limit(stack)
    emby.list_users = AsyncMock(side_effect=response) if isinstance(response, Exception) else AsyncMock(return_value=response)
    issue = AsyncMock(return_value=(200, {"PlaySessionId": "must-not-issue"}))

    async def check():
        if entrance == "inspect":
            result = await guard.inspect("u1", "b")
        else:
            result, code, data = await guard.issue_info("u1", "b", "", issue)
            assert code == 403 and data == {}
        assert not result.allowed and result.reason == "over-limit" and not result.punishable
        issue.assert_not_awaited()
        emby.list_users.assert_awaited_once()

    asyncio.run(check())


@pytest.mark.parametrize("case", ["no-member", "grant", "same-play", "unlimited", "deck-admin", "unresolved", "device-block"])
def test_user_list_not_read_for_non_concurrency_denials_or_already_allowed_paths(stack, monkeypatch, case):
    members, emby, guard = real_limit(stack)
    emby.list_users = AsyncMock(side_effect=AssertionError("unnecessary user-list read"))
    uid, device = "u1", "b"
    if case == "no-member":
        uid = "u2"
    elif case == "grant":
        emby.set_sessions([row("a"), row("b")])
    elif case == "same-play":
        device = "a"
    elif case == "unlimited":
        members.set_overrides("u1", {"max_streams": 0})
    elif case == "deck-admin":
        members.set_roles("u1", ["admin"])
    elif case == "unresolved":
        device = "missing-device"
    else:
        emby._users["u1"]["Policy"]["IsAdministrator"] = True
        monkeypatch.setattr(members, "device_blocked", lambda user, dev: user == "u1" and dev == "b")
    result = asyncio.run(guard.inspect(uid, device))
    if case == "device-block":
        assert not result.allowed and result.reason == "device-blocked"
    elif case == "unresolved":
        assert not result.allowed and result.reason == "session-unresolved"
    else:
        assert result.allowed
    assert not result.punishable
    emby.list_users.assert_not_awaited()


@pytest.mark.parametrize("entrance", ["inspect", "info"])
def test_native_admin_does_not_bypass_device_block(stack, monkeypatch, entrance):
    members, emby, guard = real_limit(stack)
    emby._users["u1"]["Policy"]["IsAdministrator"] = True
    monkeypatch.setattr(members, "device_blocked", lambda user, dev: dev == "b")
    issue = AsyncMock(return_value=(200, {}))

    async def check():
        if entrance == "inspect":
            result = await guard.inspect("u1", "b")
        else:
            result, code, _ = await guard.issue_info("u1", "b", "", issue)
            assert code == 403
        assert not result.allowed and result.reason == "device-blocked" and not result.punishable
        issue.assert_not_awaited()

    asyncio.run(check())


def test_native_admin_promotion_is_freshly_rechecked_and_cancels_proof(stack):
    _, emby, guard = real_limit(stack)
    emby.list_users = AsyncMock(wraps=emby.list_users)

    async def check():
        result = await guard.inspect("u1", "b")
        assert result.punishable
        emby._users["u1"]["Policy"]["IsAdministrator"] = True
        assert await guard.confirm_violation(result) is None
        assert emby.list_users.await_count == 2

    asyncio.run(check())


def test_native_admin_policy_read_failure_cancels_existing_proof(stack):
    _, emby, guard = real_limit(stack)

    async def check():
        result = await guard.inspect("u1", "b")
        assert result.punishable
        emby.list_users = AsyncMock(side_effect=RuntimeError("policy unavailable"))
        assert await guard.confirm_violation(result) is None

    asyncio.run(check())


@pytest.mark.parametrize("target", ["idle", "playing", "paused"])
def test_proof_playing_describes_target_not_total_playing_count(stack, target):
    _, emby, guard = real_limit(stack)

    async def check():
        result = await guard.inspect("u1", "b")
        assert result.punishable
        emby.set_sessions([row("a", playing=True),
                           row("b", playing=target != "idle", paused=target == "paused")])
        proof = await guard.confirm_violation(result)
        assert proof is not None and proof["session_id"] == "b"
        assert proof["playing"] is (target != "idle")
        assert proof["playing_count"] >= 1  # another legal session is playing
        assert proof["other_live_count"] == 1

    asyncio.run(check())


@pytest.mark.parametrize("path", ["/api/playback/info/item42", "/api/playback/admit"])
def test_http_native_admin_over_limit_is_allowed_without_main_changes(client, path):
    app.state.members.set_overrides("u1", {"max_streams": 1})
    app.state.emby.set_sessions([row("a", playing=True), row("b")])
    app.state.emby._users["u1"]["Policy"]["IsAdministrator"] = True
    if path.endswith("admit"):
        response = client.get(path, headers={**headers("b"), "X-Original-URI": "/Videos/item42/master.m3u8"})
        assert response.status_code == 204
    else:
        response = client.post(path, headers=headers("b"), json={})
        assert response.status_code == 200 and response.json()["PlaySessionId"]

@pytest.mark.parametrize("change", ["exit", "cap"])
def test_exit_or_cap_change_during_native_policy_read_cancels_proof(stack, change):
    members, emby, guard = real_limit(stack)

    async def check():
        result = await guard.inspect("u1", "b")
        assert result.punishable
        async def policy_read():
            if change == "exit":
                emby.set_sessions([row("a"), row("b")])
            else:
                members.set_overrides("u1", {"max_streams": 2})
            return [{"Id": "u1", "Policy": {"IsAdministrator": False}}]
        emby.list_users = AsyncMock(side_effect=policy_read)
        assert await guard.confirm_violation(result) is None

    asyncio.run(check())
