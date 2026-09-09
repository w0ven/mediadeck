"""Committed registration receipts, local transport only; no production identities."""
import asyncio
import copy
import time
from types import SimpleNamespace

import pytest
from test_tg_group_gift import TARGET, grant, issue
from test_tg_interaction_context import ADMIN, GROUP, VIEWER, click, command
from test_tg_interaction_context import env as context_env  # noqa: F401

from app.modules.gift_receipts import GiftReceipts
from app.modules.metering import MeasuredMeteringService
from app.modules.telegram import TelegramBot


@pytest.fixture
def env(request):
    return request.getfixturevalue('context_env')


def receipts(e):
    return e.db.query('SELECT * FROM tg_gift_receipts ORDER BY id')


def public_receipts(e):
    return [p for method, p in e.tg.calls if method == 'sendMessage'
            and str(p.get('chat_id')) == str(GROUP) and '注册成功' in p.get('text', '')]


def restart_bot(e):
    old = e.bot
    e.bot = TelegramBot(lambda: e.cfg, e.members, old._emby, db=e.db, groups=e.groups,
                        registration=old._registration, points=old._points)
    e.bot._bot_username = old._bot_username
    e.bot._call = e.tg.call
    e.tg.bot = e.bot
    return e.bot


async def register(e, username='ReceiptMember'):
    await command(e, '/start ' + grant(e)['gift_code'], chat=TARGET, user=TARGET)
    return await command(e, username, chat=TARGET, user=TARGET)


@pytest.mark.parametrize('topic', [None, 77])
def test_persisted_origin_survives_bot_restart_and_true_registration_posts_once(env, topic):
    async def run():
        if topic:
            mid = await command(env, '/kk ' + str(TARGET), thread=topic)
            await click(env, 'admin_gift', mid, thread=topic)
            data = next(b['callback_data'] for b in env.tg.actions(GROUP, mid) if b.get('callback_data', '').startswith('admin_gift_ok:'))
            await click(env, data, mid, thread=topic)
        else:
            mid, _ = await issue(env)
        saved = grant(env)
        assert saved['origin_chat_id'] == str(GROUP) and saved['origin_message_id'] == mid
        assert saved['origin_thread_id'] == topic and saved['origin_bot_id'] == '123'
        restart_bot(env)
        original = env.bot._call
        async def transport(method, payload=None, timeout=20):
            if method == 'sendMessage' and str(payload.get('chat_id')) == str(GROUP) and '注册成功' in payload.get('text', ''):
                assert grant(env)['used_at'] and env.members.find_by_telegram(str(TARGET))
            return await original(method, payload, timeout)
        env.bot._call = transport
        private_mid = await register(env)
        posts = public_receipts(env)
        assert len(posts) == 1 and posts[0].get('message_thread_id') == topic
        body = posts[0]['text']
        assert 'tg://user?id=955' in body and 'ReceiptMember' in body
        assert '普通用户' in body and '到期' in body
        for forbidden in (saved['gift_code'], '密码', '领取码', 'https://', '来源', '采集', '《'):
            assert forbidden not in body
        assert 'reply_markup' not in posts[0] and 'reply_to_message_id' not in posts[0]
        assert '密码：' in env.tg.text(TARGET, private_mid)
        assert receipts(env)[0]['status'] == 'sent' and receipts(env)[0]['message_id']
        assert len(env.tg.actions(GROUP, mid)) == 1  # original announcement not replaced
        await command(env, '/start ' + saved['gift_code'], chat=TARGET, user=TARGET)
        await env.bot._gift_receipts.recover()
        assert len(public_receipts(env)) == 1 and len(receipts(env)) == 1
    asyncio.run(run())


