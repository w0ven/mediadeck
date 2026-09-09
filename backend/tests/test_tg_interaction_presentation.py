"""Confirmed card/announcement layout and scoped trigger-command cleanup."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_tg_interaction_context import ADMIN, GROUP, VIEWER, click, command
from test_tg_interaction_context import env as context_env  # noqa: F401

from app.modules.bot_views import POEMS
from app.modules.telegram import TelegramBot


@pytest.fixture
def env(request):
    return request.getfixturevalue('context_env')


async def dispatch(env, text, chat=GROUP, user=ADMIN, mid=31, reply=None):
    message = {'chat': {'id': chat, 'type': 'supergroup' if chat < 0 else 'private'},
               'from': {'id': user}, 'text': text}
    if mid is not None:
        message['message_id'] = mid
    if reply:
        message['reply_to_message'] = {'message_id': 9, 'from': {'id': reply}}
    await env.bot._dispatch_update({'message': message})


def deletions(env):
    return [p for m, p in env.tg.calls if m == 'deleteMessage']


@pytest.mark.parametrize('chat', [GROUP, ADMIN])
@pytest.mark.parametrize('text,reply,target', [('/me', None, 'Operator'),
    ('/kk ViewerA', None, 'ViewerA'), ('/kk@MediaDeckDemoBot', VIEWER, 'ViewerA'),
    ('/me@MediaDeckDemoBot', None, 'Operator')])
def test_successful_cards_delete_only_trigger_after_send(env, chat, text, reply, target):
    asyncio.run(dispatch(env, text, chat=chat, reply=reply))
    sent = [p for m, p in env.tg.calls if m == 'sendMessage']
    assert len(sent) == 1 and target in sent[0]['text']
    assert deletions(env) == [{'chat_id': chat, 'message_id': 31}]
    methods = [m for m, _ in env.tg.calls]
    assert methods.index('deleteMessage') > methods.index('sendMessage')
    assert any(k[1] != 31 for k in env.tg.messages)


@pytest.mark.parametrize('text', ['/start', '/usage', '/help', '/prouser ViewerA', '/myinfo'])
def test_other_commands_never_delete_the_trigger(env, text):
    asyncio.run(dispatch(env, text))
    assert not deletions(env)


@pytest.mark.parametrize('case', ['send_failed', 'usage', 'unknown', 'unregistered_id',
    'unauthorized', 'wrong_group', 'other_bot', 'no_message_id', 'me_unbound', 'extra_kk_args', 'extra_me_args'])
def test_no_cleanup_without_requested_card_success(env, case):
    text, chat, user, mid = '/kk ViewerA', GROUP, ADMIN, 31
    if case == 'send_failed':
        env.tg.fail_send = True
    elif case == 'usage':
        text = '/kk'
    elif case == 'unknown':
        text = '/kk Absent'
    elif case == 'unregistered_id':
        text = '/kk 9999'
    elif case == 'unauthorized':
        user = VIEWER
    elif case == 'wrong_group':
        chat = -100701
    elif case == 'other_bot':
        text = '/kk@AnotherBot ViewerA'
    elif case == 'no_message_id':
        mid = None
    elif case == 'me_unbound':
        text, user = '/me', 9999
    elif case == 'extra_kk_args':
        text = '/kk ViewerA unexpected'
    else:
        text = '/me unexpected'
    asyncio.run(dispatch(env, text, chat=chat, user=user, mid=mid))
    assert not deletions(env)


@pytest.mark.parametrize('chat', [GROUP, ADMIN])
@pytest.mark.parametrize('failure', ['forbidden', 'already_deleted', 'timeout'])
def test_cleanup_failure_is_quiet_and_preserves_unrelated_error(env, monkeypatch, failure, chat):
    # Exercise the real _call error recorder for deletion, not a fake that
    # unconditionally writes shared status regardless of quiet-call scope.
    original = env.bot._call
    async def post(*args, **kwargs):
        if failure == 'timeout':
            raise httpx.ReadTimeout('local timeout')
        return SimpleNamespace(json=lambda: {'ok': False, 'description':
            'Forbidden: not enough rights' if failure == 'forbidden' else 'Bad Request: message to delete not found'})
    monkeypatch.setattr(env.bot, '_client', AsyncMock(return_value=SimpleNamespace(post=post)))
    async def transport(method, payload=None, timeout=20):
        if method == 'deleteMessage':
            env.tg.calls.append((method, payload))
            # Simulates another task having recorded an unrelated failure.
            env.bot._last_error = 'unrelated-operation-failed'
            return await TelegramBot._call(env.bot, method, payload, timeout)
        return await original(method, payload, timeout)
    env.bot._call = transport
    asyncio.run(dispatch(env, '/kk ViewerA', chat=chat))
    assert deletions(env) == [{'chat_id': chat, 'message_id': 31}]
    assert len([m for m, _ in env.tg.calls if m == 'sendMessage']) == 1
    assert env.bot._last_error == 'unrelated-operation-failed'
    assert env.members.get('u1')['group_id'] == 'standard'


@pytest.mark.parametrize('chat', [GROUP, ADMIN])
def test_announcement_is_one_bold_poem_and_mentions_no_card_or_buttons(env, chat):
    env.members.upsert('u1', '<b>Viewer</b>', {})
    env.members.upsert('admin', '<i>Operator</i>', {})
    asyncio.run(dispatch(env, '/prouser ' + str(VIEWER), chat=chat))
    messages = [p for m, p in env.tg.calls if m == 'sendMessage']
    assert len(messages) == 1
    text = messages[0]['text']
    assert text.startswith('<b>') and any(text.startswith('<b>' + v + '</b>\n\n') for v, _ in POEMS)
    assert f'<a href="tg://user?id={VIEWER}">&lt;b&gt;Viewer&lt;/b&gt;</a>' in text
    assert f'<a href="tg://user?id={ADMIN}">&lt;i&gt;Operator&lt;/i&gt;</a>' in text
    assert '🎉 恭喜' in text and '签发的 <b>💠 白名单</b>！' in text
    assert 'reply_markup' not in messages[0]
    for extra in ('有效期', '状态', '流量', '管理目标', '权益', '<i>', '《'):
        assert extra not in text
    assert env.members.get('u1')['roles'] == []
    assert len(env.db.query("SELECT * FROM audit_log WHERE action='telegram.prouser'")) == 1


@pytest.mark.parametrize('chat', [GROUP, ADMIN])
@pytest.mark.parametrize('text', ['/kk ViewerA', '/me'])
def test_failed_new_card_never_cleans_trigger_or_existing_card(env, chat, text):
    async def run():
        old = await command(env, '/me', chat=chat)
        env.tg.calls.clear()
        env.tg.fail_send = True
        await dispatch(env, text, chat=chat)
        assert not deletions(env)
        key = env.bot._session_key(chat, ADMIN, group=chat < 0)
        assert env.bot._panel[key] == old
        assert len([m for m, _ in env.tg.calls if m == 'sendMessage']) == 1
    asyncio.run(run())


def test_grant_button_edits_one_announcement_without_regrant_on_replay(env):
    async def run():
        mid = await command(env, '/kk ViewerA')
        env.tg.calls.clear()
        await click(env, 'admin_prouser', mid)
        assert [m for m, _ in env.tg.calls] == ['answerCallbackQuery', 'editMessageText']
        assert not env.tg.actions(GROUP, mid)
        assert '🎉 恭喜' in env.tg.text(GROUP, mid)
        await click(env, 'admin_prouser', mid)
        assert '已在白名单' in env.tg.text(GROUP, mid)
        assert '🎉' not in env.tg.text(GROUP, mid)
        assert not env.tg.actions(GROUP, mid)
        assert len(env.db.query("SELECT * FROM audit_log WHERE action='telegram.prouser'")) == 1
    asyncio.run(run())


def test_unbound_grantee_uses_real_escaped_name_without_invented_mention(env):
    env.members.upsert('unbound', '<unbound>', {'group_id': 'standard'})
    asyncio.run(dispatch(env, '/prouser <unbound>'))
    text = next(p['text'] for m, p in env.tg.calls if m == 'sendMessage')
    assert '恭喜 &lt;unbound&gt;' in text
    assert text.count('tg://user?id=') == 1  # issuer only


@pytest.mark.parametrize('chat', [GROUP, VIEWER])
@pytest.mark.parametrize('used', [None, 0, 128 * 1024**3])
def test_card_has_compact_sections_and_truthful_unknown(env, chat, used):
    class Metering:
        def snapshot(self, uid):
            return {'measured_used_bytes': used, 'as_of': 100, 'coverage': {
                'degraded': True, 'nodes': [{'name': 'hidden-node', 'ok': False}]}}
    env.members.bind_metering(Metering(), cutover=True)
    asyncio.run(dispatch(env, '/me', chat=chat, user=VIEWER))
    text = next(p['text'] for m, p in env.tg.calls if m == 'sendMessage')
    for section in ('📊 <b>本月流量</b>', '⚡ <b>带宽</b>', '🎬 <b>观看记录</b>', '💰 <b>积分：50</b>'):
        assert section in text
    assert '数据不完整' in text
    for forbidden in ('来源', '采集', '更新于', 'hidden-node', '这是本人', '请选择', '《'):
        assert forbidden not in text
    if used is None:
        assert '已用：<b>暂未测得</b>' in text and '剩余：<b>暂无法确认</b>' in text
    elif used == 0:
        assert '已用：<b>0 B</b>' in text and '剩余：<b>1.0 TiB</b>' in text
    else:
        assert '已用：<b>128.0 GiB</b>' in text and '剩余：<b>896.0 GiB</b>' in text


def test_whitelist_poetry_removed_from_all_cards_and_home(env):
    env.members.upsert('u1', 'ViewerA', {'group_id': 'whitelist'})
    async def run():
        mid = await command(env, '/me', chat=VIEWER, user=VIEWER)
        for action in ('home', 'me', 'usage', 'me_status'):
            await click(env, action, mid, chat=VIEWER, user=VIEWER)
        admin = await command(env, '/kk ViewerA', chat=ADMIN)
        for action in ('admin_usage', 'admin_binding', 'admin_card'):
            await click(env, action, admin, chat=ADMIN)
        for method, payload in env.tg.calls:
            if method not in ('sendMessage', 'editMessageText'):
                continue
            assert not any(verse in payload['text'] for verse, _ in POEMS)
        card = env.tg.text(ADMIN, admin)
        assert '管理目标' in card and 'ViewerA' in card and 'Operator' not in card
        assert card.count('白名单') == 1 and '♾ 永久' in card
    asyncio.run(run())
