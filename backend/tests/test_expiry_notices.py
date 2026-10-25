"""Scheduled expiry deletion -> real services -> durable group receipt, isolated I/O."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_activity_levels import NOW
from test_activity_levels import env as levels_env  # noqa: F401
from test_economy import build

from app.adapters.live import LiveEmby
from app.adapters.mock import MockEmby
from app.core.db import Database
from app.modules import member_ops
from app.modules.expiry_notices import KIND, ExpiryNotices
from app.modules.levels import DAY, LevelService
from app.modules.report_delivery import CALL_DELIVERY, failure
from app.modules.telegram import TelegramBot
from app.modules.watch_rewards import ActivityService

TG = '77700123'
CHAT = '-10077700123'


def attach(e):
    e.ctx = e.checkin.ctx
    e.ctx.levels, e.ctx.emby = e.levels, e.emby
    e.ctx.store.set_section('telegram', e.cfg)
    e.bot = TelegramBot(lambda: e.cfg, e.members, e.emby, db=e.db, groups=e.groups)
    e.bot._call = e.transport
    e.ctx.telegram = e.bot
    e.plugin = e.registry.get('activity_lifecycle')


@pytest.fixture
def e(request, monkeypatch):
    e = request.getfixturevalue('levels_env')
    e.clock = [NOW]
    monkeypatch.setattr('time.time', lambda: e.clock[0])
    e.members.upsert('u', '', {'expires_at': NOW-3*DAY})
    e.db.execute("UPDATE members SET username='private-media-login',tg_user_id=?,tg_display_name='群友 <&>',tg_username='tg_public' WHERE emby_user_id='u'", (TG,))
    e.config['delete_enabled'] = True
    e.levels.save_config(e.config, 1)
    e.registry.save('activity_lifecycle', enabled=True)
    e.cfg = {'enabled': True, 'bot_token': '123456:isolated-no-network',
             'registration_notify_chat_id': CHAT, 'registration_notify_thread_id': 77,
             'group_interaction_chats': [CHAT, '-10077700124']}
    e.calls = []
    async def transport(method, payload=None, **kw):
        CALL_DELIVERY.set(None)
        e.calls.append((method, copy.deepcopy(payload)))
        if method == 'sendMessage':
            return {'message_id': 1000+len(e.calls)}
        return None
    e.transport = transport
    e.emby = MockEmby()
    e.emby._users['u'] = {'Id': 'u', 'Name': 'private-media-login', 'Policy': {'IsAdministrator': False}}
    e.emby.delete_user = AsyncMock(wraps=e.emby.delete_user)
    attach(e)
    return e


def jobs(e):
    return e.db.query('SELECT * FROM play_jobs WHERE kind=? ORDER BY id', (KIND,))


def posts(e):
    return [p for method, p in e.calls if method == 'sendMessage' and str(p.get('chat_id', '')).startswith('-')]


def restart(e):
    # Reopen SQLite and reconstruct the actual scheduled services, not an in-memory queue.
    store, path = e.ctx.store, e.db.path
    e.db.close()
    db = Database(path)
    other = build(db, store)
    for key in ('db', 'members', 'groups', 'points', 'registry', 'checkin'):
        setattr(e, key, getattr(other, key))
    e.levels = LevelService(db, e.members, e.points, ActivityService(db, e.points))
    e.members.bind_levels(e.levels)
    attach(e)


@pytest.mark.asyncio
@pytest.mark.parametrize('topic', [None, 77])
async def test_actual_expiry_delete_stages_one_safe_receipt_and_never_repeats(e, topic):
    e.cfg['registration_notify_thread_id'] = topic
    e.points.add('u', 10, 'admin.adjust')
    result = await e.plugin.run({})
    assert result['已删除'] == result['已播报'] == 1
    assert not e.members.get('u') and 'u' not in e.emby._users
    assert e.points.balance('u') == 10
    e.emby.delete_user.assert_awaited_once_with('u')
    row = jobs(e)[0]
    assert row['state'] == 'sent' and row['attempts'] == 1
    assert json.loads(row['result_json'])['message_id']
    assert len(posts(e)) == 1
    p = posts(e)[0]
    assert p['chat_id'] == CHAT and p.get('message_thread_id') == topic
    assert f'<a href="tg://user?id={TG}">群友 &lt;&amp;&gt;</a>' in p['text']
    assert '到期清理成功，账号已删除' in p['text'] and '北京时间' in p['text']
    assert not any(s in p['text'] for s in ('private-media-login', '密码', '资格码'))
    assert e.db.one("SELECT ok FROM audit_log WHERE actor='activity.expiry' AND action='member.delete_emby'")['ok'] == 1
    assert (await e.plugin.run({}))['已播报'] == 0
    assert jobs(e) == [row] and len(posts(e)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('protection', ['disabled', 'not_due', 'expired_only', 'manual_suspend', 'local_admin', 'remote_admin', 'whitelist', 'whitelist_card', 'unsettled'])
async def test_disabled_not_due_and_protected_members_do_not_delete_or_stage(e, protection):
    if protection == 'disabled':
        cfg = copy.deepcopy(e.config); cfg['delete_enabled'] = False
        e.levels.save_config(cfg, 2)
    elif protection in ('not_due', 'expired_only'):
        e.members.upsert('u', '', {'expires_at': NOW-3*DAY+1 if protection == 'not_due' else NOW-1})
    elif protection == 'manual_suspend':
        e.members.set_status('u', 'suspended')
    elif protection == 'local_admin':
        e.members.upsert('u', '', {'roles': ['admin']})
    elif protection == 'remote_admin':
        e.emby._users['u']['Policy']['IsAdministrator'] = True
    elif protection == 'whitelist':
        e.members.upsert('u', '', {'group_id': 'whitelist'})
    elif protection == 'whitelist_card':
        # Effective member overlay, as returned by the existing whitelist-card service.
        original = e.members.get
        e.members.get = lambda uid: dict(original(uid), whitelist_card_expires_at=NOW+DAY)
    else:
        e.db.execute("INSERT INTO activity_watch_pending VALUES('pending',?,'u',?)", (NOW, NOW-600))
    await e.plugin.run({})
    e.emby.delete_user.assert_not_awaited()
    assert e.members.get('u') and not jobs(e) and not posts(e)


@pytest.mark.asyncio
@pytest.mark.parametrize('remote_failure', ['false', 'timeout', 'already_absent'])
async def test_remote_failure_or_already_absent_does_not_claim_a_new_success(e, remote_failure):
    if remote_failure == 'false':
        e.emby.delete_user = AsyncMock(return_value=False)
    elif remote_failure == 'timeout':
        e.emby.delete_user = AsyncMock(side_effect=TimeoutError('isolated remote uncertainty'))
    else:
        del e.emby._users['u']
    result = await e.plugin.run({})
    assert result['已删除'] == int(remote_failure == 'already_absent')
    assert not jobs(e) and not posts(e)
    if remote_failure != 'already_absent':
        assert e.members.get('u') and e.members.get('u')['activity_lifecycle'] == 'deleting'
    else:
        e.emby.delete_user.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('identity', ['nickname', 'handle', 'tg_only', 'unbound'])
async def test_safe_public_identity_never_falls_back_to_media_login(e, identity):
    if identity != 'nickname':
        e.db.execute("UPDATE members SET tg_display_name='' WHERE emby_user_id='u'")
    if identity in ('tg_only', 'unbound'):
        e.db.execute("UPDATE members SET tg_username='' WHERE emby_user_id='u'")
    if identity == 'unbound':
        e.db.execute("UPDATE members SET tg_user_id='' WHERE emby_user_id='u'")
    await e.plugin.run({})
    body = posts(e)[0]['text']
    assert 'private-media-login' not in body and len(posts(e)) == 1
    if identity == 'unbound':
        assert '未绑定 Telegram 的成员' in body and 'tg://user' not in body
    elif identity == 'handle':
        assert '@tg_public' in body
    elif identity == 'tg_only':
        assert f'TG {TG}' in body


@pytest.mark.asyncio
async def test_no_configured_target_does_not_guess_a_group_or_backfill(e):
    e.cfg['registration_notify_chat_id'] = ''
    assert (await e.plugin.run({}))['已删除'] == 1
    assert not jobs(e) and not posts(e)
    e.cfg['registration_notify_chat_id'] = CHAT
    await e.plugin.run({})
    assert not jobs(e) and not posts(e)
    e.emby.delete_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_committed_pending_job_survives_process_restart_with_delete_off(e, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(ExpiryNotices, 'deliver', AsyncMock(return_value=0))
        assert (await e.plugin.run({}))['已删除'] == 1
    assert jobs(e)[0]['state'] == 'queued' and not posts(e)
    cfg = copy.deepcopy(e.config); cfg['delete_enabled'] = False
    e.levels.save_config(cfg, 2)
    restart(e)
    try:
        assert (await e.plugin.run({}))['已播报'] == 1
        assert (await e.plugin.run({}))['已播报'] == 0
        assert jobs(e)[0]['state'] == 'sent' and len(posts(e)) == 1
        e.emby.delete_user.assert_awaited_once()
    finally:
        e.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('telegram_failure', ['retry', 'unknown', 'rejected'])
async def test_message_failure_only_retries_message_not_remote_delete(e, telegram_failure):
    original = e.bot._call
    async def transport(method, payload=None, **kw):
        if method == 'sendMessage' and str(payload['chat_id']) == CHAT:
            assert not e.members.get('u') and 'u' not in e.emby._users
            e.calls.append((method, copy.deepcopy(payload)))
            code = {'retry': 429, 'rejected': 403}.get(telegram_failure)
            CALL_DELIVERY.set(failure({'error_code': code}) if code else None)
            return None
        return await original(method, payload, **kw)
    e.bot._call = transport
    result = await e.plugin.run({})
    assert result['已删除'] == 1 and result['已播报'] == 0
    assert jobs(e)[0]['state'] == {'retry': 'retry', 'rejected': 'failed', 'unknown': 'unknown'}[telegram_failure]
    e.bot._call = original
    e.clock[0] += 301
    await e.plugin.run({})
    e.emby.delete_user.assert_awaited_once()
    assert not e.members.get('u')
    assert len(posts(e)) == (2 if telegram_failure == 'retry' else 1)
    assert jobs(e)[0]['attempts'] == (2 if telegram_failure == 'retry' else 1)


@pytest.mark.asyncio
async def test_lost_send_ack_after_restart_is_not_resent(e, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(ExpiryNotices, 'deliver', AsyncMock(return_value=0))
        await e.plugin.run({})
    assert ExpiryNotices(e.db, lambda: e.cfg).claim()['attempts'] == 1
    e.clock[0] += 91
    restart(e)
    try:
        await e.plugin.run({})
        assert jobs(e)[0]['state'] == 'unknown' and not posts(e)
        e.emby.delete_user.assert_awaited_once()
    finally:
        e.db.close()


@pytest.mark.asyncio
async def test_duplicate_success_event_and_parallel_delivery_only_send_once(e, monkeypatch):
    original = ExpiryNotices.stage
    def twice(service, member):
        original(service, member)
        original(service, member)
    with monkeypatch.context() as patch:
        patch.setattr(ExpiryNotices, 'stage', twice)
        patch.setattr(ExpiryNotices, 'deliver', AsyncMock(return_value=0))
        await e.plugin.run({})
    db2 = Database(e.db.path)
    try:
        services = [ExpiryNotices(db, lambda: e.cfg) for db in (e.db, db2)]
        await asyncio.gather(*(s.deliver(e.bot) for s in services))
        assert len(jobs(e)) == len(posts(e)) == 1 and jobs(e)[0]['attempts'] == 1
        e.emby.delete_user.assert_awaited_once()
    finally:
        db2.close()


@pytest.mark.asyncio
async def test_local_cleanup_failure_retains_notice_and_retry_does_not_delete_again(e, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(e.members, 'delete', lambda *a, **kw: (_ for _ in ()).throw(OSError('isolated local cleanup failure')))
        result = await e.plugin.run({})
    assert result['待处理'] and 'u' not in e.emby._users
    assert e.members.get('u') and jobs(e)[0]['state'] == 'sent'
    assert (await e.plugin.run({}))['已删除'] == 1
    assert not e.members.get('u') and len(posts(e)) == 1
    e.emby.delete_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_plugin_cancellation_waits_for_group_ack_and_does_not_resend(e):
    original = e.bot._call
    entered, release = asyncio.Event(), asyncio.Event()
    async def transport(method, payload=None, **kw):
        if method == 'sendMessage' and str(payload['chat_id']) == CHAT:
            entered.set()
            await release.wait()
        return await original(method, payload, **kw)
    e.bot._call = transport
    task = asyncio.create_task(e.plugin.run({}))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert jobs(e)[0]['state'] == 'sent'
    await e.plugin.run({})
    assert len(posts(e)) == 1
    e.emby.delete_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_transport_failure_is_bounded_and_never_redeletes(e):
    original = e.bot._call
    async def transport(method, payload=None, **kw):
        if method == 'sendMessage' and str(payload['chat_id']) == CHAT:
            raise httpx.ConnectTimeout('isolated no connection')
        return await original(method, payload, **kw)
    e.bot._call = transport
    for attempt in range(1, 5):
        await e.plugin.run({})
        row = jobs(e)[0]
        assert row['attempts'] == attempt
        if attempt < 4:
            assert row['state'] == 'retry'
            e.clock[0] = row['due_at']
        else:
            assert row['state'] == 'failed'
    await e.plugin.run({})
    assert jobs(e)[0]['attempts'] == 4 and not e.members.get('u')
    e.emby.delete_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_delete_and_disable_never_create_expiry_receipts(e):
    assert await e.emby.set_user_disabled('u', True)
    assert not jobs(e) and not posts(e)
    result = await member_ops.execute_delete(e.members, e.emby, 'u', actor='operator', delete_emby=True)
    assert result['deleted'] and not jobs(e)
    await e.plugin.run({})
    assert not posts(e) and not jobs(e)
    e.emby.delete_user.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['target', 'topic', 'bot', 'allowlist', 'disabled'])
async def test_frozen_destination_is_not_redirected_or_broadcast_to_all_groups(e, monkeypatch, change):
    with monkeypatch.context() as patch:
        patch.setattr(ExpiryNotices, 'deliver', AsyncMock(return_value=0))
        await e.plugin.run({})
    if change == 'target':
        e.cfg['registration_notify_chat_id'] = '-10077700124'
    elif change == 'topic':
        e.cfg['registration_notify_thread_id'] = 88
    elif change == 'bot':
        e.cfg['bot_token'] = '987654:another-isolated-bot'
    elif change == 'allowlist':
        e.cfg['group_interaction_chats'] = []
    else:
        e.cfg['enabled'] = False
    await e.plugin.run({})
    assert not posts(e)
    assert jobs(e)[0]['state'] == ('queued' if change == 'disabled' else 'failed' if change == 'allowlist' else 'cancelled')
    e.emby.delete_user.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [200, 204, 403, 500, 'absent', 'unknown', 'admin'])
async def test_live_http_adapter_full_lifecycle_only_confirmed_delete_can_stage(e, monkeypatch, response):
    remote = SimpleNamespace(exists=response != 'absent', calls=[])
    def upstream(request):
        remote.calls.append((request.method, request.url.path))
        user = {'Id': 'u', 'Policy': {'IsAdministrator': response == 'admin'}}
        if request.url.path == '/emby/Users':
            return httpx.Response(200, json=[user] if remote.exists else [])
        if request.method == 'GET':
            return httpx.Response(200, json=user) if remote.exists else httpx.Response(404)
        assert request.url.path == '/emby/Users/u/Delete'
        if response == 'unknown':
            raise httpx.ReadTimeout('isolated lost remote response')
        if response in (200, 204):
            remote.exists = False
        return httpx.Response(response)
    e.emby = LiveEmby(lambda: {'enabled': True, 'url': 'https://emby.invalid', 'api_key': 'isolated-test-key'})
    monkeypatch.setattr(e.emby, '_client', lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    attach(e)
    result = await e.plugin.run({})
    confirmed = response in (200, 204)
    assert len(jobs(e)) == len(posts(e)) == int(confirmed)
    assert result['已删除'] == int(confirmed or response == 'absent')
    assert len([c for c in remote.calls if c[0] == 'POST']) == int(response not in ('absent', 'admin'))
    if confirmed:
        assert not remote.exists and not e.members.get('u') and jobs(e)[0]['state'] == 'sent'
