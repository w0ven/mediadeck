"""Actual scratch handlers in isolated SQLite; never production TG sends/bets."""
import asyncio
import copy
import time

import pytest
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_scratch9 import card, create, msg, select, svc
from test_scratch9 import env as scratch_env  # noqa: F401
from test_tg_interaction_context import GROUP

from app.modules.report_delivery import CALL_DELIVERY


@pytest.fixture
def env(request):
    return request.getfixturevalue('scratch_env')


async def click(e, row, cell=1, index=1, request='click', message=None, actor=None, data=None):
    await e.bot._dispatch_update({'callback_query': {'id': request,
        'data': data or f'gg:{row["nonce"]}:{cell}', 'from': actor or e.actors[index],
        'message': message or card(e, svc(e).get(row['nonce']))}})
    replies = answers(e, request)
    assert len(replies) == 1 and replies[0].get('show_alert') is True and replies[0]['text']
    assert len(replies[0]['text']) <= 200
    return replies[0]['text']


def answers(e, request):
    return [p for m, p in e.tg.calls if m == 'answerCallbackQuery' and p['callback_query_id'] == request]


def test_first_visible_callback_explains_group_confirmation_without_private_send(env):
    async def run():
        row = await create(env)
        before = copy.deepcopy(env.tg.message(GROUP, row['card_message_id']))
        ledger = env.db.query('SELECT * FROM points_ledger')
        await select(env, row, index=1, request='first-visible')
        replies = answers(env, 'first-visible')
        assert len(replies) == 1 and '再次' in replies[0]['text'] and '30' in replies[0]['text']
        assert replies[0]['show_alert'] is True
        assert not [(m, p) for m, p in env.tg.calls if m == 'sendMessage']
        assert env.tg.message(GROUP, row['card_message_id']) == before
        assert env.db.query('SELECT * FROM points_ledger') == ledger and not svc(env).cells(row['nonce'])
    asyncio.run(run())


def test_duplicate_start_new_command_has_original_card_link_and_keeps_cleanup_feedback(env):
    async def run():
        env.services.registry.save('group_command_cleanup', enabled=True)
        row = await create(env)
        env.tg.calls.clear()
        await env.bot._dispatch_update({'message': msg(env, mid=1702, topic=99)})
        notices = [p for m, p in env.tg.calls if m == 'sendMessage']
        assert len(notices) == 1 and '已有' in notices[0]['text'] and '不重复开场' in notices[0]['text']
        assert f'https://t.me/c/700/{row["card_message_id"]}' in notices[0]['text']
        assert notices[0]['message_thread_id'] == 99
        assert env.db.one('SELECT COUNT(*) n FROM scratch9_rounds')['n'] == 1
        await env.bot._drain_group_commands()
        deletes = [p for m, p in env.tg.calls if m == 'deleteMessage']
        assert any(p['message_id'] == 1702 for p in deletes)
        assert not any(p['message_id'] in (row['card_message_id'], 1703) for p in deletes)
    asyncio.run(run())


@pytest.mark.parametrize('draw,prize', [(0, 0), (999999, 888)])
def test_two_group_clicks_exactly_once_repeat_update_vs_new_click_and_no_public_personal_state(env, monkeypatch, draw, prize):
    monkeypatch.setattr('secrets.randbelow', lambda n: draw)
    async def run():
        row = await create(env)
        mid = row['card_message_id']
        public = copy.deepcopy(env.tg.message(GROUP, mid))
        ledger = env.db.query('SELECT * FROM points_ledger')
        assert '同一格点击两次' in public['caption'] and '30' in public['caption']
        assert '再次' in await click(env, row, request='first')
        # Re-delivery of the SAME Telegram callback is not a second click.
        env.tg.calls.clear()
        assert '再次' in await click(env, row, request='first')
        assert env.db.query('SELECT * FROM points_ledger') == ledger
        assert env.tg.message(GROUP, mid) == public
        assert len(env.db.query('SELECT * FROM scratch9_intents')) == 1
        assert '获得'+str(prize)+'积分' in await click(env, row, request='second')
        entries = env.db.query("SELECT * FROM points_ledger WHERE reason LIKE 'scratch9.%'")
        assert [r['delta'] for r in entries] == ([-30, prize] if prize else [-30])
        ledger = env.db.query('SELECT * FROM points_ledger')
        env.tg.calls.clear()
        assert '这个格子已被刮开' in await click(env, row, request='second')
        assert '这个格子已被刮开' in await click(env, row, request='third-new-click')
        assert '本场已参与' in await click(env, row, cell=2, request='other-cell')
        assert env.db.query('SELECT * FROM points_ledger') == ledger
        assert svc(env).get(row['nonce'])['card_message_id'] == mid
        assert len(svc(env).cells(row['nonce'])) == 1
        assert not any(m in ('sendMessage', 'editMessageText') for m, _ in env.tg.calls)
        for secret in ('概率', '%', '余额', 'private-login'):
            assert secret not in env.tg.text(GROUP, mid)
    asyncio.run(run())


