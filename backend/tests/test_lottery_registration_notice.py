"""Actual lottery -> private Bot registration -> existing durable group receipt.

All accounts, messages, credentials and transports are isolated test fixtures.
"""
import asyncio
import copy
import time

import pytest

from app.modules.gift_receipts import GiftReceipts
from tests.test_bot_password_choices import Emby
from tests.test_lottery import CHAT, GUEST, config, create, draw, join, req
from tests.test_lottery import env as lottery_env  # noqa: F401

USERNAME = 'LotteryNewMember'
PASSWORD = 'Local-Only-Password-42'
OTHER_CHAT = '-1009876543210'


@pytest.fixture
def e(request, monkeypatch):
    env = request.getfixturevalue('lottery_env')
    env.bot = env.service.bot
    env.emby = Emby()
    env.update_id = 1000
    monkeypatch.setattr(env.bot, '_emby', env.emby)
    assert env.client.post('/api/settings/telegram', json={
        'registration_notify_chat_id': CHAT, 'registration_notify_thread_id': 77,
        'group_interaction_chats': [CHAT, OTHER_CHAT]}).status_code == 200
    env.lid = create(env, config(prizes=[{'kind': 'registration', 'name': '本人资格',
                     'quantity': 1, 'group_id': 'standard', 'days': 10}]))
    join(env, env.lid)
    draw(env, env.lid)
    env.code = req(env, 'GET', '/' + env.lid, GUEST).json()['my_award']['registration_url'].split('start=')[1]
    env.frozen = frozen(env)
    return env


def frozen(e):
    return {table: e.service.db.query(f'SELECT * FROM {table} WHERE ' +
            ('id' if table == 'lotteries' else 'lottery_id') + '=?', (e.lid,))
            for table in ('lotteries', 'lottery_awards', 'lottery_notices', 'lottery_dms')}


def receipts(e):
    return e.service.db.query('SELECT * FROM tg_gift_receipts ORDER BY id')


def public_posts(e):
    return [p for method, p in e.calls if method == 'sendMessage' and
            str(p.get('chat_id', '')).startswith('-') and '注册成功' in p.get('text', '')]


async def message(e, text):
    e.update_id += 1
    await e.bot._dispatch_update({'update_id': e.update_id, 'message': {
        'message_id': e.update_id, 'date': int(time.time()), 'chat': {'id': GUEST, 'type': 'private'},
        'from': {'id': GUEST, 'is_bot': False, 'first_name': 'Winner <&>'}, 'text': text}})


async def callback(e, data, message_id):
    e.update_id += 1
    await e.bot._dispatch_update({'update_id': e.update_id, 'callback_query': {
        'id': str(e.update_id), 'data': data, 'from': {'id': GUEST, 'is_bot': False},
        'message': {'chat': {'id': GUEST, 'type': 'private'}, 'message_id': message_id}}})


async def register(e):
    await message(e, '/start ' + e.code)
    assert e.bot._pending[str(GUEST)][0] == 'username'
    await message(e, USERNAME)
    choice = e.bot._pending[str(GUEST)][2]
    await callback(e, f'pw:{choice["nonce"]}:custom', choice['message_id'])
    await message(e, PASSWORD)
    confirmation = e.bot._pending[str(GUEST)][2]
    assert not e.emby.created and not receipts(e) and not public_posts(e)
    data = f'pw:{confirmation["nonce"]}:confirm'
    await callback(e, data, confirmation['message_id'])
    return data, confirmation['message_id']


async def repeat(e, confirmation):
    await callback(e, *confirmation)
    await message(e, '/start ' + e.code)
    await message(e, USERNAME)
    await e.bot._gift_receipts.recover()


def assert_registered_once(e):
    member = e.service.members.find_by_telegram(str(GUEST))
    assert member and member['emby_user_id'] == 'new-' + USERNAME
    assert member['group_id'] == 'standard' and abs(member['expires_at'] - time.time() - 10 * 86400) < 10
    admission = e.service.db.one('SELECT used_by,used_at FROM lottery_admissions WHERE code=?', (e.code,))
    assert admission['used_by'] == member['emby_user_id'] and admission['used_at']
    assert e.emby.created == [USERNAME] and e.emby.passwords == [(member['emby_user_id'], PASSWORD)]
    assert not e.emby.deleted and frozen(e) == e.frozen