def test_non_topic_reply_thread_is_not_saved_as_forum_topic(env):
    async def run():
        await env.bot._dispatch_update({'message': {'chat': {'id': GROUP, 'type': 'supergroup'},
            'from': {'id': ADMIN}, 'text': '/kk ' + str(TARGET), 'message_id': 20, 'message_thread_id': 19}})
        key = env.bot._session_key(GROUP, ADMIN, group=True)
        mid = env.bot._panel[key]
        await click(env, 'admin_gift', mid)
        data = next(b['callback_data'] for b in env.tg.actions(GROUP, mid) if b.get('callback_data', '').startswith('admin_gift_ok:'))
        await click(env, data, mid)
        assert grant(env)['origin_thread_id'] is None
        await register(env)
        assert 'message_thread_id' not in public_receipts(env)[0]
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['private', 'legacy'])
def test_private_and_old_gifts_do_not_guess_group_origin(env, kind):
    async def run():
        if kind == 'private':
            await issue(env, chat=ADMIN)
        else:
            env.bot._registration.issue_gift(str(TARGET), 'old')
        await register(env)
        assert grant(env)['used_at'] and not receipts(env) and not public_receipts(env)
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['create', 'password', 'consume'])
def test_registration_failure_never_stages_or_publishes_receipt(env, monkeypatch, failure):
    async def no(*args, **kwargs):
        return None
    async def run():
        await issue(env)
        await command(env, '/start ' + grant(env)['gift_code'], chat=TARGET, user=TARGET)
        if failure == 'create':
            monkeypatch.setattr(env.bot._emby, 'create_user', no)
        elif failure == 'password':
            monkeypatch.setattr(env.bot._emby, 'set_user_password', no)
        else:
            monkeypatch.setattr(env.bot._registration, 'consume', lambda *a, **kw: False)
        await command(env, 'FailedReceipt', chat=TARGET, user=TARGET)
        assert not env.members.find_by_telegram(str(TARGET))
        assert not grant(env)['used_at'] and not receipts(env) and not public_receipts(env)
    asyncio.run(run())


@pytest.mark.parametrize('condition', ['revoked', 'expired_session', 'other_recipient', 'already_registered'])
def test_invalid_claim_does_not_publish_success(env, condition):
    async def run():
        await issue(env)
        code = grant(env)['gift_code']
        if condition == 'revoked':
            env.bot._registration.revoke_grant(str(TARGET))
        if condition == 'already_registered':
            env.members.upsert('existing', 'Existing', {'group_id': 'standard'})
            env.members.bind_telegram('existing', str(TARGET))
        user = TARGET + 1 if condition == 'other_recipient' else TARGET
        await command(env, '/start ' + code, chat=user, user=user)
        if condition == 'expired_session':
            key = str(user)
            waiting = env.bot._pending[key]
            env.bot._pending[key] = (waiting[0], time.time() - 1, waiting[2])
        await command(env, 'NotCreated', chat=user, user=user)
        assert not receipts(env) and not public_receipts(env)
    asyncio.run(run())


def test_deleted_or_changed_gift_message_does_not_block_new_receipt(env):
    async def run():
        mid, _ = await issue(env)
        env.tg.messages.pop((str(GROUP), mid))
        await register(env)
        assert receipts(env)[0]['status'] == 'sent'
        assert public_receipts(env)[0].get('reply_to_message_id') is None
    asyncio.run(run())


def test_pending_receipt_survives_commit_before_delivery_restart(env, monkeypatch):
    async def defer(*a, **kw):
        return None
    async def run():
        await issue(env)
        monkeypatch.setattr(env.bot._gift_receipts, 'deliver', defer)
        await register(env)
        assert receipts(env)[0]['status'] == 'pending' and not public_receipts(env)
        restart_bot(env)
        await env.bot._gift_receipts.recover()
        await env.bot._gift_receipts.recover()
        assert receipts(env)[0]['status'] == 'sent' and len(public_receipts(env)) == 1
    asyncio.run(run())


