"""I1-I3 integration: real local services and mock-only external boundaries."""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from test_audit_accounts_invariants import stack as account_stack  # noqa: F401
from test_audit_accounts_telegram import callback

from app.adapters.live import LiveEmby
from app.adapters.mock import MockEmby
from app.main import app
from app.modules.enforcement import EnforcementService

ADMIN = ('admin', 'change-me')


@pytest.fixture
def stack(request):
    return request.getfixturevalue('account_stack')


def live_client(admin_at_write=False, status=204):
    posts = []
    def transport(request):
        if request.url.path == '/emby/Users':
            return httpx.Response(200, json=[{'Id': 'u1', 'Policy': {'IsAdministrator': False}}])
        if request.method == 'GET':
            return httpx.Response(200, json={'Id': 'u1', 'Policy': {
                'IsAdministrator': admin_at_write, 'CustomFlag': 'keep'}})
        posts.append(json.loads(request.content))
        return httpx.Response(status)
    emby = LiveEmby(lambda: {'enabled': True, 'url': 'https://emby.example', 'api_key': 'test-only'})
    emby._client = lambda *args: httpx.AsyncClient(transport=httpx.MockTransport(transport))
    return emby, posts


@pytest.mark.parametrize('method', ['enforce_now', 'reconcile'])
def test_live_admin_promotion_is_skipped_without_fingerprint(stack, method):
    emby, posts = live_client(admin_at_write=True)
    stack.members.set_status('u1', 'suspended')
    enforcement = EnforcementService(stack.db, stack.members, emby)
    result = asyncio.run(enforcement.enforce_now('u1') if method == 'enforce_now'
                         else enforcement.reconcile(apply=True))
    assert not posts
    assert result['ok']
    if method == 'enforce_now':
        assert result['skipped'] == 'administrator' and result['remote_ok'] is None
    else:
        assert result['applied'] == 0
        assert result['skipped'][0]['reason'] == 'administrator'
    member = stack.members.get('u1')
    assert not member['applied_fingerprint']
    assert member['last_remote_ok'] is None


@pytest.mark.parametrize('method', ['enforce_now', 'reconcile'])
def test_live_normal_member_is_really_applied(stack, method):
    emby, posts = live_client()
    stack.members.set_status('u1', 'suspended')
    enforcement = EnforcementService(stack.db, stack.members, emby)
    result = asyncio.run(enforcement.enforce_now('u1') if method == 'enforce_now'
                         else enforcement.reconcile(apply=True))
    assert result['ok'] and result['remote_ok']
    assert posts[0]['IsDisabled'] is True and posts[0]['CustomFlag'] == 'keep'
    assert stack.members.get('u1')['applied_fingerprint']


@pytest.mark.parametrize('mock', [False, True])
def test_manual_policy_can_still_edit_admin_but_automation_cannot(mock):
    if mock:
        emby = MockEmby()
        uid = 'admin'
    else:
        emby, _posts = live_client(admin_at_write=True)
        uid = 'u1'
    async def run():
        skipped = await emby.apply_member_policy(uid, {'IsDisabled': True})
        assert skipped['status'] == 'skipped_admin'
        assert await emby.apply_policy(uid, {'IsDisabled': True}) is True
    asyncio.run(run())


def test_live_policy_failure_never_sets_fingerprint(stack):
    emby, _posts = live_client(status=400)
    result = asyncio.run(EnforcementService(stack.db, stack.members, emby).enforce_now('u1'))
    assert not result['ok'] and result['retryable']
    assert not stack.members.get('u1')['applied_fingerprint']


@pytest.fixture
def integrated():
    with TestClient(app) as client:
        members = app.state.members
        members.upsert('u1', 'demo-user-1', {'group_id': 'standard', 'roles': ['admin']})
        members.bind_telegram('u1', '12')
        bot = app.state.telegram
        bot.calls = []
        async def call(method, payload=None, timeout=20):
            bot.calls.append((method, payload or {}))
            return {'message_id': (payload or {}).get('message_id', 80)} if method in ('sendMessage', 'editMessageText') else True
        bot._call = call
        yield client, bot


def test_telegram_password_change_revokes_old_panel_login(integrated):
    client, bot = integrated
    old = 'old-local-password'
    asyncio.run(bot._emby.set_user_password('u1', old))
    assert client.get('/api/whoami', auth=('demo-user-1', old)).status_code == 200
    original = bot._emby.set_user_password
    issued = []
    async def record(uid, password):
        issued.append(password)
        return await original(uid, password)
    bot._emby.set_user_password = record
    async def run():
        await bot._handle_callback(callback('resetpw'))
        await bot._handle_callback(callback('resetpw_ok:' + bot._pending['12'][2]['nonce']))
    asyncio.run(run())
    assert issued
    assert client.get('/api/whoami', auth=('demo-user-1', old)).status_code == 401
    assert client.get('/api/whoami', auth=('demo-user-1', issued[0])).status_code == 200
    assert issued[0] not in str(bot._db.query('SELECT * FROM audit_log'))


