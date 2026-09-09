"""Reply-thread metadata must not masquerade as a forum-topic authority scope."""
import asyncio
import time

import pytest
from test_tg_interaction_context import ADMIN, GROUP, SECOND, VIEWER
from test_tg_interaction_context import env as context_env  # noqa: F401

TARGET = 955


@pytest.fixture
def env(request):
    return request.getfixturevalue('context_env')


def metadata(thread=None, topic=False):
    result = {}
    if thread is not None:
        result['message_thread_id'] = thread
    if topic:
        result['is_topic_message'] = True
    return result


async def lookup(e, *, chat=GROUP, thread=None, topic=False, target=TARGET):
    await e.bot._dispatch_update({'message': {
        'chat': {'id': chat, 'type': 'supergroup' if chat < 0 else 'private'},
        'from': {'id': ADMIN}, 'message_id': 20, 'text': '/kk',
        'reply_to_message': {'message_id': 17, 'from': {'id': target, 'is_bot': False}},
        **metadata(thread, topic)}})
    panels = [(mid, p) for (cid, mid), p in e.bot._admin_panels.items() if cid == str(chat)]
    assert panels
    return panels[-1]


async def tap(e, mid, data='admin_gift', *, chat=GROUP, user=ADMIN, thread=None, topic=False):
    await e.bot._dispatch_update({'callback_query': {
        'id': 'permission-hotfix', 'data': data, 'from': {'id': user},
        'message': {'chat': {'id': chat, 'type': 'supergroup' if chat < 0 else 'private'},
                    'message_id': mid, **metadata(thread, topic)}}})


def panel_scope(e, chat, mid):
    return e.bot._admin_panels[(str(chat), mid)]['session']


def confirmation(e, chat, mid):
    return next((b['callback_data'] for b in e.tg.actions(chat, mid)
                 if b.get('callback_data', '').startswith('admin_gift_ok:')), None)


@pytest.mark.parametrize('command_thread,callback_thread', [(17, None), (None, 101), (17, 101)])
def test_non_topic_reply_metadata_does_not_change_admin_identity(env, command_thread, callback_thread):
    async def run():
        mid, panel = await lookup(env, thread=command_thread)
        reviewer = env.members.find_by_telegram(str(ADMIN))
        assert panel['owner'] == str(ADMIN)
        assert panel['reviewer_id'] == reviewer['emby_user_id']
        assert env.bot.is_admin(reviewer) and panel['user_id'] == ''
        env.tg.calls.clear()
        await tap(env, mid, thread=callback_thread)
        # Before the fix all other authority predicates stay true, but the
        # per-update reply thread changes _SESSION and trips the exact guard.
        assert not any('管理员身份或权限已变化' in p.get('text', '') for _, p in env.tg.calls)
        action = confirmation(env, GROUP, mid)
        assert action, env.tg.calls
        assert env.bot._registration.get_grant(str(TARGET)) is None
        await tap(env, mid, action, thread=callback_thread)
        gift = env.bot._registration.get_grant(str(TARGET))
        assert gift and not gift['used_at']
        assert '赠送了一份注册资格' in env.tg.text(GROUP, mid)
        assert len(env.tg.actions(GROUP, mid)) == 1
        assert panel['session'] == env.bot._session_key(GROUP, ADMIN, group=True)
        assert all('message_thread_id' not in p for method, p in env.tg.calls if method == 'sendMessage')
        await tap(env, mid, action, thread=callback_thread)
        assert env.bot._registration.get_grant(str(TARGET))['gift_code'] == gift['gift_code']
        assert len(env.bot._registration.list_grants()) == 1
        assert env.db.one("SELECT COUNT(*) AS n FROM audit_log WHERE action='registration.gift'")['n'] == 1
    asyncio.run(run())


