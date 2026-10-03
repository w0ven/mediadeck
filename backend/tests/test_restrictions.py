"""Sanctions require authenticated evidence; receipts survive delete/restart."""
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from test_membership import _basic

from app.core.db import Database
from app.core.store import SettingsStore
from app.main import app
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.report_delivery import CALL_DELIVERY
from app.modules.restrictions import RestrictionService


@pytest.fixture
def service(tmp_path):
    db = Database(tmp_path / "db")
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    members.upsert("u", "<name>", {"group_id": "standard"})
    members.bind_telegram("u", "123")
    emby = AsyncMock()
    emby.list_users.return_value = [{"Id": "u", "Name": "<name>", "Policy": {"IsAdministrator": False}}]
    emby.apply_member_policy.return_value = {"status": "applied"}
    emby.delete_user.return_value = True
    emby.stop_session.return_value = True
    bot = AsyncMock()
    bot.send_message.return_value = 42
    result = RestrictionService(db, SettingsStore(tmp_path / "settings.json"), members, emby, bot,
                                lambda: {"group_interaction_chats": ["-1001"]})
    yield result
    db.close()


def proof(playing=False):
    return AsyncMock(return_value={"session_id": "s", "playing": playing, "cap": 1, "live_count": 1})


def test_pause_dedup_deliver_restart(service):
    async def run():
        first = await service.apply("concurrency", "u", proof())
        second = await service.apply("concurrency", "u", proof())
        assert first["id"] == second["id"]
        service.emby.stop_session.assert_not_awaited()
        service.emby.apply_member_policy.assert_not_awaited()
        await service.deliver()
        await service.deliver()
        assert service.telegram.send_message.await_count == 2
        assert "&lt;name&gt;" in service.telegram.send_message.await_args.args[1]
        restarted = RestrictionService(service.db, service.store, service.members, service.emby, service.telegram, service.telegram_config)
        await restarted.deliver()
        assert service.telegram.send_message.await_count == 2
        assert all(n["state"] == "sent" for n in restarted.events()[0]["notices"])
    asyncio.run(run())


def test_only_exact_play_is_stopped(service):
    asyncio.run(service.apply("concurrency", "u", proof(True)))
    service.emby.stop_session.assert_awaited_once_with("s")
    assert "待确认" in service.events()[0]["result"]


@pytest.mark.parametrize("rule", ["web_login", "web_play", "concurrency"])
@pytest.mark.parametrize("action", ["disable", "delete"])
def test_configurable_sanction_preserves_receipts(service, rule, action):
    service.save({rule: {"enabled": True, "action": action}})
    asyncio.run(service.apply(rule, "u", proof()))
    assert service.events()[0]["status"] == "done"
    if action == "disable":
        service.emby.apply_member_policy.assert_awaited_once_with("u", {"IsDisabled": True})
        assert service.members.get("u")["state"] == "suspended"
    else:
        service.emby.delete_user.assert_awaited_once_with("u")
        assert service.members.get("u") is None
    asyncio.run(service.deliver())
    assert {c.args[0] for c in service.telegram.send_message.await_args_list} == {"123", "-1001"}


@pytest.mark.parametrize("admin", ["emby", "deck", "unknown"])
def test_admin_or_unverifiable_never_punished(service, admin):
    if admin == "emby":
        service.emby.list_users.return_value[0]["Policy"]["IsAdministrator"] = True
    elif admin == "deck":
        service.members.set_roles("u", ["admin"])
    else:
        service.emby.list_users.return_value[0].pop("Policy")
    assert asyncio.run(service.apply("concurrency", "u", proof())) is None
    assert service.events() == []
    service.emby.stop_session.assert_not_awaited()


def test_exit_before_confirmation_no_event(service):
    assert asyncio.run(service.apply("concurrency", "u", AsyncMock(return_value=None))) is None
    assert service.events() == []


def test_whitelist_not_exempt(service):
    service.members.upsert("u", "name", {"group_id": "whitelist"})
    assert asyncio.run(service.apply("concurrency", "u", proof()))["status"] == "done"


def test_unknown_sanction_never_replayed(service):
    service.save({"concurrency": {"enabled": True, "action": "delete"}})
    service.emby.delete_user.side_effect = httpx.ReadTimeout("private token must not leak")
    asyncio.run(service.apply("concurrency", "u", proof()))
    asyncio.run(service.apply("concurrency", "u", proof()))
    service.emby.delete_user.assert_awaited_once()
    assert service.events()[0]["status"] == "unknown"
    assert "private token" not in str(service.events())


