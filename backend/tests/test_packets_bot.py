"""Actual received Telegram update/handler chain; never live Telegram sending."""
import asyncio
import copy

import pytest
from test_economy_bot import env as economy_env  # noqa: F401
from test_group_points import msg
from test_tg_interaction_context import ADMIN, GROUP, VIEWER
from test_tg_interaction_context import env as interaction_env  # noqa: F401

from app.modules.game_ui import drain_ui
from app.modules.report_delivery import CALL_DELIVERY, failure


@pytest.fixture
def env(request):
    e = request.getfixturevalue('economy_env')
    e.services.registry.save('red_packets', enabled=True)
    e.services.points.add('u1', 4950, 'isolated.test')
    return e


async def dispatch(e, text='/红包 100 10', actor=VIEWER, mid=20, thread=7):
    message = msg(text, actor=actor, mid=mid, thread=thread)
    message.pop('reply_to_message')
    if thread:message['is_topic_message']=True
    assert 'entities' not in message
    await e.bot._dispatch_update({'message': message})
    await drain_ui(e.bot)
    row=e.db.one('SELECT * FROM red_packets WHERE command_message_id=?', (mid,))
    if row:
        # Existing expiring envelope snapshot; new semantics use test_packet_permanent.
        e.db.execute('UPDATE red_packets SET permanent=0 WHERE nonce=?',(row['nonce'],))
        row=e.db.one('SELECT * FROM red_packets WHERE nonce=?',(row['nonce'],))
    return row


def card_message(e, row):
    payload = copy.deepcopy(e.tg.message(GROUP, row['card_message_id']))
    payload.update(chat={'id': GROUP, 'type': 'supergroup'}, message_id=row['card_message_id'])
    payload['from'] = {'id': 123, 'is_bot': True}
    return payload


async def callback(e, row, kind='rpok', actor=None, suffix='', message=None):
    await e.bot._dispatch_update({'callback_query': {
        'id': 'packetcb', 'data': kind + ':' + row['nonce'] + suffix,
        'from': {'id': int(row['actor_tg_id']) if actor is None else actor, 'is_bot': False},
        'message': message or card_message(e, row)}})
    await drain_ui(e.bot)


def alert(e):
    return [p for m, p in e.tg.calls if m == 'answerCallbackQuery'][-1]


@pytest.mark.parametrize('text,mode', [('/红包 100 10', 'random'), ('/红包 等额 100 10', 'equal'),
    ('/redpacket 100 10', 'random'), ('/packet equal 100 10', 'equal'),
    ('/红包@MediaDeckDemoBot 100 10', 'random'), ('/REDPACKET@mediadeckdemobot 100 10', 'random')])
def test_received_command_options_confirm_claim_popup_progress_and_best(env, text, mode):
    async def run():
        row = await dispatch(env, text)
        assert row['mode'] == mode and row['card_message_id'] and row['thread_id'] == 7
        body = env.tg.text(GROUP, row['card_message_id'])
        assert '100 积分' in body and '10 份' in body and '确认后' in body
        assert '小时' not in body and '截止' not in body
        assert '扣除' not in body and '领取范围' not in body
        assert env.services.points.balance('u1') == 5000
        actions = env.tg.actions(GROUP, row['card_message_id'])
        assert all(len(b['callback_data'].encode()) <= 64 for b in actions)
        assert {'拼手气', '等额', '500分', '1份', '白名单', '确认发出'} <= {b['text'].replace('✓ ', '') for b in actions}
        await callback(env, row, 'rpedit', suffix=':mode:equal')
        assert env.db.one('SELECT mode FROM red_packets')['mode'] == 'equal'
        await callback(env, row, 'rpedit', suffix=':total:500')
        await callback(env, row, 'rpedit', suffix=':parts:1')
        await asyncio.gather(*[callback(env, row) for _ in range(3)])
        assert env.services.points.balance('u1') == 4500
        assert '0/1' in env.tg.text(GROUP, row['card_message_id'])
        assert 'rpclaim:' in str(env.tg.actions(GROUP, row['card_message_id']))
        await callback(env, row, 'rpclaim', actor=904)
        assert env.services.points.balance('u2') == 500
        assert alert(env)['show_alert'] and '500 积分' in alert(env)['text']
        end = env.tg.text(GROUP, row['card_message_id'])
        assert '已领完' in end and '手气最佳' in end and '成员' in end
        assert 'ViewerB' not in end  # missing TG display name must not expose account name
        assert not env.tg.actions(GROUP, row['card_message_id'])
        public = '\n'.join(p.get('text', '') for m, p in env.tg.calls if m in ('sendMessage', 'editMessageText'))
        assert '余额' not in public and 'mint' not in public and '不扣管理员' not in public
        assert not [p for m, p in env.tg.calls if m == 'sendMessage' and int(p['chat_id']) > 0]
    asyncio.run(run())


