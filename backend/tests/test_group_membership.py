"""Real Bot/service/delete paths; only Telegram and Emby transports are local."""
import asyncio
import copy
import time
from unittest.mock import AsyncMock

import pytest
from test_tg_interaction_context import ADMIN, GROUP, VIEWER, click, command
from test_tg_interaction_context import env as context_env  # noqa: F401

from app.core.errors import ConfigError
from app.core.store import SettingsStore
from app.modules.group_membership import (
    GroupMembership,
    GroupMembershipPlugin,
    normalize_rules,
    presence,
)
from app.modules.plugins import PluginRegistry

CHANNEL = '-100800'


@pytest.fixture
def env(request):
    e = request.getfixturevalue('context_env')
    e.cfg['membership_rules'] = {'targets': [
        {'chat_id': str(GROUP), 'title': '讨论群', 'type': 'supergroup', 'enabled': True,
         'join_url': 'https://t.me/local_group', 'verification': 'ready'},
        {'chat_id': CHANNEL, 'title': '公告频道', 'type': 'channel', 'enabled': True,
         'join_url': 'https://t.me/local_channel', 'verification': 'ready'}],
        'gate_enabled': False, 'delete_enabled': False, 'generation': 'initial', 'enabled_since': int(time.time()) - 20}
    e.db.execute('UPDATE members SET created_at=?,tg_bound_at=?', (int(time.time()) - 120, int(time.time()) - 120))
    e.states = {}
    e.permissions = {}
    e.emby_flags = {uid: False for uid in ('admin', 'admin2', 'u1', 'u2')}
    e.deleted = []
    e.hook = None
    e.counts = {}
    original = e.bot._call
    async def transport(method, payload=None, timeout=20):
        if method == 'getChat':
            e.tg.calls.append((method, payload))
            cid = str(payload['chat_id'])
            if cid.startswith('@'):
                cid = CHANNEL
            return {'id': int(cid), 'type': 'channel' if cid == CHANNEL else 'supergroup', 'title': 'Actual ' + cid}
        if method == 'getChatMember':
            e.tg.calls.append((method, payload))
            cid, uid = str(payload['chat_id']), str(payload['user_id'])
            key = (cid, uid)
            e.counts[key] = e.counts.get(key, 0) + 1
            if e.hook:
                await e.hook(cid, uid, e.counts[key])
            status = e.permissions.get(cid, 'administrator') if uid == '123' else e.states.get(key, 'member')
            if status is None:
                return None
            if isinstance(status, dict):
                return {'user': {'id': int(uid)}, **status}
            return {'user': {'id': int(uid)}, 'status': status}
        return await original(method, payload, timeout)
    e.bot._call = transport
    async def users():
        return [{'Id': uid, 'Policy': {'IsAdministrator': flag}} for uid, flag in e.emby_flags.items()]
    async def delete(uid):
        e.deleted.append(uid)
        e.emby_flags.pop(uid, None)
        return True
    e.bot._emby.list_users = users
    e.bot._emby.delete_user = delete
    return e


def leave(uid=VIEWER, status='left', update_id=10, date=None, chat=GROUP):
    return {'update_id': update_id, 'chat_member': {'chat': {'id': chat, 'type': 'supergroup'},
        'from': {'id': ADMIN}, 'date': int(time.time()) if date is None else date,
        'old_chat_member': {'user': {'id': uid}, 'status': 'member'},
        'new_chat_member': {'user': {'id': uid}, 'status': status}}}


async def scan(e):
    first = e.bot.membership.start_scan()
    assert e.bot.membership.start_scan()['id'] == first['id']
    await e.bot.membership._scan_task
    return e.bot.membership.status()


@pytest.mark.parametrize('status,expected', [('member', 'present'), ('left', 'absent'), ('kicked', 'absent'),
    ('administrator', 'present'), ('creator', 'present'), ('restricted', 'unknown'), ('unexpected', 'unknown')])
def test_presence_is_strict(status, expected):
    assert presence({'user': {'id': VIEWER}, 'status': status}, str(VIEWER)) == expected
    assert presence({'user': {'id': ADMIN}, 'status': status}, str(VIEWER)) == 'unknown'


