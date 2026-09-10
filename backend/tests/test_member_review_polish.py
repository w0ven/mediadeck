"""Review regressions: real local services, in-memory Telegram, no external I/O."""
import asyncio
import html
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from test_tg_interaction_context import GROUP, SECOND, click, command
from test_tg_interaction_context import env as base_env  # noqa: F401

from app.main import _invalidate_member_snapshot, _member_emby_snapshot, app
from app.modules.telegram import GROUP_ADMIN_COMMANDS, GROUP_MEMBER_COMMANDS


@pytest.fixture
def env(request):
    return request.getfixturevalue('base_env')


def confirm_action(env, mid):
    return next(b['callback_data'] for b in env.tg.actions(GROUP, mid)
                if b.get('callback_data', '').startswith('admin_ok:'))


@pytest.mark.parametrize('cmd,field,delta', [('/renew ViewerA 7', 'expiry', 7 * 86400),
                                            ('/score ViewerA -7', 'points', -7)])
def test_group_single_confirmation_edits_bot_card_once(env, cmd, field, delta):
    def value():
        return (env.members.get('u1')['expires_at_effective'] if field == 'expiry'
                else env.bot._points.balance('u1'))
    async def run():
        before = value()
        mid = await command(env, cmd, thread=10)
        assert mid != 20 and '确认' in env.tg.text(GROUP, mid)
        assert value() == before
        calls = env.tg.calls
        sent = next(i for i, (m, _) in enumerate(calls) if m == 'sendMessage')
        deleted = next(i for i, (m, p) in enumerate(calls)
                       if m == 'deleteMessage' and p['message_id'] == 20)
        assert deleted > sent
        action = confirm_action(env, mid)
        # Ownership, topic, chat, and nonce all have to match.
        for kw in ({'user': SECOND, 'thread': 10}, {'thread': 11},
                   {'chat': GROUP - 1, 'thread': 10}):
            await click(env, action, mid, **kw)
        await click(env, 'admin_ok:stale', mid, thread=10)
        await click(env, 'admin_ok', mid, thread=10)
        assert value() == before
        env.tg.calls.clear()
        await click(env, action, mid, thread=10)
        result = env.tg.text(GROUP, mid)
        assert '✅' in result and value() == before + delta
        await click(env, action, mid, thread=10)
        assert value() == before + delta and env.tg.text(GROUP, mid) == result
        assert not any(m == 'sendMessage' for m, _ in env.tg.calls)
        assert all(p['message_id'] == mid for m, p in env.tg.calls if m == 'editMessageText')
    asyncio.run(run())


def test_cancel_old_target_and_demotion_cannot_execute(env):
    async def run():
        before = env.bot._points.balance('u1')
        a = await command(env, '/score ViewerA 8')
        action_a = confirm_action(env, a)
        b = await command(env, '/score ViewerB 9')
        action_b = confirm_action(env, b)
        await click(env, action_a, a)
        assert env.bot._points.balance('u1') == before
        await click(env, 'admin_cancel', b)
        await click(env, action_b, b)
        assert env.bot._points.balance('u2') == 0
        c = await command(env, '/score ViewerA 8')
        action_c = confirm_action(env, c)
        env.members.set_roles('admin', [])
        await click(env, action_c, c)
        assert env.bot._points.balance('u1') == before
    asyncio.run(run())


def test_failed_response_never_deletes_command_or_replays_change(env):
    async def run():
        env.tg.fail_send = True
        await command(env, '/renew ViewerA 7')
        assert not any(m == 'deleteMessage' for m, _ in env.tg.calls)
        assert not env.bot._pending
        env.tg.fail_send = False
        mid = await command(env, '/score ViewerA 8')
        action = confirm_action(env, mid)
        before = env.bot._points.balance('u1')
        env.tg.calls.clear()
        env.tg.edit_error = 'ReadTimeout'
        await click(env, action, mid)
        await click(env, action, mid)
        assert env.bot._points.balance('u1') == before + 8
        assert not any(m == 'sendMessage' for m, _ in env.tg.calls)
        assert all(p['message_id'] != 20 for m, p in env.tg.calls if m.startswith('editMessage'))
    asyncio.run(run())


