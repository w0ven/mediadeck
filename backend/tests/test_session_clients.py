"""Session client migration preserves authenticated identity and stop-only actions."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from test_restrictions import service as service  # noqa: PLC0414 - fixture re-export

from app.main import app
from app.modules.restrictions import RestrictionService
from app.modules.session_clients import matches, validate_config

CONFIG = {"enabled": True, "blocked_patterns": ["blocked-player"], "exempt_groups": ["whitelist"]}


def session(**patch):
    return {"Id": "s", "UserId": "u", "DeviceId": "d", "Client": "blocked-player", **patch}


def enable(service, rows=None):
    service.save_client_config(CONFIG)
    service.emby.active_sessions_raw.return_value = rows if rows is not None else [session()]


@pytest.mark.parametrize("field", ["Client", "DeviceName", "ApplicationVersion"])
def test_same_session_fields_case_insensitive(service, field):
    enable(service, [session(Client="native", **({field: "BLOCKED-PLAYER"} if field != "Client" else {}))]
           if field != "Client" else [session(Client="BLOCKED-PLAYER")])
    assert asyncio.run(service.inspect_client("u", "d", "s"))
    service.emby.stop_session.assert_awaited_once_with("s")
    service.emby.apply_member_policy.assert_not_awaited()
    service.emby.delete_user.assert_not_awaited()
    assert service.members.get("u")["status"] == "active"


@pytest.mark.parametrize("exemption", ["group", "emby_admin", "deck_admin"])
def test_original_exemptions(service, exemption):
    enable(service)
    if exemption == "group":
        service.members.upsert("u", "name", {"group_id": "whitelist"})
    elif exemption == "emby_admin":
        service.emby.list_users.return_value[0]["Policy"]["IsAdministrator"] = True
    else:
        service.members.set_roles("u", ["admin"])
    assert not asyncio.run(service.inspect_client("u", "d", "s"))
    assert service.events() == []
    service.emby.stop_session.assert_not_awaited()


@pytest.mark.parametrize("rows", [[], [session(), session()], [session(UserId="other")],
                                  [session(), session(UserId="other")]])
def test_missing_ambiguous_or_reused_session_id_never_punished(service, rows):
    enable(service, rows)
    with pytest.raises(RuntimeError):
        asyncio.run(service.inspect_client("u", "d", "s"))
    assert service.events() == []
    service.emby.stop_session.assert_not_awaited()


def test_unknown_account_policy_never_punished(service):
    enable(service)
    service.emby.list_users.return_value[0]["Policy"].pop("IsAdministrator")
    with pytest.raises(RuntimeError):
        asyncio.run(service.inspect_client("u", "d", "s"))
    service.emby.stop_session.assert_not_awaited()


def test_request_profile_is_not_match_evidence(service):
    enable(service, [session(Client="legitimate")])
    assert not asyncio.run(service.inspect_client("u", "d", "s", {"Client": "blocked-player"}))
    service.emby.stop_session.assert_not_awaited()


def test_current_web_rule_takes_precedence(service):
    enable(service, [session(Client="Emby Web", DeviceName="blocked-player")])
    service.save({"web_play": {"enabled": True, "action": "disable"}})
    assert not asyncio.run(service.inspect_client("u", "d", "s"))
    assert service.config()["web_play"] == {"enabled": True, "action": "disable"}
    assert service.events() == []


def test_receipt_dedup_survives_restart_and_elapsed_cooldown(service, monkeypatch):
    enable(service)
    async def run():
        assert await service.inspect_client("u", "d", "s")
        restarted = RestrictionService(service.db, service.store, service.members, service.emby,
                                       service.telegram, service.telegram_config)
        monkeypatch.setattr("app.modules.restrictions.time.time", lambda: 9_999_999_999)
        assert await restarted.inspect_client("u", "d", "s")
        await restarted.deliver()
        await restarted.deliver()
    asyncio.run(run())
    service.emby.stop_session.assert_awaited_once_with("s")
    assert len(service.events()) == 1
    assert service.telegram.send_message.await_count == 2


def test_stop_unknown_never_blind_replay(service, monkeypatch):
    enable(service, [session(NowPlayingItem={"Id": "item"})])
    clock = [1_800_000_000]
    monkeypatch.setattr("app.modules.restrictions.time.time", lambda: clock[0])
    service.emby.stop_session.side_effect = RuntimeError("synthetic-secret-not-to-log")
    assert asyncio.run(service.inspect_client("u", "d", "s"))
    assert service.events()[0]["status"] == "unknown"
    assert "synthetic-secret" not in str(service.events())
    clock[0] += 600
    assert asyncio.run(service.inspect_client("u", "d", "s"))
    service.emby.stop_session.assert_awaited_once()


def test_failed_stop_recovers_with_durable_backoff_no_notice_replay(service, monkeypatch):
    enable(service, [session(NowPlayingItem={"Id": "item"})])
    clock = [1_800_000_000]
    monkeypatch.setattr("app.modules.restrictions.time.time", lambda: clock[0])
    service.emby.stop_session.side_effect = [False, False, True]
    async def run():
        assert await service.inspect_client("u", "d", "s")
        first = service.events()[0]
        assert first["status"] == "failed"
        await service.deliver()
        restarted = RestrictionService(service.db, service.store, service.members, service.emby,
                                       service.telegram, service.telegram_config)
        for elapsed, calls in ((5, 1), (24, 1), (1, 2), (5, 2), (54, 2), (1, 3)):
            clock[0] += elapsed
            assert await restarted.inspect_client("u", "d", "s")
            assert service.emby.stop_session.await_count == calls
            assert len(service.events()) == 1
            assert service.events()[0]["id"] == first["id"]
            await restarted.deliver()
        assert service.events()[0]["status"] == "done"
        clock[0] += 600
        assert await restarted.inspect_client("u", "d", "s")
        assert service.emby.stop_session.await_count == 3
        assert service.telegram.send_message.await_count == 2
    asyncio.run(run())


@pytest.mark.parametrize("change", ["gone", "idle", "allowed", "whitelist", "admin", "ambiguous"])
def test_failed_stop_retry_requires_current_play_and_identity(service, monkeypatch, change):
    active = session(NowPlayingItem={"Id": "item"})
    enable(service, [active])
    clock = [1_800_000_000]
    monkeypatch.setattr("app.modules.restrictions.time.time", lambda: clock[0])
    service.emby.stop_session.return_value = False
    assert asyncio.run(service.inspect_client("u", "d", "s"))
    if change == "gone":
        service.emby.active_sessions_raw.return_value = []
    elif change == "idle":
        service.emby.active_sessions_raw.return_value = [session()]
    elif change == "allowed":
        service.emby.active_sessions_raw.return_value = [session(Client="allowed", NowPlayingItem={"Id": "item"})]
    elif change == "whitelist":
        service.members.upsert("u", "name", {"group_id": "whitelist"})
    elif change == "admin":
        service.emby.list_users.return_value[0]["Policy"]["IsAdministrator"] = True
    else:
        service.emby.active_sessions_raw.return_value = [active, active]
    clock[0] += 31
    asyncio.run(service.sample_clients([active]))
    service.emby.stop_session.assert_awaited_once()
    assert len(service.events()) == 1


def test_sampler_rechecks_fresh_identity_not_old_snapshot(service):
    enable(service, [session(Client="legitimate")])
    result = asyncio.run(service.sample_clients([session()]))
    assert result == {"ok": True, "checked": 1, "errors": 0}
    service.emby.stop_session.assert_not_awaited()


def test_sampler_identity_failure_remains_visible(service):
    enable(service, [])
    assert asyncio.run(service.sample_clients([session()])) == {"ok": False, "checked": 0, "errors": 1}
    service.emby.stop_session.assert_not_awaited()


def test_default_off_and_invalid_config():
    for patch in ({"action": "disable"}, {"enabled": 1}, {"blocked_patterns": ["["]},
                  {"exempt_groups": ["not/a/group"]}):
        with pytest.raises(ValueError):
            validate_config({**CONFIG, **patch})
    assert not matches({}, CONFIG)


@pytest.mark.parametrize("status,web,expected", [(401, False, 401), (200, False, 403), (200, True, 403)])
def test_login_failure_never_punished_and_web_disable_retained(service, monkeypatch, status, web, expected):
    with TestClient(app) as client:
        enable(service, [session(Client="Emby Web" if web else "blocked-player")])
        service.save({"web_login": {"enabled": True, "action": "disable"}})
        monkeypatch.setattr(app.state, "restrictions", service)
        monkeypatch.setattr(app.state, "emby", service.emby)
        monkeypatch.setattr("app.main.login_request", AsyncMock(return_value=(status, {
            "User": {"Id": "u"}, "SessionInfo": session(Client="Emby Web" if web else "blocked-player"),
            "AccessToken": "synthetic-personal-token"})))
        response = client.post("/api/access/emby-login", json={"Username": "synthetic", "Pw": "wrong"})
        assert response.status_code == expected
        if status == 401:
            assert service.events() == []
            service.emby.stop_session.assert_not_awaited()
            service.emby.apply_member_policy.assert_not_awaited()
        else:
            assert "AccessToken" not in response.text
            if web:
                service.emby.apply_member_policy.assert_awaited_once_with("u", {"IsDisabled": True})
            else:
                service.emby.apply_member_policy.assert_not_awaited()


def test_playback_info_denied_before_lease_and_config_api_authenticated(service, monkeypatch):
    with TestClient(app) as client:
        enable(service)
        monkeypatch.setattr(app.state, "restrictions", service)
        app.state.emby.user_for_token = AsyncMock(return_value="u")
        app.state.emby.verify_item_access = AsyncMock(return_value=True)
        app.state.streams.inspect = AsyncMock()
        response = client.post("/api/playback/info/item42?SessionId=s&DeviceId=d",
                               headers={"X-Emby-Token": "synthetic-token"}, json={})
        assert response.status_code == 403
        app.state.streams.inspect.assert_not_awaited()
        assert client.get("/api/access/session-clients").status_code == 401
        assert client.get("/api/access/session-clients", auth=("admin", "change-me")).json() == CONFIG
        assert client.put("/api/access/session-clients", auth=("admin", "change-me"),
                          json={**CONFIG, "action": "delete"}).status_code == 422