@pytest.mark.parametrize('is_member', [True, False])
def test_restricted_membership_is_explicit(is_member):
    assert presence({'user': {'id': VIEWER}, 'status': 'restricted', 'is_member': is_member}, str(VIEWER)) == ('present' if is_member else 'absent')


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('status', ['left', 'kicked'])
def test_event_targets_departing_user_not_operator_and_switch_is_authority(env, enabled, status):
    env.cfg['membership_rules']['delete_enabled'] = enabled
    env.states[(str(GROUP), str(VIEWER))] = status
    asyncio.run(env.bot._dispatch_update(leave(status=status)))
    assert env.deleted == (['u1'] if enabled else [])
    assert env.members.get('admin') and env.members.get('u2')


@pytest.mark.parametrize('enabled', [False, True])
def test_new_scan_can_process_unobserved_existing_accounts_only_when_enabled(env, enabled):
    env.cfg['membership_rules']['delete_enabled'] = enabled
    env.states[(CHANNEL, str(VIEWER))] = 'left'
    out = asyncio.run(scan(env))
    row = next(r for r in out['rows'] if r['user_id'] == 'u1')
    assert out['processed'] == out['total'] == 4 and not out['running']
    assert row['state'] == 'absent'
    assert row['action'] == ('deleted' if enabled else 'detected')
    assert env.deleted == (['u1'] if enabled else [])
    assert any(r['state'] == 'exempt' for r in out['rows'])


@pytest.mark.parametrize('condition', ['query_unknown', 'restricted_unknown', 'bot_lost_rights', 'admin_unknown', 'mixed_unknown'])
def test_unknown_never_becomes_delete_evidence(env, condition):
    env.cfg['membership_rules']['delete_enabled'] = True
    env.states[(CHANNEL, str(VIEWER))] = 'left'
    if condition == 'query_unknown':
        env.states[(CHANNEL, str(VIEWER))] = None
    elif condition == 'restricted_unknown':
        env.states[(CHANNEL, str(VIEWER))] = 'restricted'
    elif condition == 'bot_lost_rights':
        env.permissions[CHANNEL] = 'member'
    elif condition == 'admin_unknown':
        env.emby_flags['u1'] = None
    else:
        env.states[(str(GROUP), str(VIEWER))] = None
    asyncio.run(scan(env))
    assert not env.deleted and env.members.get('u1')


@pytest.mark.parametrize('identity', ['deck_admin', 'emby_admin', 'whitelist'])
def test_only_admins_exempt_whitelist_still_deleted(env, identity):
    env.cfg['membership_rules']['delete_enabled'] = True
    env.states[(CHANNEL, str(VIEWER))] = 'left'
    if identity == 'deck_admin':
        env.members.set_roles('u1', ['admin'])
    elif identity == 'emby_admin':
        env.emby_flags['u1'] = True
    else:
        env.members.upsert('u1', 'ViewerA', {'group_id': 'whitelist'})
    asyncio.run(scan(env))
    assert env.deleted == (['u1'] if identity == 'whitelist' else [])


@pytest.mark.parametrize('change', ['rejoin', 'unbind', 'deck_admin', 'emby_admin', 'switch_off', 'rules_change', 'bot_event'])
def test_fresh_final_recheck_cancels_changed_authority(env, change):
    env.cfg['membership_rules']['delete_enabled'] = True
    env.states[(CHANNEL, str(VIEWER))] = 'left'
    async def hook(cid, uid, count):
        if cid != str(GROUP) or uid != str(VIEWER) or count != 2:
            return
        if change == 'rejoin':
            env.states[(CHANNEL, str(VIEWER))] = 'member'
        elif change == 'unbind':
            env.members.bind_telegram('u1', '9999')
        elif change == 'deck_admin':
            env.members.set_roles('u1', ['admin'])
        elif change == 'emby_admin':
            env.emby_flags['u1'] = True
        elif change == 'switch_off':
            env.cfg['membership_rules']['delete_enabled'] = False
        elif change == 'rules_change':
            env.cfg['membership_rules']['generation'] = 'new'
        else:
            await env.bot.membership.handle_update({'my_chat_member': {'chat': {'id': GROUP}}})
    env.hook = hook
    asyncio.run(scan(env))
    assert not env.deleted and env.members.get('u1')