@pytest.mark.parametrize('chat,topic,thread', [(GROUP, False, None), (GROUP, True, 77), (ADMIN, False, None)])
def test_normal_group_real_forum_topic_and_private_gift_still_work(env, chat, topic, thread):
    async def run():
        mid, _ = await lookup(env, chat=chat, topic=topic, thread=thread)
        await tap(env, mid, chat=chat, topic=topic, thread=thread)
        action = confirmation(env, chat, mid)
        assert action
        await tap(env, mid, action, chat=chat, topic=topic, thread=thread)
        assert env.bot._registration.get_grant(str(TARGET))
        if topic:
            assert panel_scope(env, chat, mid).split(':')[-2] == str(thread)
            assert any(p.get('message_thread_id') == thread for method, p in env.tg.calls if method == 'sendMessage')
    asyncio.run(run())


@pytest.mark.parametrize('case', ['demoted', 'unbound', 'rebound_admin', 'other_admin', 'ordinary',
                                  'wrong_group', 'wrong_message', 'expired', 'wrong_nonce'])
def test_reply_metadata_fix_preserves_confirmation_authority(env, case):
    async def run():
        mid, panel = await lookup(env, thread=17)
        await tap(env, mid, thread=17)
        action = confirmation(env, GROUP, mid)
        assert action
        user, chat, message = ADMIN, GROUP, mid
        if case == 'demoted':
            env.members.upsert('admin', 'Operator', {'roles': []})
        elif case in ('unbound', 'rebound_admin'):
            env.members.unbind_telegram('admin')
            if case == 'rebound_admin':
                env.members.upsert('replacement', 'Replacement', {'roles': ['admin'], 'group_id': 'standard'})
                env.members.bind_telegram('replacement', str(ADMIN))
        elif case == 'other_admin':
            user = SECOND
        elif case == 'ordinary':
            user = VIEWER
        elif case == 'wrong_group':
            chat = GROUP - 1
            env.cfg['group_interaction_chats'].append(str(chat))  # even another allowed group cannot borrow a card
        elif case == 'wrong_message':
            message += 1000
        elif case == 'expired':
            panel['expires'] = time.time() - 1
        else:
            action = 'admin_gift_ok:forged'
        await tap(env, message, action, chat=chat, user=user, thread=17)
        assert not env.bot._registration.get_grant(str(TARGET))
    asyncio.run(run())


def test_reply_metadata_does_not_allow_cross_card_nonce_or_target(env):
    async def run():
        a, _ = await lookup(env, thread=17, target=TARGET)
        b, _ = await lookup(env, thread=18, target=TARGET + 1)
        await tap(env, a, 'admin_card', thread=101)  # reopen the earlier target, not a cancelled draft
        await tap(env, a, thread=101)
        nonce_a = confirmation(env, GROUP, a)
        await tap(env, b, thread=102)
        nonce_b = confirmation(env, GROUP, b)
        await tap(env, a, nonce_b, thread=101)
        assert not env.bot._registration.list_grants()
        await tap(env, a, nonce_a, thread=101)
        assert env.bot._registration.get_grant(str(TARGET))
        assert not env.bot._registration.get_grant(str(TARGET + 1))
    asyncio.run(run())


def test_registered_target_uses_same_reply_scope_without_losing_authority(env):
    async def run():
        mid, panel = await lookup(env, thread=17, target=VIEWER)
        assert panel['user_id'] == 'u1'
        env.tg.calls.clear()
        await tap(env, mid, 'admin_usage', thread=101)
        assert not any('管理员身份或权限已变化' in p.get('text', '') for _, p in env.tg.calls)
        assert any(method == 'editMessageText' for method, _ in env.tg.calls)
        assert not env.bot._registration.list_grants()
    asyncio.run(run())


@pytest.mark.parametrize('callback_thread,topic', [(78, True), (None, False), (77, False)])
def test_real_forum_topic_mismatch_cannot_borrow_gift_panel(env, callback_thread, topic):
    async def run():
        mid, _ = await lookup(env, thread=77, topic=True)
        await tap(env, mid, thread=callback_thread, topic=topic)
        assert confirmation(env, GROUP, mid) is None
        assert not env.bot._registration.get_grant(str(TARGET))
        assert any('管理员身份或权限已变化' in p.get('text', '') for _, p in env.tg.calls)
    asyncio.run(run())
