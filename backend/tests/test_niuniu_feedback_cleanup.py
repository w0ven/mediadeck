"""Short-lived group errors delete only their own durable feedback ID."""
import asyncio
import json

import pytest
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_niuniu import create, message
from test_niuniu import env as nn_env  # noqa: F401

from app.modules.checkin_cleanup import CleanupService, NiuniuFeedbackCleanup
from app.modules.report_delivery import CALL_DELIVERY


@pytest.fixture
def env(request):
    return request.getfixturevalue('nn_env')


def test_insufficient_banker_feedback_exact_id_after_60s_not_original_or_card(env):
    async def run():
        # Check-in cleanup being disabled must not disable game feedback cleanup.
        env.services.registry.save('checkin_cleanup', enabled=False)
        await env.bot._dispatch_update({'message': message(env, '/牛牛 1000001', topic=25)})
        job = env.db.one("SELECT * FROM play_jobs WHERE kind='niuniu.feedback.delete'")
        target = json.loads(job['payload_json'])
        assert target['message_id'] != 1001 and target['thread_id'] == 25
        assert '积分不足' in env.tg.text(target['chat_id'], target['message_id'])
        assert job['due_at']-job['created_at'] == 60
        assert CleanupService(env.db).claim('123', now=job['due_at']+1) is None
        await env.bot._drain_niuniu_feedback(now=job['due_at']-.1)
        assert not [p for m,p in env.tg.calls if m=='deleteMessage' and p['message_id']==target['message_id']]
        card = await create(env, mid=1002, topic=25)
        # Fresh service object claims the persisted job after the due time.
        assert NiuniuFeedbackCleanup(env.db).kind == 'niuniu.feedback.delete'
        await env.bot._drain_niuniu_feedback(now=job['due_at']+1)
        await env.bot._drain_niuniu_feedback(now=job['due_at']+2)
        deleted = [p['message_id'] for m,p in env.tg.calls if m=='deleteMessage']
        assert deleted.count(target['message_id']) == 1
        assert card['card_message_id'] not in deleted
        assert env.db.one('SELECT state FROM play_jobs WHERE id=?',(job['id'],))['state']=='deleted'
    asyncio.run(run())


def test_failed_feedback_send_has_no_delete_job_and_private_help_retained(env):
    async def run():
        call = env.bot._call
        async def fail(method, fields, **kwargs):
            if method=='sendMessage' and '积分不足' in fields.get('text',''):return None
            return await call(method, fields, **kwargs)
        env.bot._call=fail
        await env.bot._dispatch_update({'message':message(env, '/牛牛 1000001')})
        assert not env.db.query("SELECT * FROM play_jobs WHERE kind='niuniu.feedback.delete'")
        env.bot._call=call
        msg=message(env, '/牛牛帮助')
        msg['chat']={'id':env.actors[0]['id'],'type':'private'}
        await env.bot._dispatch_update({'message':msg})
        assert not env.db.query("SELECT * FROM play_jobs WHERE kind='niuniu.feedback.delete'")
    asyncio.run(run())


def test_delete_retry_uses_same_target_and_lease_survives_worker_restart(env):
    async def run():
        await env.bot._dispatch_update({'message':message(env, '/牛牛 1000001')})
        job=env.db.one("SELECT * FROM play_jobs WHERE kind='niuniu.feedback.delete'")
        target=json.loads(job['payload_json'])
        service=NiuniuFeedbackCleanup(env.db)
        claimed=service.claim('123',now=job['due_at'])
        assert claimed and service.claim('123',now=job['due_at']+1) is None
        original=env.bot._call
        async def fail(method, fields, **kwargs):
            if method=='deleteMessage' and fields['message_id']==target['message_id']:
                CALL_DELIVERY.set({'state':'retry','retry_after':5})
                return None
            return await original(method,fields,**kwargs)
        ledger=env.db.query('SELECT * FROM points_ledger')
        env.bot._call=fail
        await env.bot._drain_niuniu_feedback(now=job['due_at']+46)
        retry=env.db.one('SELECT * FROM play_jobs WHERE id=?',(job['id'],))
        assert retry['state']=='queued'
        env.bot._call=original
        await env.bot._drain_niuniu_feedback(now=retry['due_at']+1)
        assert env.db.one('SELECT state FROM play_jobs WHERE id=?',(job['id'],))['state']=='deleted'
        assert env.db.query('SELECT * FROM points_ledger')==ledger
    asyncio.run(run())