@pytest.mark.parametrize('text', ['/红包@OtherBot 100 10', '/redpacket@OtherBot 100 10', '红包 100 10', '/redpackets 100 10'])
def test_otherbot_suffix_and_chatter_do_not_issue(env, text):
    asyncio.run(dispatch(env, text))
    assert not env.db.query('SELECT * FROM red_packets')


def test_full_reward_flow_distinct_finance_and_public_copy_and_backend_audit(env):
    async def run():
        row = await dispatch(env, '/红包 等额 100 1', actor=ADMIN)
        assert row['funding'] == 'reward'
        text = env.tg.text(GROUP, row['card_message_id'])
        assert '等额红包' in text and '奖励红包' not in text and '扣除' not in text and 'mint' not in text and '不扣' not in text
        await callback(env, row)
        assert env.services.points.balance('admin') == 0
        await callback(env, row, 'rpclaim', actor=904)
        assert env.services.points.balance('u2') == 100
    asyncio.run(run())
    audits = env.db.query("SELECT * FROM audit_log WHERE action LIKE 'points.packet.reward.%'")
    assert len(audits) == 2
    assert env.services.points.ledger('u2')[0]['reason'] == 'packet.reward'


@pytest.mark.parametrize('mutation', ['other_owner', 'topic', 'message', 'forward', 'other_chat', 'expired', 'role', 'binding', 'disable'])
def test_actual_dispatch_security_refusal_never_spends_or_overwrites_card(env, mutation, monkeypatch):
    async def run():
        row = await dispatch(env)
        message = card_message(env, row)
        operator = VIEWER
        if mutation == 'other_owner':
            operator = 904
        elif mutation == 'topic':
            message['message_thread_id'] += 1
        elif mutation == 'message':
            message['message_id'] += 1
        elif mutation == 'forward':
            message['forward_origin'] = {'type': 'user'}
        elif mutation == 'other_chat':
            message['chat']['id'] -= 1
        elif mutation == 'expired':
            monkeypatch.setattr('time.time', lambda: row['confirm_by'] + 1)
        elif mutation == 'role':
            env.members.set_roles('u1', ['admin'])
        elif mutation == 'binding':
            env.members.bind_telegram('u1', '999992')
        else:
            env.services.registry.save('red_packets', enabled=False)
        before = env.tg.text(GROUP, row['card_message_id'])
        await callback(env, row, actor=operator, message=message)
        assert alert(env)['text'] and alert(env)['show_alert']
        assert env.services.points.balance('u1') == 5000
        assert env.tg.text(GROUP, row['card_message_id']) == before
    asyncio.run(run())


def test_actual_whiteonly_claim_validates_current_group_and_account(env):
    async def run():
        row = await dispatch(env, '/红包 100 1 白名单')
        await callback(env, row)
        await callback(env, row, 'rpclaim', actor=904)
        assert '白名单' in alert(env)['text'] and env.services.points.balance('u2') == 0
        env.members.upsert('u2', 'ViewerB', {'group_id': 'whitelist'})
        env.members.set_status('u2', 'suspended')
        await callback(env, row, 'rpclaim', actor=904)
        assert '有效' in alert(env)['text'] and env.services.points.balance('u2') == 0
        env.members.set_status('u2', 'active')
        await asyncio.gather(*[callback(env, row, 'rpclaim', actor=904) for _ in range(4)])
        assert env.services.points.balance('u2') == 100
        assert len(env.db.query('SELECT * FROM red_packet_claims')) == 1
    asyncio.run(run())