def test_failed_password_reset_keeps_old_login_and_cache(integrated):
    client, bot = integrated
    old = 'old-local-password'
    asyncio.run(bot._emby.set_user_password('u1', old))
    assert client.get('/api/whoami', auth=('demo-user-1', old)).status_code == 200
    cached = app.state.cache.get('panelauth:demo-user-1')
    bot._emby.set_user_password = AsyncMock(return_value=False)
    async def run():
        await bot._handle_callback(callback('resetpw'))
        await bot._handle_callback(callback('resetpw_ok:' + bot._pending['12'][2]['nonce']))
    asyncio.run(run())
    assert app.state.cache.get('panelauth:demo-user-1') == cached
    assert client.get('/api/whoami', auth=('demo-user-1', old)).status_code == 200


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('path', ['command', 'text', 'button', 'bulk', 'gift', 'shop', 'group'])
def test_all_bot_entitlement_paths_use_injected_callback(integrated, monkeypatch, enabled, path):
    _client, bot = integrated
    monkeypatch.setattr(app.state.settings_service, 'membership_config', lambda: {'enforcement_enabled': enabled})
    enforce = AsyncMock(return_value={'ok': True, 'remote_ok': True})
    terminate = AsyncMock(return_value=0)
    monkeypatch.setattr(app.state.enforcement, 'enforce_now', enforce)
    monkeypatch.setattr(app.state.enforcement, 'terminate_users', terminate)
    app.state.cache.set('rate:u1', 'keep-unless-rate-changed')
    member = bot._members.get('u1')
    async def run():
        if path == 'command':
            await bot._handle_command(12, '12', 'operator', '/renew demo-user-1 7')
        elif path == 'text':
            await bot._admin_handle_text(12, member, 'admin_renew_days', {'user_id': 'u1'}, '7')
        elif path == 'button':
            bot._hold_admin_user(12, member, 'operator')
            await bot._admin_user_action(12, 80, member, 'admin_renew')
            kind, _, extra = bot._pending['12']
            await bot._admin_handle_text(12, member, kind, extra, '7')
        elif path == 'bulk':
            await bot._handle_command(12, '12', 'operator', '/renewall 7')
            await bot._handle_callback(callback('admin_ok'))
        elif path == 'gift':
            await bot._handle_command(12, '12', 'operator', '/gift demo-user-1 days 7')
        elif path == 'shop':
            bot._points.add('u1', 100, 'admin.adjust')
            item = bot._shop.create({'kind': 'days', 'name': 'Term', 'cost': 10, 'amount': 7})
            await bot._handle_callback(callback('buy:' + str(item['id'])))
            await bot._handle_callback(callback('buyok:' + str(item['id'])))
        else:
            await bot._preview_group_change(12, member, 'operator', 'vip')
            nonce = bot._pending['12'][2]['group_confirm']['nonce']
            await bot._handle_callback(callback('admin_group_apply:keep:' + nonce))
    asyncio.run(run())
    assert enforce.await_count == int(enabled)
    terminate.assert_not_awaited()
    assert app.state.cache.get('rate:u1') == 'keep-unless-rate-changed'
    if path != 'group':
        assert bot._members.get('u1')['expires_at_effective'] == member['expires_at_effective'] + 7 * 86400


@pytest.mark.parametrize('enabled', [False, True])
def test_bandwidth_purchase_reissues_and_kicks_only_for_changed_rate(integrated, monkeypatch, enabled):
    _client, bot = integrated
    monkeypatch.setattr(app.state.settings_service, 'membership_config', lambda: {'enforcement_enabled': enabled})
    bot._members.set_overrides('u1', {'bandwidth_limit_kbps': 1000})
    bot._points.add('u1', 100, 'admin.adjust')
    item = bot._shop.create({'kind': 'bandwidth', 'name': 'Speed', 'cost': 10, 'amount': 1})
    enforce = AsyncMock(return_value={'ok': True, 'remote_ok': True})
    terminate = AsyncMock(return_value=0)
    monkeypatch.setattr(app.state.enforcement, 'enforce_now', enforce)
    monkeypatch.setattr(app.state.enforcement, 'terminate_users', terminate)
    app.state.cache.set('rate:u1', 'old-signature')
    async def run():
        await bot._handle_callback(callback('buy:' + str(item['id'])))
        await bot._handle_callback(callback('buyok:' + str(item['id'])))
        await bot._handle_callback(callback('buyok:' + str(item['id'])))
    asyncio.run(run())
    assert app.state.cache.get('rate:u1') is None
    assert enforce.await_count == int(enabled)
    terminate.assert_awaited_once()
    assert terminate.call_args.args[0] == {'u1'}
    assert bot._points.balance('u1') == 90 and len(bot._shop.orders('u1')) == 1