def test_switch_cells_cancels_old_intent_and_expiry_rearms_without_spending_or_reserving(env):
    async def run():
        row = await create(env)
        before = env.db.query('SELECT * FROM points_ledger')
        await click(env, row, request='pick-1')
        old = env.db.one("SELECT * FROM scratch9_intents WHERE source_request='pick-1'")
        await click(env, row, cell=2, request='pick-2')
        assert env.db.one('SELECT state FROM scratch9_intents WHERE token=?', (old['token'],))['state'] == 'cancelled'
        # Returning to cell 1 is a NEW first click, not permission to debit.
        assert '再次' in await click(env, row, request='return-1')
        env.db.execute("UPDATE scratch9_intents SET expires_at=? WHERE state='group_pending'", (time.time()-1,))
        assert '确认已过期' in await click(env, row, request='expired-reclick')
        assert not svc(env).cells(row['nonce']) and env.db.query('SELECT * FROM points_ledger') == before
        # Another player can take the unreserved cell while our intent waits.
        await click(env, row, index=2, request='competitor-first')
        assert '已刮开' in await click(env, row, index=2, request='competitor-second')
        before = env.db.query('SELECT * FROM points_ledger')
        assert '这个格子已被刮开' in await click(env, row, request='lost-race')
        assert env.db.query('SELECT * FROM points_ledger') == before
    asyncio.run(run())


@pytest.mark.parametrize('phase', ['first', 'second'])
def test_insufficient_alert_uses_round_cost_snapshot_not_live_config_or_public_balance(env, phase):
    async def run():
        env.services.registry.save('scratch9', config={'cost': 47})
        row = await create(env)
        if phase == 'second':
            await click(env, row, request='funded-first')
        env.services.registry.save('scratch9', config={'cost': 99})
        env.services.points.add(env.uids[1], 46-env.services.points.balance(env.uids[1]), 'isolated.balance')
        ledger = env.db.query('SELECT * FROM points_ledger')
        public = copy.deepcopy(env.tg.message(GROUP, row['card_message_id']))
        assert '积分不足，本次需要47积分' in await click(env, row, request='insufficient')
        assert env.db.query('SELECT * FROM points_ledger') == ledger and not svc(env).cells(row['nonce'])
        assert env.tg.message(GROUP, row['card_message_id']) == public
        assert not any(m == 'sendMessage' for m, _ in env.tg.calls)
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['deadline', 'closed', 'disabled', 'unbound', 'rebind', 'qualified', 'intent_expired', 'invalid', 'missing'])
def test_every_refusal_first_and_only_answer_is_visible_alert_no_charge(env, failure):
    async def run():
        row = await create(env)
        await click(env, row, request='prepare-first')
        actor = env.actors[1]
        data = None
        expected = ''
        if failure == 'deadline':
            env.db.execute('UPDATE scratch9_rounds SET expires_at=? WHERE nonce=?', (time.time()-1, row['nonce']))
            expected = '结束'
        elif failure == 'closed':
            env.db.execute("UPDATE scratch9_rounds SET state='closed' WHERE nonce=?", (row['nonce'],))
            expected = '结束'
        elif failure == 'disabled':
            env.services.registry.save('scratch9', enabled=False)
            expected = '暂停'
        elif failure == 'unbound':
            actor = {'id': 7654321, 'is_bot': False, 'first_name': '未绑定'}
            expected = '有效绑定'
        elif failure == 'rebind':
            env.members.bind_telegram(env.uids[1], '12000')
            expected = '有效绑定'
        elif failure == 'qualified':
            env.members.upsert(env.uids[1], 'private-login-1', {'expires_at': int(time.time())-1})
            expected = '有效绑定'
        elif failure == 'intent_expired':
            env.db.execute("UPDATE scratch9_intents SET expires_at=?", (time.time()-1,))
            expected = '确认已过期'
        elif failure == 'invalid':
            data = f'gg:{row["nonce"]}:bad'
            expected = '格子无效'
        elif failure == 'missing':
            data = 'gg:nonexistent:1'
            expected = '不存在'
        ledger = env.db.query('SELECT * FROM points_ledger')
        assert expected in await click(env, row, actor=actor, data=data, request='refused')
        assert env.db.query('SELECT * FROM points_ledger') == ledger and not svc(env).cells(row['nonce'])
    asyncio.run(run())


@pytest.mark.parametrize('fault', ['chat', 'private', 'topic', 'mid', 'sender', 'forward', 'actorbot'])
def test_group_confirmation_actual_handler_original_identity_message_topic_reject_forgery(env, fault):
    async def run():
        row = await create(env)
        await click(env, row, request='legitimate-first')
        message = card(env, row)
        actor = env.actors[1]
        if fault == 'chat': message['chat']['id'] -= 1
        elif fault == 'private': message['chat'] = {'id': actor['id'], 'type': 'private'}
        elif fault == 'topic': message['message_thread_id'] += 1
        elif fault == 'mid': message['message_id'] += 1
        elif fault == 'sender': message['from']['id'] += 1
        elif fault == 'forward': message['forward_origin'] = {'type': 'channel'}
        elif fault == 'actorbot': actor = {**actor, 'is_bot': True}
        before = env.db.query('SELECT * FROM points_ledger')
        # Non-authorized group and unreliable actor are rejected by the native
        # outer dispatcher. Test the actual scratch handler too, with its alert.
        if fault in ('chat', 'actorbot'):
            await env.bot._scratch9_callback(f'gg:{row["nonce"]}:1', message, actor, 'forged')
            reply = answers(env, 'forged')
            assert len(reply) == 1 and reply[0]['text'] and reply[0]['show_alert'] is True
        else:
            text = await click(env, row, message=message, actor=actor, request='forged')
            assert any(term in text for term in ('原', '授权', '本人'))
        assert env.db.query('SELECT * FROM points_ledger') == before and not svc(env).cells(row['nonce'])
    asyncio.run(run())