@pytest.mark.parametrize('unknown_ack', [False, True])
def test_initial_card_failure_or_lost_ack_is_durable_no_charge_no_blind_resend(env, unknown_ack):
    original = env.bot._call
    async def failing(method, payload=None, **kwargs):
        if method == 'sendMessage':
            if unknown_ack:
                await original(method, payload, **kwargs)
            CALL_DELIVERY.set({'state': 'unknown' if unknown_ack else 'failed'})
            return None
        return await original(method, payload, **kwargs)
    env.bot._call = failing
    async def run():
        row = await dispatch(env)
        assert not row['card_message_id'] and row['card_send_state'] == ('unknown' if unknown_ack else 'failed')
        before = len(env.tg.messages)
        await dispatch(env)
        assert len(env.tg.messages) == before and env.services.points.balance('u1') == 5000
        if unknown_ack:
            actualmid = max(mid for (chat, mid) in env.tg.messages if chat == str(GROUP))
            actual = dict(row, card_message_id=actualmid)
            await callback(env, actual)
            assert '原Bot' in alert(env)['text'] and env.services.points.balance('u1') == 5000
    asyncio.run(run())


def test_publish_failure_duplicate_confirm_and_restart_worker_same_message_only(env, monkeypatch):
    async def run():
        row = await dispatch(env)
        env.tg.edit_error = 'isolated temporary failure'
        await callback(env, row)
        await callback(env, row)
        assert env.services.points.balance('u1') == 4900
        failed = env.db.one('SELECT * FROM red_packets')
        assert failed['publish_error'] and failed['rendered_version'] < failed['render_version']
        # Failed publish keeps the original confirmation buttons, cannot claim.
        await callback(env, row, 'rpclaim', actor=904)
        assert '领取按钮' in alert(env)['text'] and env.services.points.balance('u2') == 0
        env.tg.edit_error = ''
        monkeypatch.setattr('time.time', lambda: failed['next_publish_at'])
        await env.bot._packet_tick()  # same component used after a process restart
        assert 'rpclaim:' in str(env.tg.actions(GROUP, row['card_message_id']))
        await callback(env, row, 'rpclaim', actor=904)
        assert env.services.points.balance('u2') >= 1
        assert len([p for m, p in env.tg.calls if m == 'sendMessage' and p['chat_id'] == GROUP]) == 1
        end = env.db.one('SELECT * FROM red_packets')
        env.services.registry.save('red_packets', enabled=False)
        monkeypatch.setattr('time.time', lambda: end['expires_at'] + 1)
        await env.bot._packet_tick()  # issuing switch off must not orphan escrow
        final = env.db.one('SELECT * FROM red_packets')
        assert final['status'] == 'expired'
        assert env.services.points.balance('u1') + env.services.points.balance('u2') == 5000
        assert '已过期' in env.tg.text(GROUP, row['card_message_id'])
        assert '已退回' not in env.tg.text(GROUP, row['card_message_id'])
    asyncio.run(run())


def test_ack_lost_but_edit_received_then_not_modified_converges_and_never_reserves_twice(env):
    original = env.bot._call
    edits = [0]
    async def lost_ack(method, payload=None, **kwargs):
        if method == 'editMessageText':
            edits[0] += 1
            if edits[0] == 1:
                await original(method, payload, **kwargs)
                CALL_DELIVERY.set({'state': 'unknown'})
            else:
                CALL_DELIVERY.set(failure({'error_code': 400, 'description': 'Bad Request: message is not modified'}))
            return None
        return await original(method, payload, **kwargs)
    env.bot._call = lost_ack
    async def run():
        row = await dispatch(env)
        await callback(env, row)
        assert env.db.one('SELECT * FROM red_packets')['publish_error']
        await callback(env, row)
        final = env.db.one('SELECT * FROM red_packets')
        assert final['rendered_version'] == final['render_version'] and not final['publish_error']
        assert env.services.points.balance('u1') == 4900
        assert 'rpclaim:' in str(env.tg.actions(GROUP, row['card_message_id']))
    asyncio.run(run())


def test_worker_lifecycle_settles_even_bot_disabled_and_stop_cancels(env, monkeypatch):
    async def run():
        row = await dispatch(env)
        await callback(env, row)
        row = env.db.one('SELECT * FROM red_packets')
        monkeypatch.setattr('time.time', lambda: row['expires_at'] + 1)
        env.cfg['enabled'] = False
        env.bot.start()
        await asyncio.sleep(0.05)
        assert env.db.one('SELECT status FROM red_packets')['status'] == 'expired'
        assert env.services.points.balance('u1') == 5000
        await env.bot.stop()
        assert not env.bot._in_flight
    asyncio.run(run())
