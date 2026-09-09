"""Audit actual bot workflows using SQLite and locally recorded transports."""
import asyncio
import time

import pytest
from test_audit_accounts_invariants import stack as account_stack  # noqa: F401

from app.modules.registration import RegistrationService
from app.modules.telegram import TelegramBot


@pytest.fixture
def stack(request):
    return request.getfixturevalue("account_stack")


class Emby:
    def __init__(self):
        self.users = {}
        self.password_ok = True
        self.deleted = []
    async def create_user(self, name):
        await asyncio.sleep(0)
        row = {'Id': 'new-' + name, 'Name': name}
        self.users[row['Id']] = row
        return row
    async def set_user_password(self, uid, password):
        await asyncio.sleep(0)
        return self.password_ok
    async def delete_user(self, uid):
        self.deleted.append(uid)
        self.users.pop(uid, None)
        return True
    async def list_users(self):
        return list(self.users.values())


@pytest.fixture
def bot(stack):
    cfg = {'enabled': True, 'bot_token': '123' + ':test-only', 'register_days': 30,
           'default_group_id': 'standard', 'group_interaction_chats': ['-1007']}
    reg = RegistrationService(stack.db, stack.groups, lambda: cfg)
    b = TelegramBot(lambda: cfg, stack.members, Emby(), db=stack.db, registration=reg,
                    groups=stack.groups, shop=stack.shop, points=stack.points)
    b.calls = []
    async def call(method, payload=None, timeout=20):
        b.calls.append((method, payload or {}))
        if method in ('sendMessage', 'editMessageText'):
            return {'message_id': (payload or {}).get('message_id', 80)}
        return True
    b._call = call
    return b


def callback(data, user=12, mid=80):
    return {'id': 'cb', 'data': data, 'from': {'id': user},
            'message': {'chat': {'id': user, 'type': 'private'}, 'message_id': mid}}


def test_registration_password_failure_does_not_issue_unusable_account(bot):
    invite = bot._registration.issue_invite('u1')
    admission = bot._registration.resolve('12', invite['code'])
    bot._emby.password_ok = False
    asyncio.run(bot._finish_registration(12, '12', 'guest', 'guestname', admission))
    assert bot._member_for_chat('12') is None
    assert not bot._emby.users
    assert bot._registration.get_invite(invite['code'])['uses_left'] == 1
    assert '注册成功' not in str(bot.calls)


def test_registration_single_use_code_race_creates_only_one_member(bot):
    invite = bot._registration.issue_invite('u1')
    a = bot._registration.resolve('12', invite['code'])
    b = bot._registration.resolve('13', invite['code'])
    async def run():
        await asyncio.gather(bot._finish_registration(12, '12', 'guest', 'guestone', a),
                             bot._finish_registration(13, '13', 'guest', 'guesttwo', b))
    asyncio.run(run())
    assert sum(bot._member_for_chat(tg) is not None for tg in ('12', '13')) == 1
    assert len(bot._emby.users) == 1


def test_registration_revoked_admission_rechecked(bot):
    invite = bot._registration.issue_invite('u1')
    admission = bot._registration.resolve('12', invite['code'])
    bot._registration.revoke_invite(invite['code'])
    asyncio.run(bot._finish_registration(12, '12', 'guest', 'guestname', admission))
    assert not bot._emby.users
    assert bot._member_for_chat('12') is None


def test_registration_cap_race(bot):
    bot._cfg()['max_users'] = 2  # includes the existing fixture member
    bot._registration.grant_admin('12')
    bot._registration.grant_admin('13')
    async def run():
        await asyncio.gather(*[bot._finish_registration(int(tg), tg, 'guest', name,
                 bot._registration.resolve(tg)) for tg, name in [('12', 'guestone'), ('13', 'guesttwo')]])
    asyncio.run(run())
    assert len(bot._members.list()) == 2
    assert len(bot._emby.users) == 1


def test_expired_request_confirmation_cannot_submit(bot):
    class Requests:
        async def create(self, *args):
            raise AssertionError('expired request was submitted')
    bot._requests = Requests()
    bot._members.bind_telegram('u1', '12')
    bot._pending['12'] = ('request_confirm', time.time() - 1, {'tmdb_id': 101})
    asyncio.run(bot._handle_callback(callback('req_ok')))


def test_purchase_confirmation_is_single_use(bot):
    bot._members.bind_telegram('u1', '12')
    item = bot._shop.create({'kind': 'invite', 'name': 'Slot', 'cost': 10, 'amount': 1})
    async def run():
        await bot._handle_callback(callback('buy:' + str(item['id'])))
        await bot._handle_callback(callback('buyok:' + str(item['id'])))
        await bot._handle_callback(callback('buyok:' + str(item['id'])))
    asyncio.run(run())
    assert len(bot._shop.orders('u1')) == 1
    assert bot._points.balance('u1') == 990


def test_guest_name_is_literal_not_html(bot):
    assert '<b>injected</b>' not in bot._guest_home('<b>injected</b>')
    assert '&lt;b&gt;' in bot._guest_home('<b>injected</b>')


def test_restricted_nonmember_does_not_pass_group_gate(bot):
    bot._cfg()['require_group'] = '-1007'
    async def call(*args, **kwargs):
        return {'status': 'restricted', 'is_member': False}
    bot._call = call
    assert asyncio.run(bot.in_required_group('12'))[0] is False


