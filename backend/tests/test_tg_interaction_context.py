"""Real local Bot methods + temporary services, Telegram represented in memory."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from app.adapters.mock import MockEmby
from app.core.db import Database
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.points import PointsService
from app.modules.registration import RegistrationService
from app.modules.requests import RequestService
from app.modules.shop import ShopService
from app.modules.telegram import TelegramBot

GROUP = -100700
ADMIN = 901
SECOND = 902
VIEWER = 903


class FakeTelegram:
    def __init__(self):
        self.messages = {}
        self.calls = []
        self.next_id = 100
        self.edit_error = ''
        self.fail_send = False
        self.bot = None

    async def call(self, method, payload=None, timeout=20):
        payload = copy.deepcopy(payload or {})
        self.calls.append((method, payload))
        if method in ('sendMessage', 'sendPhoto'):
            if self.fail_send:
                return None
            self.next_id += 1
            self.messages[(str(payload['chat_id']), self.next_id)] = payload
            return {'message_id': self.next_id, 'chat': {'id': payload['chat_id']}}
        if method in ('editMessageText', 'editMessageReplyMarkup'):
            if self.edit_error and method == 'editMessageText':
                self.bot._last_error = self.edit_error
                return None
            key = (str(payload['chat_id']), payload['message_id'])
            self.messages.setdefault(key, {}).update(payload)
            return {'message_id': payload['message_id']}
        return True

    def message(self, chat, mid):
        return self.messages[(str(chat), mid)]

    def text(self, chat, mid):
        row = self.message(chat, mid)
        return row.get('text') or row.get('caption') or ''

    def actions(self, chat, mid):
        return [b for row in self.message(chat, mid).get('reply_markup', {}).get('inline_keyboard', []) for b in row]


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / 'interaction.db')
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    for uid, name, tg, roles in [('admin', 'Operator', ADMIN, ['admin']),
                                ('admin2', 'SecondAdmin', SECOND, ['admin']),
                                ('u1', 'ViewerA', VIEWER, []), ('u2', 'ViewerB', 904, [])]:
        members.upsert(uid, name, {'group_id': 'standard', 'roles': roles})
        members.bind_telegram(uid, str(tg))
    points = PointsService(db)
    points.add('u1', 50, 'admin.adjust')
    cfg = {'bot_token': '123' + ':local-only', 'enabled': True,
           'group_interaction_chats': [str(GROUP)], 'default_group_id': 'standard'}
    stats = SimpleNamespace(watch_summary=lambda uid: {'seconds_24h': 600,
        'seconds_30d': 7200, 'recorded_seconds': 36000, 'first_at': 1_780_000_000})
    bot = TelegramBot(lambda: cfg, members, MockEmby(), db=db, groups=groups,
                      points=points, shop=ShopService(db, members, points), stats=stats,
                      registration=RegistrationService(db, groups, lambda: cfg),
                      requests=RequestService(db, members, groups))
    bot._bot_username = 'MediaDeckDemoBot'
    tg = FakeTelegram()
    tg.bot = bot
    bot._call = tg.call
    yield SimpleNamespace(bot=bot, tg=tg, db=db, members=members, cfg=cfg, groups=groups)
    db.close()


async def command(env, text, chat=GROUP, user=ADMIN, reply=None, thread=None):
    message = {'chat': {'id': chat, 'type': 'supergroup' if chat < 0 else 'private'},
               'from': {'id': user, 'username': 'local_user'}, 'message_id': 20, 'text': text}
    if reply:
        message['reply_to_message'] = {'message_id': reply, 'from': {'is_bot': True, 'id': 123}}
    if thread:
        message['message_thread_id'] = thread
    await env.bot._dispatch_update({'message': message})
    key = env.bot._session_key(chat, user, group=chat < 0, thread_id=thread)
    return env.bot._panel.get(key)


async def click(env, action, mid, chat=GROUP, user=ADMIN, thread=None):
    message = {'chat': {'id': chat, 'type': 'supergroup' if chat < 0 else 'private'}, 'message_id': mid}
    if thread:
        message['message_thread_id'] = thread
    await env.bot._dispatch_update({'callback_query': {'id': 'callback', 'data': action,
                    'from': {'id': user}, 'message': message}})
    key = env.bot._session_key(chat, user, group=chat < 0, thread_id=thread)
    return env.bot._panel.get(key)


def test_group_kk_renew_cancel_returns_target_not_personal_home(env):
    async def run():
        mid = await command(env, '/kk ViewerA')
        await click(env, 'admin_renew', mid)
        back = next(b['callback_data'] for b in env.tg.actions(GROUP, mid) if '返回' in b['text'] or '取消' in b['text'])
        await click(env, back, mid)
        text = env.tg.text(GROUP, mid)
        assert 'ViewerA' in text and '欢迎回来' not in text
        assert 'admin_card' in str(env.tg.actions(GROUP, mid))
    asyncio.run(run())


def test_old_target_card_never_acts_on_last_queried_target(env):
    before_a = env.members.get('u1')['expires_at_effective']
    before_b = env.members.get('u2')['expires_at_effective']
    async def run():
        a = await command(env, '/kk ViewerA')
        b = await command(env, '/kk ViewerB')
        assert a != b
        await click(env, 'admin_renew', a)
        await command(env, '7', reply=a)
        assert 'ViewerA' in env.tg.text(GROUP, a)
    asyncio.run(run())
    assert env.members.get('u1')['expires_at_effective'] == before_a + 7 * 86400
    assert env.members.get('u2')['expires_at_effective'] == before_b


def test_group_me_has_useful_public_data_and_no_private_fields(env):
    env.members.upsert('u1', 'ViewerA', {'group_id': 'whitelist', 'note': 'private-note', 'contact': 'private-contact'})
    mid = asyncio.run(command(env, '/me', user=VIEWER))
    text = env.tg.text(GROUP, mid)
    for expected in ('ViewerA', '白名单', '有效期', '积分', '50', '近24小时', '来源'):
        assert expected in text
    for forbidden in ('private-note', 'private-contact', '邀请码', '密码：', 'device_id', 'token'):
        assert forbidden not in text
    assert any(b.get('url', '').startswith('https://t.me/') for b in env.tg.actions(GROUP, mid))


def test_group_personal_subpage_cannot_leak_private_note(env):
    env.members.upsert('u1', 'ViewerA', {'note': 'private-note'})
    async def run():
        mid = await command(env, '/me', user=VIEWER)
        await click(env, 'me_status', mid, user=VIEWER)
        assert 'private-note' not in env.tg.text(GROUP, mid)
    asyncio.run(run())


def test_direct_edit_falls_back_for_deleted_message_and_preserves_target(env):
    async def run():
        mid = await command(env, '/kk ViewerA')
        env.tg.edit_error = 'Bad Request: message to edit not found'
        new = await click(env, 'admin_usage', mid)
        assert new != mid
        assert 'ViewerA' in env.tg.text(GROUP, new)
    asyncio.run(run())


def test_ambiguous_network_edit_failure_does_not_send_duplicate(env):
    async def run():
        mid = await command(env, '/kk ViewerA')
        env.tg.calls.clear()
        env.tg.edit_error = 'ReadTimeout: 请求失败'
        # Use the actual bound context for a navigation refresh.
        with env.bot._bind_session(GROUP, ADMIN, group=True):
            env.tg.calls.clear()
            await env.bot._show(GROUP, 'updated', [[{'text': 'back', 'callback_data': 'home'}]])
        assert not any(method == 'sendMessage' for method, _ in env.tg.calls)
        assert mid
    asyncio.run(run())