@pytest.mark.parametrize('retry_succeeds', [True, False])
def test_group_send_failure_keeps_registration_and_recipient_can_retry_once_after_restart(env, retry_succeeds):
    original = env.bot._call
    failing = {'value': True}
    async def transport(method, payload=None, timeout=20):
        if method == 'sendMessage' and str((payload or {}).get('chat_id')) == str(GROUP) and failing['value']:
            env.tg.calls.append((method, copy.deepcopy(payload)))
            return None
        return await original(method, payload, timeout)
    async def run():
        await issue(env)
        env.bot._call = transport
        await register(env)
        row = receipts(env)[0]
        assert row['status'] == 'failed' and row['attempts'] == 1
        uid = env.members.find_by_telegram(str(TARGET))['emby_user_id']
        assert grant(env)['used_at']
        assert any('账号注册成功。群回执暂未送达' in p.get('text', '') for _, p in env.tg.calls)
        restart_bot(env)
        env.bot._call = transport
        await env.bot._gift_receipts.recover()
        mid = await command(env, '/start', chat=TARGET, user=TARGET)
        action = f'gift_receipt_retry:{row["id"]}'
        assert any(b.get('callback_data') == action for b in env.tg.actions(TARGET, mid))
        before = len(public_receipts(env))
        await click(env, action, mid, chat=TARGET + 1, user=TARGET + 1)
        assert len(public_receipts(env)) == before and receipts(env)[0]['attempts'] == 1
        failing['value'] = not retry_succeeds
        await click(env, action, mid, chat=TARGET, user=TARGET)
        await click(env, action, mid, chat=TARGET, user=TARGET)
        after = receipts(env)[0]
        assert after['attempts'] == 2 and after['status'] == ('sent' if retry_succeeds else 'failed')
        assert env.members.find_by_telegram(str(TARGET))['emby_user_id'] == uid
        assert len(public_receipts(env)) == before + 1
    asyncio.run(run())


def test_repeated_gift_does_not_redirect_origin_and_rearm_does_not_reuse_origin(env):
    async def run():
        await issue(env)
        first = grant(env)
        other = env.bot._registration.issue_gift(str(TARGET), 'another', origin={
            'chat_id': '-100701', 'message_id': 1234, 'thread_id': 44, 'bot_id': '123'})
        assert other['gift_code'] == first['gift_code'] and other['origin_chat_id'] == str(GROUP)
        assert env.bot._registration.consume(env.bot._registration.resolve(str(TARGET)), 'used')
        private = env.bot._registration.issue_gift(str(TARGET), 'private')
        assert private['origin_chat_id'] is None and private['gift_code'] != first['gift_code']
    asyncio.run(run())


def test_interrupted_ack_is_not_automatically_resent(env, monkeypatch):
    async def defer(*a, **kw):
        return None
    async def run():
        await issue(env)
        monkeypatch.setattr(env.bot._gift_receipts, 'deliver', defer)
        await register(env)
        env.db.execute("UPDATE tg_gift_receipts SET status='sending',attempts=1")
        restart_bot(env)
        await env.bot._gift_receipts.recover()
        row = receipts(env)[0]
        assert row['status'] == 'failed' and row['last_error'] == 'delivery_interrupted'
        assert not public_receipts(env)
    asyncio.run(run())


def test_cancel_waits_for_receipt_send_and_persistence(env, monkeypatch):
    async def defer(*a, **kw):
        return None
    async def run():
        await issue(env)
        monkeypatch.setattr(env.bot._gift_receipts, 'deliver', defer)
        await register(env)
        service = GiftReceipts(env.bot)
        original = env.bot.send_message
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*a, **kw):
            entered.set()
            await release.wait()
            return await original(*a, **kw)
        monkeypatch.setattr(env.bot, 'send_message', delayed)
        task = asyncio.create_task(service.deliver(receipts(env)[0]['id']))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert receipts(env)[0]['status'] == 'sent' and len(public_receipts(env)) == 1
    asyncio.run(run())