def test_interaction_profiles_persist_and_handle_lookup_does_not_scan(env):
    async def run():
        await env.bot._handle_message({'chat': {'id': GROUP, 'type': 'supergroup'},
            'from': {'id': 903, 'first_name': '观影', 'last_name': '小明', 'username': 'NewHandle'},
            'text': '普通群聊', 'message_id': 24})
        await env.bot._handle_callback({'id': 'c', 'data': 'help',
            'from': {'id': 904, 'first_name': '昵称', 'username': 'SecondHandle'},
            'message': {'chat': {'id': 904, 'type': 'private'}, 'message_id': 30}})
    asyncio.run(run())
    assert env.members.get('u1')['tg_display_name'] == '观影 小明'
    assert env.members.get('u2')['tg_username'] == 'SecondHandle'
    env.members.upsert('collision', 'NewHandle', {'group_id': 'standard'})
    env.members.linked_telegram = lambda: pytest.fail('must use indexed database lookup')
    assert env.bot._find_target('@newhandle')['emby_user_id'] == 'u1'
    assert env.bot._find_target('NewHandle')['emby_user_id'] == 'collision'
    assert env.bot._find_target('903')['emby_user_id'] == 'u1'
    for search in ('@newhandle', '903'):
        assert [m['emby_user_id'] for m in env.members.list(search=search) if m['emby_user_id'] == 'u1'] == ['u1']
    env.bot._remember_tg_user({'id': 903, 'first_name': '新昵称'})
    assert env.members.get('u1')['tg_username'] == ''
    assert env.bot._find_target('@newhandle') is None
    env.bot._remember_tg_user({'id': 903})
    assert env.members.get('u1')['tg_display_name'] == '新昵称'
    env.members.bind_telegram('u1', '999')
    assert env.members.get('u1')['tg_display_name'] == ''


@pytest.mark.parametrize('photo_ok', [True, False])
def test_rank_command_shares_scheduled_poster_pager_and_page_profile_fetch(env, photo_ok):
    rows = [{'tg_user_id': str(1000 + n), 'tg_username': 'fallback',
             'username': 'not-a-telegram-name', 'seconds': 3600,
             'group_id': 'whitelist' if n == 0 else 'standard'} for n in range(23)]
    env.bot._stats = SimpleNamespace(top_users=lambda **kw: [dict(r) for r in rows[:kw.get('limit', 5000)]])
    profile_calls = []
    async def profile(uid):
        profile_calls.append(uid)
        return {'display_name': '昵称<&😀' * 25, 'username': 'known_' + uid}
    env.bot._tg_profile = profile
    env.bot._watch_rank_poster = AsyncMock(return_value=b'poster')
    async def multipart(method, payload, files):
        assert files['photo'][0] == 'watch-rank.jpg'
        return await env.tg.call(method, payload) if photo_ok else None
    env.bot._call_multipart = multipart
    async def run():
        mid = await command(env, '/rank 7', thread=31)
        assert env.bot._watch_rank_poster.await_args.args == (7,)
        assert len(profile_calls) == 10
        body = env.tg.text(GROUP, mid)
        assert body.count('tg://user?id=') == 10
        assert '白名单' in body and 'not-a-telegram-name' not in body
        plain = html.unescape(re.sub('<[^>]+>', '', body))
        assert len(plain.encode('utf-16-le')) // 2 <= 1024
        assert env.tg.message(GROUP, mid)['message_thread_id'] == 31
        env.tg.calls.clear()
        await click(env, 'urank:2_7', mid, thread=31)
        assert len(profile_calls) == 20
        method = 'editMessageCaption' if photo_ok else 'editMessageText'
        page = next(p for m, p in env.tg.calls if m == method)
        assert page['message_id'] == mid
        text = page.get('caption') or page['text']
        assert text.count('tg://user?id=') == 10 and '第11名' in text
        await click(env, 'urank:2_7', mid, thread=31)
        assert len(profile_calls) == 20
        await click(env, 'urank:3_7', mid, thread=31)
        assert len(profile_calls) == 23
        assert not any(m.startswith('send') for m, _ in env.tg.calls)
        await env.bot.broadcast_watch_rank(str(GROUP), 7)
        assert env.bot._watch_rank_poster.await_count == 2
        assert len(profile_calls) == 23
    asyncio.run(run())


def test_profile_lookup_failure_has_short_retry_and_keeps_database_names(env):
    env.db.execute("UPDATE members SET tg_username='known',tg_display_name='已知' WHERE emby_user_id='u1'")
    env.bot._tg_profile = AsyncMock(return_value={})
    async def run():
        rows = [{'tg_user_id': '903'}]
        await env.bot._remember_watch_profiles(rows)
        await env.bot._remember_watch_profiles(rows)
        assert env.bot._tg_profile.await_count == 1
        assert env.members.get('u1')['tg_display_name'] == '已知'
        env.bot._tg_profile_until['903'] = time.time() - 1
        env.bot._tg_profile.return_value = {'username': 'fresh', 'display_name': '新名字'}
        await env.bot._remember_watch_profiles(rows)
        assert env.members.get('u1')['tg_username'] == 'fresh'
    asyncio.run(run())