def test_delete_failure_retains_local_and_history_deletion_only_self(env):
    env.cfg['membership_rules']['delete_enabled'] = True
    env.states[(CHANNEL, str(VIEWER))] = 'left'
    env.bot._emby.delete_user = AsyncMock(return_value=False)
    out = asyncio.run(scan(env))
    assert next(r for r in out['rows'] if r['user_id'] == 'u1')['action'] == 'failed_retained'
    assert env.members.get('u1') and env.bot._points.balance('u1') == 50
    env.db.execute("UPDATE members SET inviter_id='u1' WHERE emby_user_id='u2'")
    env.db.execute("INSERT INTO watch_totals(emby_user_id,seconds,first_at,last_at) VALUES('u1',3600,1,2)")
    env.bot._emby.delete_user = AsyncMock(return_value=True)
    asyncio.run(scan(env))
    assert env.members.get('u1') is None and env.members.get('u2')
    assert env.bot._points.balance('u1') == 50
    assert env.db.one("SELECT seconds FROM watch_totals WHERE emby_user_id='u1'")['seconds'] == 3600
    assert env.db.query("SELECT * FROM audit_log WHERE action='telegram.membership.result'")


def test_old_duplicate_and_newer_join_events_safe(env):
    env.cfg['membership_rules']['delete_enabled'] = True
    env.states[(str(GROUP), str(VIEWER))] = 'left'
    async def run():
        await env.bot._dispatch_update(leave(date=int(time.time()) - 100))
        assert not env.deleted
        joined = leave(status='member', update_id=20)
        await env.bot._dispatch_update(joined)
        await env.bot._dispatch_update(leave(update_id=19))
        assert not env.deleted
        await env.bot._dispatch_update(leave(update_id=21))
        await env.bot._dispatch_update(leave(update_id=21))
    asyncio.run(run())
    assert env.deleted == ['u1']


def test_channel_event_and_service_message_are_supported_without_interaction_acl(env):
    env.cfg['membership_rules']['delete_enabled'] = True
    env.states[(CHANNEL, str(VIEWER))] = 'left'
    asyncio.run(env.bot._dispatch_update(leave(chat=int(CHANNEL))))
    assert env.deleted == ['u1']
    assert CHANNEL not in env.cfg['group_interaction_chats']


def test_gate_edits_original_rechecks_and_admin_not_locked(env):
    env.cfg['membership_rules']['gate_enabled'] = True
    env.states[(CHANNEL, str(VIEWER))] = 'left'
    async def run():
        mid = await command(env, '/me', user=VIEWER)
        assert '请先加入' in env.tg.text(GROUP, mid)
        assert 'membership_recheck' in str(env.tg.actions(GROUP, mid))
        env.tg.calls.clear()
        await click(env, 'membership_recheck', mid, user=VIEWER)
        assert '请先加入' in env.tg.text(GROUP, mid)
        assert not any(m == 'sendMessage' for m, _ in env.tg.calls)
        env.states[(CHANNEL, str(VIEWER))] = 'member'
        await click(env, 'membership_recheck', mid, user=VIEWER)
        assert '欢迎回来' in env.tg.text(GROUP, mid)
        admin = await command(env, '/kk ViewerA')
        assert '管理目标 · ViewerA' in env.tg.text(GROUP, admin)
    asyncio.run(run())


def test_gate_unknown_not_join_failure_and_never_deletes(env):
    env.cfg['membership_rules'].update(gate_enabled=True, delete_enabled=True)
    env.states[(CHANNEL, str(VIEWER))] = None
    mid = asyncio.run(command(env, '/start', chat=VIEWER, user=VIEWER))
    assert '暂时无法核实' in env.tg.text(VIEWER, mid) and not env.deleted