@pytest.mark.parametrize('target', [CHAT, OTHER_CHAT])
@pytest.mark.parametrize('topic', [None, 77])
def test_lottery_registration_uses_configured_target_once_without_login_details(e, target, topic):
    assert e.client.post('/api/settings/telegram', json={
        'registration_notify_chat_id': target, 'registration_notify_thread_id': topic}).status_code == 200

    async def run():
        confirmation = await register(e)
        assert_registered_once(e)
        rows, posts = receipts(e), public_posts(e)
        assert len(rows) == len(posts) == 1
        assert rows[0]['status'] == 'sent' and rows[0]['attempts'] == 1 and rows[0]['message_id']
        body = posts[0]['text']
        assert posts[0]['chat_id'] == target and posts[0].get('message_thread_id') == topic
        assert f'<a href="tg://user?id={GUEST}">Winner &lt;&amp;&gt;</a> 注册成功' in body
        assert '到期' in body and '普通用户' in body
        assert 'reply_to_message_id' not in posts[0] and 'reply_markup' not in posts[0]
        for secret in (USERNAME, PASSWORD, e.code, '账号：', '密码', '领取码'):
            assert secret not in body and secret not in rows[0]['body']
        await repeat(e, confirmation)
        assert_registered_once(e)
        assert receipts(e) == rows and public_posts(e) == posts
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['create', 'password', 'consume', 'staging'])
def test_failed_lottery_registration_does_not_consume_or_announce(e, monkeypatch, failure):
    async def no_create(username):
        e.emby.created.append(username)
    if failure == 'create':
        monkeypatch.setattr(e.emby, 'create_user', no_create)
    elif failure == 'password':
        e.emby.fail = True
    elif failure == 'consume':
        monkeypatch.setattr(e.bot._registration, 'consume', lambda *a, **kw: False)
    else:
        original = e.bot._gift_receipts.stage
        def fail_after_insert(*a, **kw):
            original(*a, **kw)
            raise OSError('isolated receipt transaction failure')
        monkeypatch.setattr(e.bot._gift_receipts, 'stage', fail_after_insert)

    async def run():
        await register(e)
        assert not e.service.members.find_by_telegram(str(GUEST))
        assert not e.service.db.one('SELECT used_at FROM lottery_admissions WHERE code=?', (e.code,))['used_at']
        assert e.bot._registration.resolve(str(GUEST), e.code).allowed
        assert not receipts(e) and not public_posts(e) and frozen(e) == e.frozen
        assert e.emby.deleted == ([] if failure == 'create' else ['new-' + USERNAME])
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['rejected', 'exception'])
def test_notice_failure_keeps_committed_registration_and_only_retries_receipt(e, monkeypatch, failure):
    original = e.bot._call
    blocked = True

    async def transport(method, payload=None, **kw):
        if method == 'sendMessage' and str((payload or {}).get('chat_id')) == CHAT and '注册成功' in payload.get('text', ''):
            assert_registered_once(e)  # never sends under the registration transaction
            if blocked:
                e.calls.append((method, copy.deepcopy(payload)))
                if failure == 'exception':
                    raise OSError('isolated Telegram transport failure')
                return None
        return await original(method, payload, **kw)
    monkeypatch.setattr(e.bot, '_call', transport)

    async def run():
        nonlocal blocked
        confirmation = await register(e)
        assert_registered_once(e)
        row = receipts(e)[0]
        assert row['status'] == 'failed' and row['attempts'] == 1
        private = [p.get('text', '') for _, p in e.calls if str((p or {}).get('chat_id')) == str(GUEST)]
        assert any('账号注册成功。群回执暂未送达' in text for text in private)
        assert not any('注册未完成' in text for text in private)
        e.bot._gift_receipts = GiftReceipts(e.bot)  # reload only the persisted receipt worker
        await repeat(e, confirmation)
        assert len(public_posts(e)) == 1 and receipts(e)[0] == row
        blocked = False
        retry = f'gift_receipt_retry:{row["id"]}'
        await callback(e, retry, e.bot._panel[str(GUEST)])
        await callback(e, retry, e.bot._panel[str(GUEST)])
        assert receipts(e)[0]['status'] == 'sent' and receipts(e)[0]['attempts'] == 2
        assert len(public_posts(e)) == 2  # one failed attempt, one explicit retry; no repeat on duplicate tap
        assert_registered_once(e)
    asyncio.run(run())


def test_pending_lottery_receipt_recovered_once_without_registration_replay(e, monkeypatch):
    async def defer(*a, **kw):
        return None
    monkeypatch.setattr(e.bot._gift_receipts, 'deliver', defer)

    async def run():
        confirmation = await register(e)
        assert_registered_once(e)
        assert receipts(e)[0]['status'] == 'pending' and not public_posts(e)
        e.bot._gift_receipts = GiftReceipts(e.bot)
        await repeat(e, confirmation)
        await e.bot._gift_receipts.recover()
        assert len(public_posts(e)) == 1 and receipts(e)[0]['status'] == 'sent'
        assert_registered_once(e)
    asyncio.run(run())


def test_unconfigured_notification_target_is_not_guessed_from_lottery(e):
    assert e.client.post('/api/settings/telegram', json={
        'registration_notify_chat_id': '', 'registration_notify_thread_id': None}).status_code == 200

    async def run():
        confirmation = await register(e)
        await repeat(e, confirmation)
        assert_registered_once(e)
        assert not receipts(e) and not public_posts(e)
    asyncio.run(run())


def test_pre_fix_consumed_admission_is_not_backfilled_on_worker_recovery(e, monkeypatch):
    async def run():
        # Explicitly model the old successful flow, which deliberately staged no receipt.
        with monkeypatch.context() as patch:
            patch.setattr(e.bot._gift_receipts, 'stage', lambda *a, **kw: None)
            confirmation = await register(e)
        e.bot._gift_receipts = GiftReceipts(e.bot)
        await repeat(e, confirmation)
        assert_registered_once(e)
        assert not receipts(e) and not public_posts(e)
    asyncio.run(run())
