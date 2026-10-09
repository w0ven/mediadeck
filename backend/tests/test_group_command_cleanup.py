"""Actual message dispatch and native Bot transport with an in-memory HTTP endpoint."""
import asyncio
import copy
import json
import time
from types import SimpleNamespace

import pytest
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_tg_interaction_context import ADMIN, GROUP

from app.modules.command_cleanup import CommandCleanupService
from app.modules.report_delivery import failure
from app.modules.telegram import _CALL_ERROR, TelegramBot


@pytest.fixture
def env(request):
    e = request.getfixturevalue('bw_env')
    e.services.registry.save('group_command_cleanup', enabled=True)
    e.services.registry.save('niuniu', enabled=True)
    e.services.registry.save('scratch9', enabled=True)
    e.delete_mode = 'ok'
    e.public_mode = 'ok'
    class Client:
        async def post(self, url, json=None, data=None, files=None, **kw):
            method = url.rsplit('/', 1)[1]
            payload = copy.deepcopy(json or data or {})
            for key in ('reply_markup', 'reply_parameters', 'media'):
                if isinstance(payload.get(key), str):
                    import json as codec
                    payload[key] = codec.loads(payload[key])
            mode = e.delete_mode if method == 'deleteMessage' else e.public_mode if method in ('sendPhoto', 'sendMessage') else 'ok'
            if mode != 'ok':
                e.tg.calls.append((method, payload))
                body = {'ok': False, 'error_code': 403, 'description': 'not enough rights'} if mode == 'denied' else {'ok': False, 'error_code': 429, 'description': 'retry', 'parameters': {'retry_after': 1}}
                if mode == 'gone':
                    body = {'ok': False, 'error_code': 400, 'description': 'message to delete not found'}
                return SimpleNamespace(json=lambda: body)
            result = await e.tg.call(method, payload)
            return SimpleNamespace(json=lambda: {'ok': True, 'result': result})
    async def client():
        return Client()
    e.bot._client = client
    e.bot._call = TelegramBot._call.__get__(e.bot)
    e.bot._call_multipart = TelegramBot._call_multipart.__get__(e.bot)
    return e


def msg(e, text='/牛牛', mid=1901, private=False):
    return {'chat': {'id': ADMIN if private else GROUP, 'type': 'private' if private else 'supergroup'},
            'from': e.actors[0], 'message_id': mid, 'text': text, 'message_thread_id': 88, 'is_topic_message': not private,
            'reply_to_message': {'message_id': 111, 'from': e.actors[1]}}


def jobs(e):
    return e.db.query("SELECT * FROM play_jobs WHERE kind='group.command.delete' ORDER BY id")