def test_notifications_retry_only_failed_destination(service):
    asyncio.run(service.apply("concurrency", "u", proof()))
    async def send(chat, text):
        if chat == "123":
            CALL_DELIVERY.set({"state": "retry", "reason": "限流", "retry_after": 1})
            return None
        return 42
    service.telegram.send_message.side_effect = send
    asyncio.run(service.deliver())
    for attempt in range(3):
        service.db.execute("UPDATE restriction_notices SET next_at=0")
        asyncio.run(service.deliver())
    assert service.telegram.send_message.await_count == 5
    notices = {n["kind"]: n for n in service.events()[0]["notices"]}
    assert notices["private"]["state"] == "failed"
    assert notices["private"]["attempts"] == 4
    assert notices["group:-1001"]["state"] == "sent"


def test_unknown_and_blocked_send_not_retried(service):
    asyncio.run(service.apply("concurrency", "u", proof()))
    service.telegram.send_message.side_effect = httpx.ReadTimeout("secret")
    asyncio.run(service.deliver())
    asyncio.run(service.deliver())
    assert service.telegram.send_message.await_count == 2
    assert all(n["state"] == "unknown" for n in service.events()[0]["notices"])


def test_sampler_rechecks_activity_and_order(service):
    service.members.set_overrides("u", {"max_streams": 1})
    service.emby.active_sessions_raw.return_value = [
        {"Id": "a", "UserId": "u", "NowPlayingItem": {"Id": "i"}, "PlayStartTime": "1"},
        {"Id": "s", "UserId": "u", "PlayStartTime": "2"}]
    assert not asyncio.run(service.sampled_concurrency("u", "s"))
    assert service.events() == []
    service.emby.active_sessions_raw.return_value[1]["NowPlayingItem"] = {"Id": "i"}
    assert asyncio.run(service.sampled_concurrency("u", "s"))
    service.emby.stop_session.assert_awaited_once_with("s")


@pytest.fixture
def client(monkeypatch):
    with TestClient(app) as c:
        c.put("/api/members/u1", headers=_basic(), json={"username": "demo", "group_id": "standard"})
        app.state.restrictions.save({"web_login": {"enabled": True, "action": "disable"},
                                     "web_play": {"enabled": True, "action": "disable"}})
        app.state.emby.set_sessions([{"Id": "s", "UserId": "u1", "DeviceId": "d", "Client": "Emby Web"}])
        # Explicit live identity so tests are not dependent on mock seed names.
        app.state.emby.list_users = AsyncMock(return_value=[{"Id": "u1", "Name": "demo", "Policy": {"IsAdministrator": False}}])
        app.state.emby.apply_member_policy = AsyncMock(return_value={"status": "applied"})
        app.state.telegram.send_message = AsyncMock(return_value=42)
        yield c


def login_data():
    return {"AccessToken": "never-return-this", "User": {"Id": "u1"},
            "SessionInfo": {"Id": "s", "UserId": "u1", "Client": "Emby Web"}}


def test_web_login_authenticated_banned_token_withheld(client, monkeypatch):
    monkeypatch.setattr("app.main.login_request", AsyncMock(return_value=(200, login_data())))
    response = client.post("/api/access/emby-login", json={"Username": "demo", "Pw": "password"})
    assert response.status_code == 403
    assert "never-return-this" not in response.text
    assert app.state.members.get("u1")["state"] == "suspended"
    assert app.state.restrictions.events()[0]["rule"] == "web_login"


def test_invalid_password_or_page_visit_no_sanction(client, monkeypatch):
    monkeypatch.setattr("app.main.login_request", AsyncMock(return_value=(401, {})))
    assert client.post("/api/access/emby-login", json={"Username": "demo", "Pw": "wrong"}).status_code == 401
    assert client.get("/api/access/emby-login").status_code == 405
    assert not app.state.restrictions.events()
    app.state.emby.apply_member_policy.assert_not_awaited()


def test_admin_web_login_not_banned(client, monkeypatch):
    app.state.emby.list_users.return_value[0]["Policy"]["IsAdministrator"] = True
    monkeypatch.setattr("app.main.login_request", AsyncMock(return_value=(200, login_data())))
    response = client.post("/api/access/emby-login", json={"Username": "admin", "Pw": "password"})
    assert response.status_code == 200
    assert response.json()["AccessToken"] == "never-return-this"
    assert not app.state.restrictions.events()


@pytest.mark.parametrize("path", ["/Videos/item42/stream.mkv?Static=true", "/api/playback/info/item42",
                                  "/api/playback/admit"])
