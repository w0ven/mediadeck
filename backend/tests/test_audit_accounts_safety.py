"""Late identity, concurrency and transport regression evidence."""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from test_audit_accounts_invariants import stack as account_stack  # noqa: F401
from test_audit_accounts_telegram import bot as account_bot  # noqa: F401
from test_audit_accounts_telegram import callback

from app.modules.plugins_points import PointsTransferPlugin
from app.modules.shop import ShopError


@pytest.fixture
def stack(request):
    return request.getfixturevalue("account_stack")


@pytest.fixture
def bot(request):
    return request.getfixturevalue("account_bot")


def test_points_cap_checked_under_same_lock(stack, monkeypatch):
    plugin = PointsTransferPlugin(SimpleNamespace(db=stack.db, points=stack.points))
    original = stack.points.spent_since
    def spent(*args):
        value = original(*args)
        time.sleep(0.01)
        return value
    monkeypatch.setattr(stack.points, 'spent_since', spent)
    def transfer(_):
        try:
            return plugin.transfer('u1', 'recipient', 100)['ok']
        except ValueError:
            return False
    with ThreadPoolExecutor(10) as pool:
        results = list(pool.map(transfer, range(10)))
    assert sum(results) == 5
    assert stack.points.balance('u1') == stack.points.balance('recipient') == 500


def test_shop_limit_and_grants_serialized(stack):
    item = stack.shop.create({'kind': 'traffic', 'name': 'Bundle', 'cost': 10,
                              'amount': 1, 'per_user_limit': 3})
    def redeem(_):
        try:
            stack.shop.redeem('u1', item['id'])
            return True
        except ShopError:
            return False
    with ThreadPoolExecutor(10) as pool:
        results = list(pool.map(redeem, range(10)))
    assert sum(results) == 3
    assert stack.points.balance('u1') == 970
    assert stack.members.get('u1')['overrides']['extra_traffic_bytes'] == 3 * 1024**3


def test_password_result_not_disclosed_after_rebinding(bot):
    bot._members.bind_telegram('u1', '12')
    issued = []
    async def password(uid, value):
        issued.append(value)
        bot._members.bind_telegram('u1', '13')
        return True
    bot._emby.set_user_password = password
    async def run():
        await bot._handle_callback(callback('resetpw'))
        nonce = bot._pending['12'][2]['nonce']
        await bot._handle_callback(callback('resetpw_ok:' + nonce))
    asyncio.run(run())
    assert issued and issued[0] not in str(bot.calls)


def test_rebind_verification_cannot_continue_after_pending_expired(bot):
    bot._members.bind_telegram('u1', '20')
    async def authenticate(*args):
        bot._pending['12'] = ('rebind_verify', time.time() - 1, {})
        bot._sweep_pending()
        return {'Id': 'u1'}
    bot._emby.authenticate_user = authenticate
    async def run():
        await bot._start_rebind(12, '12')
        await bot._submit_rebind({'message_id': 81}, 12, '12', 'guest', 'viewer example-password')
    asyncio.run(run())
    assert not bot._db.query('SELECT * FROM tg_requests')
    assert 'example-password' not in str(bot.calls)


def test_disabled_transfer_does_not_consume_confirmation(bot):
    bot._members.bind_telegram('u1', '12')
    class Registry:
        def enabled(self, name):
            return False
        def get(self, name):
            return SimpleNamespace(transfer=lambda *args: pytest.fail('disabled transfer executed'))
    bot._plugins = Registry()
    bot._pending['12'] = ('transfer_confirm', time.time() + 60,
                          {'to_id': 'recipient', 'to_name': 'recipient', 'amount': 10})
    asyncio.run(bot._handle_callback(callback('transfer_ok')))


def test_explicit_tmdb_type_not_silently_replaced_on_lookup_miss(bot):
    class Metadata:
        async def resolve(self, kind, ident):
            return 'tv', {'title': 'Different work', 'year': 2001}
    bot._tmdb = Metadata()
    asyncio.run(bot._request_pick_title(12, {}, 'https://www.themoviedb.org/movie/101'))
    assert bot._pending['12'][2]['media_type'] == 'movie'
    assert 'Different work' not in str(bot.calls)


def test_bot_identity_change_resets_poll_offset(bot):
    bot._offset = 999999
    bot._cfg()['bot_token'] = '456' + ':test-only'
    bot._check_bot_identity()
    assert bot._offset == 0


