"""Local mock accounts only: explicit manual access changes and durable receipts."""
import asyncio
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.live import LiveEmby
from app.adapters.mock import MockEmby
from app.core.db import Database
from app.core.store import SettingsStore
from app.main import app
from app.modules.enforcement import EnforcementService
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.report_delivery import CALL_DELIVERY
from app.modules.restrictions import RestrictionService

ADMIN = ("admin", "change-me")


@pytest.fixture
def service(tmp_path):
    db = Database(tmp_path / "db")
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    members.upsert("u1", "alice", {"group_id": "standard"})
    members.bind_telegram("u1", "123")
    emby = MockEmby()
    bot = AsyncMock()
    bot.send_message.return_value = 42
    enforcement = EnforcementService(db, members, emby)
    result = RestrictionService(db, SettingsStore(tmp_path / "settings.json"), members,
                                emby, bot, lambda: {"group_interaction_chats": ["-1001"]},
                                enforcement=enforcement)
    yield result
    db.close()


def manual(service, action, **kwargs):
    return service.manual("u1", action, actor="operator", **kwargs)


def test_regression_manual_status_with_automatic_enforcement_off():
    # Before this fix this exact request returned remote_ok=None, suspended
    # locally, while MockEmby remained enabled (proved before implementation).
    with TestClient(app) as client:
        app.state.members.upsert("u1", "alice", {"group_id": "standard"})
        app.state.store.set_section("membership", {"enforcement_enabled": False})
        result = client.post("/api/members/u1/status", auth=ADMIN,
                             json={"status": "suspended"}).json()
        assert result["ok"] and result["remote_ok"] is True
        assert app.state.members.get("u1")["status"] == "suspended"
        assert app.state.emby._users["u1"]["Policy"]["IsDisabled"] is True
        enabled = client.post("/api/members/u1/status", auth=ADMIN,
                              json={"status": "active"}).json()
        assert enabled["remote_ok"] is True and enabled["access"]["account_available"]


@pytest.mark.parametrize("prefix", ["/api/members/u1/actions", "/api/emby/users/u1"])
def test_explicit_routes_and_legacy_emby_consistent_even_switch_off(prefix):
    with TestClient(app) as client:
        app.state.members.upsert("u1", "alice", {"group_id": "standard"})
        app.state.store.set_section("membership", {"enforcement_enabled": False})
        assert client.post(prefix + "/disable").status_code == 401
        for action, disabled, local in [("disable", True, "suspended"), ("enable", False, "active")]:
            result = client.post(prefix + "/" + action, auth=ADMIN,
                                 json={"request_id": "web-" + action}).json()
            assert result["ok"] and result["disabled"] is disabled
            assert result["status"] == local
            assert result["event_id"]
        result = client.post("/api/emby/users/u1/policy", auth=ADMIN,
                             json={"IsDisabled": True}).json()
        assert result["status"] == "suspended"
        assert client.post("/api/emby/users/u1/policy", auth=ADMIN,
                           json={"IsDisabled": False, "IsHidden": True}).status_code == 422


def test_bulk_switch_off_reports_protected_and_missing_members():
    with TestClient(app) as client:
        app.state.members.enroll_defaults(asyncio.run(app.state.emby.list_users()))
        app.state.store.set_section("membership", {"enforcement_enabled": False})
        body = client.post("/api/members/bulk", auth=ADMIN,
                           json={"action": "suspend", "user_ids": ["u1", "u1", "admin", "gone"]}).json()
        assert body["requested"] == 3 and body["ok"] == 1
        assert {r["user_id"] for r in body["failed"]} == {"admin", "gone"}
        assert app.state.emby._users["u1"]["Policy"]["IsDisabled"] is True
        assert app.state.members.get("admin")["status"] == "active"
        body = client.post("/api/members/bulk", auth=ADMIN,
                           json={"action": "activate", "user_ids": ["u1"]}).json()
        assert body["results"][0]["access"]["account_available"]