def test_web_watch_all_issuers_refuse_before_url(client, path):
    response = client.get(path, headers={"X-Emby-Token": "tok:u1", "X-Emby-Device-Id": "d",
                                        "X-Original-URI": "/Videos/item42/master.m3u8"}, follow_redirects=False)
    assert response.status_code == 403
    assert "location" not in response.headers
    assert app.state.restrictions.events()[0]["rule"] == "web_play"


def test_ua_alone_is_not_web_violation(client):
    app.state.emby.set_sessions([{"Id": "s", "UserId": "u1", "DeviceId": "d", "Client": "Hills"}])
    response = client.get("/api/playback/admit", headers={"X-Emby-Token": "tok:u1", "X-Emby-Device-Id": "d",
                            "User-Agent": "Mozilla/5.0 Chrome Safari", "X-Original-URI": "/Videos/item42/master.m3u8"})
    assert response.status_code == 204
    assert not app.state.restrictions.events()


def test_config_authenticated_validated(client):
    assert client.get("/api/access/restrictions").status_code == 401
    assert client.put("/api/access/restrictions", headers=_basic(), json={"web_login": {"enabled": True, "action": "typo"}}).status_code == 422
    assert client.put("/api/access/restrictions", headers=_basic(), json={"web_login": {"enabled": False, "action": "pause"}}).status_code == 200


def test_login_transport_strips_tokens_retains_client(monkeypatch):
    from app.adapters.live import LiveEmby
    from app.modules.restriction_http import login_request
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=login_data())
    emby = LiveEmby(lambda: {"enabled": True, "url": "http://emby", "api_key": "ADMIN-SECRET"})
    monkeypatch.setattr(emby, "_client", lambda *args: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    asyncio.run(login_request(emby, {"x-emby-authorization": 'MediaBrowser Client="Emby Web", DeviceId="d", Token="BAD"', "x-emby-token": "BAD"}, {"api_key": "BAD"}, {"Username": "demo", "Pw": "pw"}))
    assert seen
    assert "ADMIN-SECRET" not in str(seen[0].headers) and "BAD" not in str(seen[0].headers)
    assert "Emby Web" in seen[0].headers["x-emby-authorization"]
    assert "api_key" not in str(seen[0].url)


def test_web_missing_device_not_bypass_or_punishment(client):
    response = client.get('/api/playback/admit', headers={'X-Emby-Token': 'tok:u1',
                           'X-Original-URI': '/Videos/item42/master.m3u8?SessionId=s'})
    assert response.status_code == 503
    assert app.state.restrictions.events() == []


def test_web_bound_hls_without_device_still_checked(client):
    app.state.db.execute("INSERT INTO stream_leases(user_id,session_id,play_id,pending_at) VALUES('u1','s','play-1',1)")
    response = client.get('/api/playback/admit', headers={'X-Emby-Token': 'tok:u1',
                           'X-Original-URI': '/Videos/item42/master.m3u8?PlaySessionId=play-1'})
    assert response.status_code == 403
    assert app.state.restrictions.events()[0]['rule'] == 'web_play'


def test_overlimit_http_real_vs_pending_and_exit(client):
    service = app.state.restrictions
    service.save({'web_play': {'enabled': False, 'action': 'disable'}})
    app.state.members.set_overrides('u1', {'max_streams': 1})
    app.state.emby.set_sessions([{'Id': s, 'UserId': 'u1', 'DeviceId': s} for s in 'ab'])
    def admit(device):
        return client.get('/api/playback/admit', headers={'X-Emby-Token': 'tok:u1',
            'X-Emby-Device-Id': device, 'X-Original-URI': '/Videos/item42/master.m3u8'})
    assert admit('a').status_code == 204
    assert admit('b').status_code == 403
    assert not service.events()  # pending start is not evidence of watching
    app.state.emby.set_sessions([{'Id': 'a', 'UserId': 'u1', 'DeviceId': 'a', 'NowPlayingItem': {'Id': 'i'}},
                                 {'Id': 'b', 'UserId': 'u1', 'DeviceId': 'b'}])
    assert admit('b').status_code == 403
    assert service.events()[0]['rule'] == 'concurrency'
    assert service.events()[0]['action'] == 'pause'
    app.state.emby.apply_member_policy.assert_not_awaited()
    app.state.emby.set_sessions([{'Id': s, 'UserId': 'u1', 'DeviceId': s} for s in 'ab'])
    assert admit('b').status_code == 204
    assert len(service.events()) == 1


def test_admin_promoted_during_disable_restores_local_state(service):
    service.save({'concurrency': {'enabled': True, 'action': 'disable'}})
    before = service.members.get('u')['status']
    service.emby.apply_member_policy.return_value = {'status': 'skipped_admin'}
    asyncio.run(service.apply('concurrency', 'u', proof()))
    assert service.members.get('u')['status'] == before