def test_transport_malformed_response_is_safe(bot):
    import httpx
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=['unexpected']))) as client:
            async def local_client():
                return client
            bot._client = local_client
            from app.modules.telegram import TelegramBot
            assert await TelegramBot._call(bot, 'getMe') is None
    asyncio.run(run())


def test_transport_description_never_returns_bot_token(bot):
    import httpx
    token = bot._cfg()['bot_token']
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(400, json={'ok': False, 'description': 'bad ' + token}))) as client:
            async def local_client():
                return client
            bot._client = local_client
            from app.modules.telegram import TelegramBot
            await TelegramBot._call(bot, 'getMe')
    asyncio.run(run())
    assert token not in str(bot.status())


def test_registration_local_failure_rolls_back_member_binding_and_consumption(bot):
    bot._registration.grant_admin('12')
    bot._db.execute("CREATE TRIGGER fail_grant BEFORE UPDATE ON admin_grants BEGIN SELECT RAISE(ABORT, 'injected'); END")
    asyncio.run(bot._finish_registration(12, '12', 'guest', 'guestname', bot._registration.resolve('12')))
    assert bot._member_for_chat('12') is None
    assert len(bot._members.list()) == 1
    assert not bot._emby.users
    assert not bot._registration.get_grant('12')['used_at']
    assert not bot._db.query("SELECT * FROM audit_log WHERE subject LIKE 'new-%'")


def test_enforcement_concurrent_old_write_cannot_win(stack):
    from app.modules.enforcement import EnforcementService
    async def run():
        started = asyncio.Event()
        release = asyncio.Event()
        class Emby:
            def __init__(self):
                self.policy = None
                self.calls = 0
            async def list_users(self):
                return [{'Id': 'u1', 'Policy': {}}]
            async def apply_member_policy(self, uid, policy):
                self.calls += 1
                if self.calls == 1:
                    started.set()
                    await release.wait()
                self.policy = policy
                return {'status': 'applied'}
        emby = Emby()
        service = EnforcementService(stack.db, stack.members, emby)
        stack.members.set_status('u1', 'suspended')
        first = asyncio.create_task(service.enforce_now('u1'))
        await started.wait()
        stack.members.set_status('u1', 'active')
        second = asyncio.create_task(service.enforce_now('u1'))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        assert emby.policy['IsDisabled'] is False
    asyncio.run(run())


def test_delete_rechecks_admin_after_remote_listing(bot):
    bot._members.set_roles('u1', ['admin'])
    bot._members.bind_telegram('u1', '12')
    bot._members.upsert('other', 'other', {'group_id': 'standard'})
    async def users():
        bot._members.set_roles('u1', [])
        return [{'Id': 'other'}]
    bot._emby.list_users = users
    async def run():
        await bot._handle_command(12, '12', 'operator', '/rm other')
        saved = bot._pending['12'][2]
        await bot._handle_callback(callback('rm_self:' + saved['nonce']))
    asyncio.run(run())
    assert bot._members.get('other')
    assert not bot._emby.deleted


def test_rebind_notice_alias_retry_does_not_duplicate(bot):
    bot._cfg()['group_interaction_chats'] = ['@reviewgroup']
    bot._members.bind_telegram('u1', '20')
    row = bot._rebinding.create('u1', '30', 'guest')
    sends = []
    async def call(method, payload=None, timeout=20):
        if method == 'getChat':
            return {'id': -1007}
        if method == 'sendMessage':
            sends.append(payload)
            return {'message_id': 90, 'chat': {'id': -1007}}
        return True
    bot._call = call
    async def run():
        assert await bot._publish_rebind(row) == 1
        assert await bot._publish_rebind(row) == 1
    asyncio.run(run())
    assert len(sends) == 1


def test_unknown_shop_failure_is_not_echoed_to_member(bot):
    bot._members.bind_telegram('u1', '12')
    item = bot._shop.create({'kind': 'invite', 'name': 'Slot', 'cost': 10, 'amount': 1})
    def fail(*args, **kwargs):
        raise RuntimeError('unlabelled-example-private-value')
    bot._shop.redeem = fail
    async def run():
        await bot._handle_callback(callback('buy:' + str(item['id'])))
        await bot._handle_callback(callback('buyok:' + str(item['id'])))
    asyncio.run(run())
    assert 'unlabelled-example-private-value' not in str(bot.calls)