def test_delete_cascade_uses_original_preview_ids(bot):
    m = bot._members
    m.upsert('admin', 'operator', {'group_id': 'standard', 'roles': ['admin']})
    m.bind_telegram('admin', '12')
    m.upsert('parent', 'parent', {'group_id': 'standard'})
    m.upsert('other', 'other', {'group_id': 'standard'})
    m.upsert('child', 'child', {'group_id': 'standard', 'inviter_id': 'parent'})
    async def run():
        await bot._handle_command(12, '12', 'operator', '/rm child')
        saved = bot._pending['12'][2]
        bot._db.execute("UPDATE members SET inviter_id='other' WHERE emby_user_id='child'")
        await bot._handle_callback(callback('rm_cascade:' + saved['nonce']))
    asyncio.run(run())
    assert bot._emby.deleted == []
    assert m.get('child') and m.get('other')


def test_failed_delete_never_says_deleted(bot):
    bot._members.set_roles('u1', ['admin'])
    bot._members.bind_telegram('u1', '12')
    bot._members.upsert('other', 'other', {'group_id': 'standard'})
    bot._emby.users['other'] = {'Id': 'other'}
    async def fail(uid):
        return False
    bot._emby.delete_user = fail
    async def run():
        await bot._handle_command(12, '12', 'operator', '/rm other')
        saved = bot._pending['12'][2]
        await bot._handle_callback(callback('rm_self:' + saved['nonce']))
    asyncio.run(run())
    assert bot._members.get('other')
    assert '已删除 <b>other' not in str(bot.calls)


def test_rebind_rechecks_same_admin_identity_after_await(bot):
    bot._members.bind_telegram('u1', '20')
    for uid, tg in [('admin', '12'), ('otheradmin', '13')]:
        bot._members.upsert(uid, uid, {'group_id': 'standard', 'roles': ['admin']})
        bot._members.bind_telegram(uid, tg)
    row = bot._rebinding.create('u1', '30', 'new')
    async def users():
        bot._members.bind_telegram('otheradmin', '12')
        return [{'Id': 'u1'}]
    bot._emby.list_users = users
    with pytest.raises(ValueError):
        asyncio.run(bot.review_rebind(row['id'], True, 'test', reviewer_tg='12'))
    assert bot._members.get('u1')['tg_user_id'] == '20'


def test_registration_cancellation_cleans_remote_account(bot):
    bot._registration.grant_admin('12')
    async def run():
        started = asyncio.Event()
        release = asyncio.Event()
        original = bot._emby.create_user
        async def create(name):
            result = await original(name)
            started.set()
            await release.wait()
            return result
        bot._emby.create_user = create
        task = asyncio.create_task(bot._finish_registration(12, '12', 'guest', 'guestname',
                                       bot._registration.resolve('12')))
        await started.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert not bot._emby.users
    assert bot._member_for_chat('12') is None
    assert not bot._registration.get_grant('12')['used_at']


def test_request_and_shop_render_external_text_as_literal(bot):
    bot._members.bind_telegram('u1', '12')
    bot._shop.create({'kind': 'invite', 'name': '<b>external</b>',
                     'description': '<a href="https://invalid.example">text</a>',
                     'cost': 10, 'amount': 1})
    asyncio.run(bot._shop_view(12, 80, bot._members.get('u1')))
    text = bot.calls[-1][1]['text']
    assert '<b>external</b>' not in text
    assert '&lt;b&gt;external' in text


def test_bulk_confirmation_does_not_expand_to_new_members(bot):
    bot._members.set_roles('u1', ['admin'])
    bot._members.bind_telegram('u1', '12')
    async def run():
        await bot._handle_command(12, '12', 'operator', '/scoreall 10')
        bot._members.upsert('later', 'later', {'group_id': 'standard'})
        await bot._handle_callback(callback('admin_ok'))
    asyncio.run(run())
    assert bot._points.balance('later') == 0
    assert bot._points.balance('u1') == 1010


def test_unavailable_group_audit_reports_uncertainty(bot):
    bot._cfg()['require_group'] = '-1007'
    bot._members.bind_telegram('u1', '12')
    async def call(*args, **kwargs):
        return None
    bot._call = call
    result = asyncio.run(bot.audit_group_membership())
    assert result['unavailable'] is True


def test_rebind_group_removed_while_verifying_reviewer(bot):
    bot._members.bind_telegram('u1', '20')
    bot._members.upsert('admin', 'operator', {'group_id': 'standard', 'roles': ['admin']})
    bot._members.bind_telegram('admin', '12')
    row = bot._rebinding.create('u1', '30', 'guest')
    async def users():
        bot._cfg()['group_interaction_chats'] = []
        return [{'Id': 'u1'}]
    bot._emby.list_users = users
    with pytest.raises(ValueError):
        asyncio.run(bot.review_rebind(row['id'], True, 'test', reviewer_tg='12'))
    assert bot._members.get('u1')['tg_user_id'] == '20'


def test_poll_failure_triggers_backoff_not_hot_loop(bot):
    async def call(*args, **kwargs):
        return None
    bot._call = call
    with pytest.raises(RuntimeError):
        asyncio.run(bot._poll_once())