@pytest.mark.asyncio
async def test_violation_release_reconcile_and_new_violation(service):
    service.save({"web_login": {"enabled": True, "action": "disable"}})
    proof = AsyncMock(return_value={"session_id": "login", "playing": False})
    first = await service.apply("web_login", "u1", proof)
    assert service.members.get("u1")["state"] == "suspended"
    result = await manual(service, "enable")
    assert result["ok"] and result["access"]["account_available"]
    await service.enforcement.reconcile(apply=True)
    assert service.emby._users["u1"]["Policy"]["IsDisabled"] is False
    # A fresh confirmed attempt after release is not a permanent exemption,
    # even within the old five-minute login receipt window.
    second = await service.apply("web_login", "u1", proof)
    assert second["id"] != first["id"] and second["status"] == "done"
    assert service.members.get("u1")["status"] == "suspended"


@pytest.mark.asyncio
async def test_remote_only_disable_can_be_released_without_enrolment(service):
    result = await service.manual("u2", "enable", actor="operator")
    assert result["ok"] and result["access"]["emby_disabled"] is False
    assert service.members.get("u2") is None
    # An enrolled active member independently disabled in Emby also works.
    service.emby._users["u1"]["Policy"]["IsDisabled"] = True
    assert (await manual(service, "enable"))["access"]["account_available"]


@pytest.mark.parametrize("limit", ["expired", "exhausted", "pending"])
@pytest.mark.asyncio
async def test_release_does_not_bypass_entitlements_or_change_other_facts(service, limit):
    members = service.members
    members.set_status("u1", "pending" if limit == "pending" else "suspended")
    if limit == "expired":
        members.upsert("u1", "alice", {"expires_at": int(time.time()) - 10})
    elif limit == "exhausted":
        service.db.execute("UPDATE members SET traffic_used_bytes=9999999999999 WHERE emby_user_id='u1'")
    before = members.get("u1")
    service.emby._users["u1"]["Policy"].update({"IsDisabled": True, "IsHidden": True, "EnableRemoteAccess": False})
    policy = dict(service.emby._users["u1"]["Policy"])
    result = await manual(service, "enable")
    assert result["ok"] and result["access"]["state"] == limit
    assert not result["access"]["account_available"]
    assert result["access"]["remaining_restrictions"] and "仍不可用" in result["result"]
    assert service.emby._users["u1"]["Policy"] == policy
    after = members.get("u1")
    for key in ("expires_at", "traffic_used_bytes", "group_id", "roles", "overrides"):
        assert before[key] == after[key]
    await service.enforcement.reconcile(apply=True)
    assert service.emby._users["u1"]["Policy"]["IsDisabled"] is True


@pytest.mark.asyncio
async def test_disable_then_release_preserves_pending(service):
    service.members.set_status("u1", "pending")
    assert (await manual(service, "disable"))["ok"]
    result = await manual(service, "enable")
    assert result["access"]["state"] == "pending" and result["access"]["emby_disabled"] is True


@pytest.mark.parametrize("guard", ["remote_admin", "deck_admin", "self", "unknown", "missing", "list_failure"])
@pytest.mark.asyncio
async def test_admin_self_and_unverifiable_identity_protection(service, guard):
    kwargs = {}
    if guard == "remote_admin":
        service.emby._users["u1"]["Policy"]["IsAdministrator"] = True
    elif guard == "deck_admin":
        service.members.set_roles("u1", ["admin"])
    elif guard == "self":
        kwargs["actor_user_id"] = "u1"
    elif guard == "unknown":
        service.emby._users["u1"]["Policy"].pop("IsAdministrator")
    elif guard == "missing":
        service.emby._users.pop("u1")
    else:
        service.emby.list_users = AsyncMock(side_effect=RuntimeError("secret"))
    service.emby.apply_member_policy = AsyncMock()
    result = await manual(service, "disable", **kwargs)
    assert result["ok"] is False and result["local_ok"] is False
    assert service.members.get("u1")["status"] == "active"
    service.emby.apply_member_policy.assert_not_awaited()
    assert "secret" not in str(result)