def test_internal_error_visible_without_details_and_rolls_back_fee_then_real_retry(env, monkeypatch):
    async def run():
        row = await create(env)
        await click(env, row, request='first')
        before = env.db.query('SELECT * FROM points_ledger')
        def broken(n):
            raise RuntimeError('sensitive-debug-detail-must-not-leak')
        monkeypatch.setattr('secrets.randbelow', broken)
        text = await click(env, row, request='broken-second')
        assert '暂未完成' in text and 'sensitive' not in text
        assert env.db.query('SELECT * FROM points_ledger') == before and not svc(env).cells(row['nonce'])
        monkeypatch.setattr('secrets.randbelow', lambda n: 0)
        assert '已刮开' in await click(env, row, request='retry-new-click')
        assert env.db.one("SELECT COUNT(*) n FROM points_ledger WHERE reason='scratch9.cost'")['n'] == 1
    asyncio.run(run())


def test_old_private_confirmation_callback_cannot_charge_or_send_private_message(env):
    async def run():
        row = await create(env)
        await click(env, row, request='old-first')
        intent = env.db.one("SELECT * FROM scratch9_intents WHERE source_request='old-first'")
        env.db.execute("UPDATE scratch9_intents SET state='pending',message_id=1877 WHERE token=?", (intent['token'],))
        private = {'chat': {'id': env.actors[1]['id'], 'type': 'private'}, 'message_id': 1877,
                   'from': {'id': 123, 'is_bot': True}, 'reply_markup': {'inline_keyboard': [[{'callback_data': f'ggc:{intent["token"]}:yes'}]]}}
        ledger = env.db.query('SELECT * FROM points_ledger')
        assert '原群原卡' in await click(env, row, message=private, data=f'ggc:{intent["token"]}:yes', request='old-private')
        assert env.db.query('SELECT * FROM points_ledger') == ledger and not svc(env).cells(row['nonce'])
        assert not any(m == 'sendMessage' for m, _ in env.tg.calls)
        assert '再次' in await click(env, row, request='new-group-first')
        assert '已刮开' in await click(env, row, request='new-group-second')
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['unknown', 'retry'])
def test_duplicate_start_initial_unconfirmed_notice_no_blind_resend_and_retry_has_refreshed_link(env, failure):
    async def run():
        original = env.bot._call_multipart
        async def lost(method, fields, files, **kw):
            env.media.append((method, fields))
            CALL_DELIVERY.set({'state': failure})
        env.bot._call_multipart = lost
        row = await create(env)
        assert row['card_message_id'] is None
        env.tg.calls.clear()
        env.bot._call_multipart = original
        env.db.execute('UPDATE scratch9_rounds SET next_publish_at=0 WHERE nonce=?', (row['nonce'],))
        await env.bot._dispatch_update({'message': msg(env, mid=1702, topic=99)})
        notice = [p for m, p in env.tg.calls if m == 'sendMessage'][-1]
        assert '已有' in notice['text'] and '不重复开场' in notice['text']
        assert 'reply_parameters' not in notice
        current = svc(env).get(row['nonce'])
        if failure == 'unknown':
            assert current['card_message_id'] is None and len(env.media) == 1 and '尚未确认' in notice['text']
        else:
            assert current['card_message_id'] is not None and f'/700/{current["card_message_id"]}' in notice['text']
        assert env.db.one('SELECT COUNT(*) n FROM scratch9_rounds')['n'] == 1
        assert not env.db.query("SELECT * FROM points_ledger WHERE reason LIKE 'scratch9.%'")
    asyncio.run(run())


def test_cancel_between_selection_and_confirm_is_visible_and_no_charge(env, monkeypatch):
    from app.modules.scratch9 import Scratch9Service
    original = Scratch9Service.confirm
    def cancel(self, token, actor, message, action, **kw):
        return original(self, token, actor, message, 'no', **kw)
    async def run():
        row = await create(env)
        await click(env, row, request='first')
        before = env.db.query('SELECT * FROM points_ledger')
        monkeypatch.setattr(Scratch9Service, 'confirm', cancel)
        assert '确认已取消' in await click(env, row, request='second')
        assert env.db.query('SELECT * FROM points_ledger') == before and not svc(env).cells(row['nonce'])
        assert len(env.media) == 1
    asyncio.run(run())