@pytest.mark.parametrize('text', ['/牛牛', '/刮刮乐', '/黑白板 50', '/游戏', '/积分榜', '/红包', '/transfer 10', '/kk', '/renew', '/score', '/help', '/me', '/usage', '/rules', '/签到', '/start', '/requests'])
def test_native_actual_recognized_existing_entries_only_original_after_response(env, text):
    async def run():
        await env.bot._dispatch_update({'message': msg(env, text)})
        rows = jobs(env)
        assert len(rows) == 1, (text, env.tg.calls, env.bot._last_error)
        target = json.loads(rows[0]['payload_json'])
        assert target['chat_id'] == GROUP and target['thread_id'] == 88 and target['message_id'] == 1901
        assert target['actor_tg_id'] == str(ADMIN) and rows[0]['state'] == 'deleted'
        assert not any(m == 'deleteMessage' and p['message_id'] == 111 for m, p in env.tg.calls)
        index = next(i for i, (m, p) in enumerate(env.tg.calls) if m == 'deleteMessage' and p['message_id'] == 1901)
        assert any(m in ('sendMessage', 'sendPhoto') for m, p in env.tg.calls[:index])
        await env.bot._dispatch_update({'message': msg(env, text)})
        assert len(jobs(env)) == 1
        assert sum(m == 'deleteMessage' and p['message_id'] == 1901 for m, p in env.tg.calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize('case', ['ordinary', 'unknown', 'private', 'otherbot', 'forward', 'anonymous', 'botactor', 'invalidmid', 'ungroup', 'denied_response'])
def test_native_no_delete_unprocessed_private_ordinary_unknown_reply_or_untrusted(env, case):
    async def run():
        m = msg(env, '/help')
        if case == 'ordinary':
            m['text'] = '我在正常聊天 /牛牛'
        elif case == 'unknown':
            m['text'] = '/not_a_real_command'
        elif case == 'private':
            m = msg(env, '/help', private=True)
        elif case == 'otherbot':
            m['text'] = '/牛牛@AnotherBot'
        elif case == 'forward':
            m['forward_origin'] = {'type': 'user', 'sender_user': env.actors[1]}
        elif case == 'anonymous':
            m['sender_chat'] = {'id': GROUP}
        elif case == 'botactor':
            m['from'] = {'id': 998, 'is_bot': True}
        elif case == 'invalidmid':
            m['message_id'] = True
        elif case == 'ungroup':
            m['chat']['id'] -= 1
        elif case == 'denied_response':
            env.public_mode = 'denied'
        await env.bot._dispatch_update({'message': m})
        assert not jobs(env)
        assert not any(method == 'deleteMessage' and p['message_id'] in (111, 1901) for method, p in env.tg.calls)
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['denied', 'retry', 'gone'])
def test_native_delete_permission_failure_quiet_and_exact_persistent_retry(env, mode):
    async def run():
        env.delete_mode = mode
        await env.bot._dispatch_update({'message': msg(env)})
        row = env.db.one('SELECT * FROM niuniu_rounds')
        assert row['card_message_id'] is not None and row['state'] == 'lobby'
        assert env.services.points.balance(env.uids[0]) == 960
        saved = env.db.query('SELECT * FROM points_ledger')
        job = jobs(env)[0]
        assert job['state'] == ('blocked' if mode == 'denied' else 'gone' if mode == 'gone' else 'queued')
        assert 'rights' not in env.bot._last_error.lower()
        env.delete_mode = 'ok'
        # A new service instance resumes the durable exact target, never re-runs the command.
        if mode == 'retry':
            recovered = CommandCleanupService(env.db).claim('123', now=time.time()+1000)
            target = json.loads(recovered['payload_json'])
            assert target['message_id'] == 1901 and target['chat_id'] == GROUP
            CommandCleanupService(env.db).finish(recovered, 'queued', now=time.time()-10)
            await env.bot._drain_group_commands()
            assert jobs(env)[0]['state'] == 'deleted'
            assert len([p for m, p in env.tg.calls if m == 'deleteMessage' and p['message_id'] == 1901]) == 2
        assert env.db.query('SELECT * FROM points_ledger') == saved
    asyncio.run(run())


def test_durable_lost_delete_ack_lease_bot_identity_and_no_history_scan(env):
    async def run():
        env.delete_mode = 'retry'
        await env.bot._dispatch_update({'message': msg(env, '/help')})
        service = CommandCleanupService(env.db)
        assert service.claim('999', now=time.time()+1000) is None
        claimed = service.claim('123', now=time.time()+1000)
        assert service.claim('123', now=time.time()+1001) is None
        resumed = service.claim('123', now=time.time()+1100)
        assert json.loads(claimed['payload_json']) == json.loads(resumed['payload_json'])
        service.finish(claimed, 'deleted')
        assert jobs(env)[0]['state'] == 'running'
        service.finish(resumed, 'gone')
        assert jobs(env)[0]['state'] == 'gone'
        _CALL_ERROR.set('message to delete not found')
        assert failure({'error_code': 403})['state'] == 'failed'
    asyncio.run(run())