def test_rules_cannot_trust_browser_verification(env):
    raw = copy.deepcopy(env.cfg['membership_rules'])
    raw['delete_enabled'] = True
    with pytest.raises(ConfigError):
        normalize_rules(raw)
    out = asyncio.run(env.bot.membership.prepare_rules(raw))
    assert out['targets'][1]['type'] == 'channel'
    env.permissions[CHANNEL] = 'member'
    with pytest.raises(ConfigError):
        asyncio.run(env.bot.membership.prepare_rules(raw))


def test_scheduler_uses_existing_registry_and_starts_disabled(env, tmp_path):
    store = SettingsStore(tmp_path / 'settings.json')
    registry = PluginRegistry(store, env.db)
    env.bot.bind_plugins(registry)
    assert isinstance(registry.get('group_membership'), GroupMembershipPlugin)
    assert not registry.enabled('group_membership')
    env.states[(CHANNEL, str(VIEWER))] = 'left'
    env.cfg['membership_rules']['delete_enabled'] = True
    registry.save('group_membership', True, {'mode': 'interval', 'interval_hours': 1})
    async def run():
        assert await registry.tick() == ['group_membership']
        await env.bot.membership._scan_task
    asyncio.run(run())
    assert env.deleted == ['u1']


def test_service_leave_message_deletes_departed_target_not_sender(env):
    env.cfg['membership_rules']['delete_enabled'] = True
    env.states[(str(GROUP), str(VIEWER))] = 'left'
    asyncio.run(env.bot._dispatch_update({'update_id': 90, 'message': {
        'chat': {'id': GROUP, 'type': 'supergroup'}, 'date': int(time.time()),
        'from': {'id': ADMIN}, 'left_chat_member': {'id': VIEWER}}}))
    assert env.deleted == ['u1'] and env.members.get('admin')


def test_delayed_leave_before_rebinding_cannot_delete_new_holder(env):
    env.cfg['membership_rules']['delete_enabled'] = True
    old_time = int(time.time()) - 5
    env.members.bind_telegram('u2', str(VIEWER))
    env.states[(str(GROUP), str(VIEWER))] = 'left'
    asyncio.run(env.bot._dispatch_update(leave(date=old_time)))
    assert not env.deleted and env.members.get('u2')


def test_join_event_during_final_emby_read_cancels_even_scan_deletion(env):
    env.cfg['membership_rules']['delete_enabled'] = True
    env.states[(CHANNEL, str(VIEWER))] = 'left'
    original = env.bot._emby.list_users
    calls = []
    async def users():
        calls.append(1)
        if len(calls) == 3:
            await env.bot._dispatch_update(leave(status='member', update_id=99, chat=int(CHANNEL)))
        return await original()
    env.bot._emby.list_users = users
    asyncio.run(scan(env))
    assert not env.deleted and env.members.get('u1')


def test_gate_rule_change_during_query_does_not_grant_access(env):
    env.cfg['membership_rules']['gate_enabled'] = True
    async def hook(cid, uid, count):
        if uid == str(VIEWER):
            env.cfg['membership_rules']['generation'] = str(count) + cid
    env.hook = hook
    mid = asyncio.run(command(env, '/me', chat=VIEWER, user=VIEWER))
    assert '暂时无法核实' in env.tg.text(VIEWER, mid)


def test_normal_group_conversation_does_not_trigger_gate_queries_or_messages(env):
    env.cfg['membership_rules']['gate_enabled'] = True
    env.states[(CHANNEL, str(VIEWER))] = 'left'
    asyncio.run(command(env, '只是普通群聊', user=VIEWER))
    assert not any(method in ('getChat', 'getChatMember', 'sendMessage', 'editMessageText')
                   for method, _ in env.tg.calls)


def test_restart_never_replays_last_scan_or_old_plugin_notices(env):
    env.cfg['membership_rules']['delete_enabled'] = True
    env.bot.membership._save('latest', {'running': True, 'rows': [], 'id': 'old'})
    restored = GroupMembership(env.bot)
    assert not restored.status()['running'] and restored.status()['interrupted']
    assert not env.deleted and restored._scan_task is None