@pytest.mark.parametrize("action", ["disable", "enable"])
@pytest.mark.parametrize("failure", ["failed", "exception", "skipped_admin"])
@pytest.mark.asyncio
async def test_remote_failure_and_admin_promotion_does_not_claim_success(service, action, failure):
    if action == "enable":
        service.members.set_status("u1", "suspended")
        service.emby._users["u1"]["Policy"]["IsDisabled"] = True
    before = service.members.get("u1")["status"]
    service.emby.apply_member_policy = AsyncMock(
        side_effect=RuntimeError("private credential") if failure == "exception" else None,
        return_value={"status": failure})
    result = await manual(service, action)
    assert not result["ok"] and result["retryable"]
    assert service.members.get("u1")["status"] == before
    assert service.events()[0]["status"] != "done"
    assert "private credential" not in str(result)


@pytest.mark.asyncio
async def test_readback_failure_is_unknown_not_enabled(service):
    read = service.emby.list_users
    count = 0
    async def users():
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("unavailable")
        return await read()
    service.emby.list_users = users
    result = await manual(service, "disable")
    assert not result["ok"] and result["remote_ok"] is None
    assert result["local_ok"] and result["access"]["emby_disabled"] is None
    assert service.members.get("u1")["status"] == "suspended"


@pytest.mark.asyncio
async def test_duplicate_requests_noop_and_notice_receipts(service):
    write = AsyncMock(wraps=service.emby.apply_member_policy)
    service.emby.apply_member_policy = write
    first, second = await asyncio.gather(manual(service, "disable", request_id="req-one"),
                                         manual(service, "disable", request_id="req-one"))
    assert first["event_id"] == second["event_id"]
    await manual(service, "disable", request_id="req-two")
    await manual(service, "disable")
    assert write.await_count == 1
    await service.deliver()
    await service.deliver()
    assert service.telegram.send_message.await_count == 2
    assert "管理员手动操作" in service.telegram.send_message.await_args.args[1]
    assert "网页登录" not in service.telegram.send_message.await_args.args[1]
    await manual(service, "enable", request_id="req-three")
    replay = await manual(service, "disable", request_id="req-two")
    assert replay["access"]["emby_disabled"] is False and "未重复执行" in replay["result"]
    assert write.await_count == 2
    mismatch = await manual(service, "enable", request_id="req-one")
    assert not mismatch["ok"] and "不符" in mismatch["result"]
    await service.deliver()
    assert service.telegram.send_message.await_count == 4


@pytest.mark.asyncio
async def test_notices_independent_unbound_blocked_and_failure_does_not_undo(service):
    async def send(chat, text):
        if chat == "123":
            CALL_DELIVERY.set({"state": "failed", "reason": "被屏蔽"})
            return None
        return 42
    service.telegram.send_message.side_effect = send
    result = await manual(service, "enable")
    assert result["ok"]
    await service.deliver()
    await service.deliver()
    notices = {n["kind"]: n for n in service.events()[0]["notices"]}
    assert notices["private"]["state"] == "failed"
    assert notices["group:-1001"]["state"] == "sent"
    assert service.telegram.send_message.await_count == 2
    service.members.unbind_telegram("u1")
    await manual(service, "disable")
    await service.deliver()
    assert any(n["kind"] == "private" and n["state"] == "skipped" for n in service.events()[0]["notices"])


@pytest.mark.asyncio
async def test_manual_and_old_reconcile_are_serialized(service):
    service.members.set_status("u1", "suspended")
    entered, resume = asyncio.Event(), asyncio.Event()
    apply = service.emby.apply_member_policy
    async def delayed(uid, patch, **kwargs):
        if not kwargs:
            entered.set()
            await resume.wait()
        return await apply(uid, patch, **kwargs)
    service.emby.apply_member_policy = delayed
    old = asyncio.create_task(service.enforcement.reconcile(apply=True))
    await entered.wait()
    newer = asyncio.create_task(manual(service, "enable"))
    await asyncio.sleep(0)
    assert not newer.done()
    resume.set()
    await asyncio.wait_for(asyncio.gather(old, newer), 2)
    assert service.members.get("u1")["state"] == "active"
    assert service.emby._users["u1"]["Policy"]["IsDisabled"] is False