def test_bot_start_recovers_committed_pending_notice_and_stop_joins_worker(env, monkeypatch):
    async def defer(*a, **kw):
        return None
    async def idle():
        await asyncio.sleep(3600)
    async def run():
        await issue(env)
        monkeypatch.setattr(env.bot._gift_receipts, 'deliver', defer)
        await register(env)
        restart_bot(env)
        monkeypatch.setattr(env.bot, 'run', idle)
        env.bot.start()
        try:
            for _ in range(100):
                if receipts(env)[0]['status'] == 'sent':
                    break
                await asyncio.sleep(.01)
            assert receipts(env)[0]['status'] == 'sent'
        finally:
            await env.bot.stop()
        assert not env.bot._in_flight and len(public_receipts(env)) == 1
    asyncio.run(run())


def test_parallel_delivery_claim_is_one_use_in_sql(env, monkeypatch):
    async def defer(*a, **kw):
        return None
    async def run():
        await issue(env)
        monkeypatch.setattr(env.bot._gift_receipts, 'deliver', defer)
        await register(env)
        rid = receipts(env)[0]['id']
        services = [GiftReceipts(env.bot), GiftReceipts(env.bot)]
        await asyncio.gather(*(s.deliver(rid) for s in services))
        assert receipts(env)[0]['status'] == 'sent' and receipts(env)[0]['attempts'] == 1
        assert len(public_receipts(env)) == 1
    asyncio.run(run())


def test_receipt_staging_and_binding_rollback_together(env, monkeypatch):
    async def run():
        await issue(env)
        original = env.bot._gift_receipts.stage
        def fail_after_insert(*a, **kw):
            original(*a, **kw)
            raise OSError('local simulated transaction failure')
        monkeypatch.setattr(env.bot._gift_receipts, 'stage', fail_after_insert)
        await register(env)
        assert not receipts(env) and not grant(env)['used_at']
        assert not env.members.find_by_telegram(str(TARGET)) and not public_receipts(env)
    asyncio.run(run())


@pytest.mark.parametrize('health', ['healthy', 'stale', 'absent'])
@pytest.mark.parametrize('chat', [GROUP, VIEWER])
def test_real_snapshot_none_is_presented_without_mutating_usage(env, health, chat):
    meter = MeasuredMeteringService(env.db, expected_nodes=lambda: ['local'])
    if health != 'absent':
        observed = time.time() if health == 'healthy' else time.time() - 1000
        env.db.execute('INSERT INTO measured_node_seq(node,boot_id,seq,nft_ok,observed_at,updated_at) VALUES(?,?,?,?,?,?)',
                       ('local', 'boot', 1, 1, observed, time.time()))
    env.members.bind_metering(meter, cutover=True)
    before = env.members.get('u1')
    assert before['measured_used_bytes'] is None
    async def run():
        mid = await command(env, '/me', chat=chat, user=VIEWER)
        text = env.tg.text(chat, mid)
        if health == 'healthy':
            assert '本周期暂无播放记录' in text and '剩余：<b>1.0 TiB</b>' in text
            assert '0 B' not in text
        else:
            assert '计量暂不可用' in text and '剩余：<b>暂无法确认</b>' in text
            assert '剩余：<b>1.0 TiB</b>' not in text
        for hidden in ('采集', '来源', 'local', 'boot'):
            assert hidden not in text
    asyncio.run(run())
    assert env.members.get('u1')['measured_used_bytes'] is None
    assert env.db.one('SELECT COUNT(*) n FROM measured_usage_monthly')['n'] == 0


@pytest.mark.parametrize('used', [None, 0, 1024])
def test_unlimited_and_numeric_measurement_text_remain_truthful(env, used):
    sample = {'measured_used_bytes': used, 'measurement_status': 'no_usage_records' if used is None else 'measured',
              'coverage': {'degraded': False}}
    env.members.bind_metering(SimpleNamespace(snapshot=lambda uid: sample), cutover=True)
    env.members.upsert('u1', 'ViewerA', {'group_id': 'whitelist'})
    mid = asyncio.run(command(env, '/me', user=VIEWER))
    text = env.tg.text(GROUP, mid)
    assert '剩余：<b>不限</b>' in text
    assert ('本周期暂无播放记录' if used is None else '0 B' if used == 0 else '1.0 KiB') in text