def test_group_help_and_private_find_labels_are_accurate(env):
    group_commands = set(GROUP_ADMIN_COMMANDS) | set(GROUP_MEMBER_COMMANDS)
    body = env.bot._group_help_text(env.members.get('admin'))
    assert set(re.findall(r'(?<!<)/([a-z]+)', body)) <= group_commands
    menu = env.bot.admin_menu()
    find = [b for row in menu for b in row if b['callback_data'] == 'admin_find']
    assert len(find) == 1 and '查找' in find[0]['text'] and '赠送' in find[0]['text']


ADMIN_AUTH = ('admin', 'change-me')


def test_web_pages_search_totals_and_remote_snapshot_invalidation(monkeypatch):
    with TestClient(app) as client:
        members = app.state.members
        for i in range(13):
            members.upsert(f'local{i:02}', f'name{i:02}', {'group_id': 'standard'})
        members.bind_telegram('local12', '123456789', 'TargetHandle')
        remote = AsyncMock(return_value=[{'Id': f'local{i:02}', 'Name': f'name{i:02}'} for i in range(13)]
                          + [{'Id': 'outside', 'Name': 'unmanaged'}])
        monkeypatch.setattr(app.state.emby, 'list_users', remote)
        def get(**kw):
            r = client.get('/api/members', params={'page': 1, 'page_size': 5, **kw}, auth=ADMIN_AUTH)
            assert r.status_code == 200
            return r.json()
        first, second, third = get(), get(page=2), get(page=3)
        assert [len(p['members']) for p in (first, second, third)] == [5, 5, 3]
        assert len({m['emby_user_id'] for p in (first, second, third) for m in p['members']}) == 13
        assert all(p['total'] == p['counts']['total'] == 13 for p in (first, second, third))
        assert [u['emby_user_id'] for u in first['unmanaged']] == ['outside']
        for search in ('123456789', '@targethandle'):
            assert get(search=search)['members'][0]['emby_user_id'] == 'local12'
        assert remote.await_count == 1
        members.set_status('local00', 'suspended')
        assert get(status='suspended')['total'] == 1
        assert remote.await_count == 2
        # Explicit remote writes invalidate even without a member audit record.
        monkeypatch.setattr(app.state.emby, 'set_user_disabled', AsyncMock(return_value=True))
        client.post('/api/emby/users/local00/disable', auth=ADMIN_AUTH)
        get()
        assert remote.await_count == 3
        app.state.cache.set('members:emby', app.state.cache.get('members:emby'), ttl=-1)
        remote.side_effect = RuntimeError('upstream unavailable')
        failed = get()
        assert all(m['emby_status'] == 'unknown' for m in failed['members'])
        assert failed['unmanaged_error']
        get(page=2)
        assert remote.await_count == 4


def test_web_paged_population_is_not_silently_capped_at_5000(monkeypatch):
    with TestClient(app) as client:
        with app.state.db.write() as conn:
            conn.executemany('INSERT INTO members(emby_user_id,username,group_id,status,created_at,updated_at) VALUES(?,?,?,?,0,0)',
                             [(f'u{i:05}', f'user{i:05}', 'standard', 'active') for i in range(5003)])
        monkeypatch.setattr(app.state.emby, 'list_users', AsyncMock(return_value=[]))
        result = client.get('/api/members?page=51&page_size=100', auth=ADMIN_AUTH).json()
        assert result['total'] == 5003 and len(result['members']) == 3
        assert result['members'][-1]['emby_user_id'] == 'u05002'


def test_snapshot_single_flight_and_write_during_fetch(monkeypatch):
    with TestClient(app):
        async def run():
            remote = AsyncMock(return_value=[])
            monkeypatch.setattr(app.state.emby, 'list_users', remote)
            await asyncio.gather(*[_member_emby_snapshot() for _ in range(5)])
            assert remote.await_count == 1
            app.state.cache.delete('members:emby')
            async def changing():
                _invalidate_member_snapshot()
                return []
            remote.side_effect = changing
            await _member_emby_snapshot()
            remote.side_effect = None
            await _member_emby_snapshot()
            assert remote.await_count == 3
        asyncio.run(run())