@pytest.mark.parametrize('exception', [False, True])
def test_remote_failure_keeps_successful_order_and_reports_partial(integrated, monkeypatch, exception):
    _client, bot = integrated
    monkeypatch.setattr(app.state.settings_service, 'membership_config', lambda: {'enforcement_enabled': True})
    enforce = AsyncMock(side_effect=RuntimeError('private-example-value')) if exception else AsyncMock(return_value={'ok': False, 'remote_ok': False})
    monkeypatch.setattr(app.state.enforcement, 'enforce_now', enforce)
    bot._points.add('u1', 100, 'admin.adjust')
    item = bot._shop.create({'kind': 'days', 'name': 'Term', 'cost': 10, 'amount': 7})
    before = bot._members.get('u1')['expires_at_effective']
    async def run():
        await bot._handle_callback(callback('buy:' + str(item['id'])))
        await bot._handle_callback(callback('buyok:' + str(item['id'])))
    asyncio.run(run())
    assert bot._points.balance('u1') == 90 and len(bot._shop.orders('u1')) == 1
    assert bot._members.get('u1')['expires_at_effective'] == before + 7 * 86400
    assert '远端未确认' in str(bot.calls)
    assert 'private-example-value' not in str(bot.calls)


def test_manual_web_policy_route_keeps_admin_edit_semantics(integrated):
    client, _bot = integrated
    result = client.post('/api/emby/users/admin/policy', auth=ADMIN, json={'IsDisabled': True})
    assert result.status_code == 200
    assert app.state.emby._users['admin']['Policy']['IsDisabled'] is True


@pytest.mark.parametrize('mode', ['false', 'exception'])
def test_rate_kick_failure_is_reported_without_refunding_order(integrated, monkeypatch, mode):
    _client, bot = integrated
    monkeypatch.setattr(app.state.settings_service, 'membership_config', lambda: {'enforcement_enabled': False})
    bot._members.set_overrides('u1', {'bandwidth_limit_kbps': 1000})
    bot._points.add('u1', 100, 'admin.adjust')
    item = bot._shop.create({'kind': 'bandwidth', 'name': 'Speed', 'cost': 10, 'amount': 1})
    bot._emby.active_sessions_raw = AsyncMock(return_value=[{'Id': 'session', 'UserId': 'u1'}])
    bot._emby.stop_session = AsyncMock(return_value=False) if mode == 'false' else AsyncMock(side_effect=RuntimeError('private-example-value'))
    async def run():
        await bot._handle_callback(callback('buy:' + str(item['id'])))
        await bot._handle_callback(callback('buyok:' + str(item['id'])))
    asyncio.run(run())
    assert bot._points.balance('u1') == 90 and len(bot._shop.orders('u1')) == 1
    assert '远端未确认' in str(bot.calls) and 'private-example-value' not in str(bot.calls)


def test_group_change_compares_effective_override_not_group_default(integrated, monkeypatch):
    _client, bot = integrated
    monkeypatch.setattr(app.state.settings_service, 'membership_config', lambda: {'enforcement_enabled': True})
    bot._members.set_overrides('u1', {'bandwidth_limit_kbps': 1000})
    bot._groups.update('vip', {'bandwidth_limit_kbps': 2000})
    terminate = AsyncMock(return_value=0)
    monkeypatch.setattr(app.state.enforcement, 'terminate_users', terminate)
    app.state.cache.set('rate:u1', 'personal-rate')
    async def run():
        member = bot._members.get('u1')
        await bot._preview_group_change(12, member, 'operator', 'vip')
        nonce = bot._pending['12'][2]['group_confirm']['nonce']
        await bot._handle_callback(callback('admin_group_apply:keep:' + nonce))
    asyncio.run(run())
    assert bot._members.get('u1')['bandwidth_limit_kbps'] == 1000
    assert bot._emby._users['u1']['Policy']['RemoteClientBitrateLimit'] == 1_000_000
    assert app.state.cache.get('rate:u1') == 'personal-rate'
    terminate.assert_not_awaited()


def test_bulk_renew_stops_if_authority_changes_during_sync(integrated):
    _client, bot = integrated
    bot._members.upsert('u2', 'later-viewer', {'group_id': 'standard'})
    before = bot._members.get('u2')['expires_at_effective']
    async def changed(uid, bandwidth):
        bot._members.set_roles('u1', [])
        return {'ok': True}
    bot._on_member_changed = changed
    async def run():
        await bot._handle_command(12, '12', 'operator', '/renewall 7')
        await bot._handle_callback(callback('admin_ok'))
    asyncio.run(run())
    assert bot._members.get('u2')['expires_at_effective'] == before
    assert '剩余未执行' in str(bot.calls)


def test_cancel_during_post_order_sync_never_rolls_back_or_duplicates(integrated):
    _client, bot = integrated
    bot._points.add('u1', 100, 'admin.adjust')
    item = bot._shop.create({'kind': 'days', 'name': 'Term', 'cost': 10, 'amount': 7})
    async def run():
        started = asyncio.Event()
        async def changed(*args):
            started.set()
            await asyncio.Event().wait()
        bot._on_member_changed = changed
        await bot._handle_callback(callback('buy:' + str(item['id'])))
        task = asyncio.create_task(bot._handle_callback(callback('buyok:' + str(item['id']))))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await bot._handle_callback(callback('buyok:' + str(item['id'])))
    asyncio.run(run())
    assert bot._points.balance('u1') == 90 and len(bot._shop.orders('u1')) == 1