@pytest.mark.asyncio
async def test_auto_sanction_and_manual_release_are_serialized(service):
    service.save({"web_play": {"enabled": True, "action": "disable"}})
    entered, resume = asyncio.Event(), asyncio.Event()
    async def confirm():
        entered.set()
        await resume.wait()
        return {"session_id": "new-play", "playing": False}
    sanction = asyncio.create_task(service.apply("web_play", "u1", confirm))
    await entered.wait()
    release = asyncio.create_task(manual(service, "enable"))
    await asyncio.sleep(0)
    resume.set()
    await asyncio.wait_for(asyncio.gather(sanction, release), 2)
    assert service.members.get("u1")["state"] == "active"
    assert service.emby._users["u1"]["Policy"]["IsDisabled"] is False


@pytest.mark.parametrize("change", ["remote_admin", "unknown_policy", "reviewer", "deck_admin", "pending", "wrong_target"])
@pytest.mark.asyncio
async def test_live_adapter_rechecks_admin_authority_after_fresh_policy_read(service, change):
    writes = []
    allowed = True
    async def handler(request):
        nonlocal allowed
        if request.method == "GET":
            if request.url.path.endswith("/Users"):
                return httpx.Response(200, json=[{"Id": "u1", "Policy": {"IsAdministrator": False, "IsDisabled": False}}])
            policy = {"IsAdministrator": False, "IsDisabled": False}
            if change == "remote_admin":
                policy["IsAdministrator"] = True
            elif change == "unknown_policy":
                policy.pop("IsAdministrator")
            elif change == "reviewer":
                allowed = False
            elif change == "deck_admin":
                service.members.set_roles("u1", ["admin"])
            elif change == "pending":
                service.members.set_status("u1", "pending")
            return httpx.Response(200, json={"Id": "wrong" if change == "wrong_target" else "u1", "Policy": policy})
        writes.append(request)
        return httpx.Response(204)
    live = LiveEmby(lambda: {"enabled": True, "url": "https://emby.invalid", "api_key": "test-only"})
    live._client = lambda *a: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service.emby = live
    result = await manual(service, "disable", authorize=lambda: allowed)
    assert not result["ok"] and writes == []
    assert service.members.get("u1")["status"] != "suspended"


@pytest.mark.asyncio
async def test_release_reports_other_remote_limits_and_preserves_policy(service):
    policy = service.emby._users['u1']['Policy']
    policy.update({'IsDisabled': True, 'EnableRemoteAccess': False, 'EnableMediaPlayback': False})
    result = await manual(service, 'enable')
    assert result['ok'] and result['access']['emby_disabled'] is False
    assert not result['access']['account_available'] and '仍不可用' in result['result']
    assert '禁止远程访问' in result['result'] and '禁止媒体播放' in result['result']
    assert policy['EnableRemoteAccess'] is False and policy['EnableMediaPlayback'] is False
    await service.deliver()
    assert '仍不可用' in service.telegram.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_measured_exhaustion_survives_release_without_reset(service):
    class Ledger:
        def snapshot(self, uid):
            return {'measured_used_bytes': 9999999999999, 'measured_raw_bytes': 9999999999999}
    # Use the actual measured decorator, not a legacy-usage fallback.
    service.members.bind_metering(Ledger(), cutover=True)
    service.members.set_status('u1', 'suspended')
    result = await manual(service, 'enable')
    assert result['access']['state'] == 'exhausted'
    assert result['access']['measured_used_bytes'] == 9999999999999
    assert result['access']['emby_disabled'] is True


@pytest.mark.asyncio
async def test_stop_failure_can_retry_without_repeat_policy_or_success_notices(service):
    service.emby.active_sessions_raw = AsyncMock(side_effect=RuntimeError('unavailable'))
    write = AsyncMock(wraps=service.emby.apply_member_policy)
    service.emby.apply_member_policy = write
    first = await manual(service, 'disable', request_id='stop-one')
    assert first['ok'] and '会话终止未确认' in first['result']
    await service.deliver()
    assert service.telegram.send_message.await_count == 2
    service.emby.active_sessions_raw = AsyncMock(return_value=[])
    second = await manual(service, 'disable', request_id='stop-two')
    assert second['ok'] and '会话终止未确认' not in second['result']
    await service.deliver()
    assert write.await_count == 1 and service.telegram.send_message.await_count == 2


@pytest.mark.asyncio
async def test_manual_receipts_persist_restart_and_never_replay_success(service):
    result = await manual(service, 'disable', request_id='persisted')
    await service.deliver()
    restarted = RestrictionService(service.db, service.store, service.members, service.emby,
                                   service.telegram, service.telegram_config, service.enforcement)
    service.emby.apply_member_policy = AsyncMock()
    replay = await manual(restarted, 'disable', request_id='persisted')
    assert replay['event_id'] == result['event_id']
    await restarted.deliver()
    service.emby.apply_member_policy.assert_not_awaited()
    assert service.telegram.send_message.await_count == 2


def test_manual_failed_retry_remote_requires_new_explicit_confirmation():
    with TestClient(app) as client:
        app.state.members.upsert('u1', 'alice', {'group_id': 'standard'})
        app.state.emby.apply_member_policy = AsyncMock(return_value={'status': 'failed'})
        first = client.post('/api/members/u1/actions/disable', auth=ADMIN).json()
        assert not first['ok'] and first['retryable']
        before = app.state.emby.apply_member_policy.await_count
        assert client.post('/api/members/u1/retry-remote', auth=ADMIN).status_code == 409
        assert app.state.emby.apply_member_policy.await_count == before
        assert app.state.members.get('u1')['status'] == 'active'


def test_bot_manual_remote_only_change_invalidates_web_observation_without_status_write():
    with TestClient(app) as client:
        app.state.members.upsert('u2', 'bob', {'group_id': 'standard'})
        before = client.get('/api/members/u2', auth=ADMIN).json()['member']
        assert before['emby_disabled'] is True and before['status'] == 'active'
        result = asyncio.run(app.state.restrictions.manual('u2', 'enable', actor='tg:operator'))
        assert result['ok'] and app.state.members.get('u2')['status'] == 'active'
        after = client.get('/api/members/u2', auth=ADMIN).json()['member']
        assert after['emby_disabled'] is False


def test_cookie_admin_role_revoked_before_manual_action_is_rejected():
    with TestClient(app) as client:
        app.state.members.upsert('u1', 'alice', {'group_id': 'standard'})
        app.state.members.upsert('u2', 'operator', {'roles': ['admin']})
        app.state.cache.set('panelsess:local-session-only', 'operator', ttl=60)
        client.cookies.set('mediadeck_session', 'local-session-only')
        app.state.members.set_roles('u2', [])
        response = client.post('/api/members/u1/actions/disable').json()
        assert not response['ok'] and '权限已变化' in response['result']
        assert app.state.emby._users['u1']['Policy']['IsDisabled'] is False


def test_bulk_request_identity_does_not_replay_disable_after_new_enable():
    with TestClient(app) as client:
        app.state.members.upsert('u1', 'alice', {'group_id': 'standard'})
        payload = {'action': 'suspend', 'user_ids': ['u1'], 'request_id': 'bulk-one'}
        first = client.post('/api/members/bulk', auth=ADMIN, json=payload).json()
        assert first['ok'] == 1
        client.post('/api/members/u1/actions/enable', auth=ADMIN, json={'request_id': 'new-enable'})
        replay = client.post('/api/members/bulk', auth=ADMIN, json=payload).json()
        assert replay['results'][0]['event_id'] == first['results'][0]['event_id']
        assert replay['results'][0]['access']['emby_disabled'] is False
        assert app.state.members.get('u1')['status'] == 'active'
        assert app.state.emby._users['u1']['Policy']['IsDisabled'] is False


@pytest.mark.asyncio
async def test_automatic_disable_preserves_pending_on_release(service):
    service.members.set_status('u1', 'pending')
    service.save({'web_login': {'enabled': True, 'action': 'disable'}})
    event = await service.apply('web_login', 'u1', AsyncMock(return_value={'session_id': 'pending-login'}))
    assert event['status'] == 'done' and service.members.get('u1')['status'] == 'pending'
    result = await manual(service, 'enable')
    assert result['access']['state'] == 'pending' and result['access']['emby_disabled'] is True
