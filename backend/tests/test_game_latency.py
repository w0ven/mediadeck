"""Real dispatch + real SQLite, synthetic network gates: ACK/order/render bounds."""
import asyncio
import copy
import json
import statistics
import threading
import time
from types import MethodType

import pytest
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_niuniu import card, create, svc
from test_niuniu import env as nn_fixture  # noqa: F401
from test_packet_permanent import card as packet_card
from test_packet_permanent import env as packet_fixture  # noqa: F401
from test_packet_permanent import issue
from test_tg_interaction_context import GROUP

from app.modules.game_ui import MAX_PENDING, defer_ui, drain_ui, render_image
from app.modules.niuniu_image import render_room
from app.modules.packet_delivery import receipt_view


@pytest.fixture
def nn_env(request):return request.getfixturevalue('nn_fixture')


@pytest.fixture
def packet_env(request):return request.getfixturevalue('packet_fixture')


def nn_update(e, row, op, actor, callback_id, original):
    return {'callback_query': {'id': callback_id, 'data': f'nn:{row["nonce"]}:{op}',
                              'from': e.actors[actor], 'message': original}}


def test_niuniu_upload_does_not_hold_next_ack_and_old_image_cannot_overwrite_final(nn_env):
    async def run():
        e = nn_env
        row = await create(e)
        original = card(e, row)
        entered, release = asyncio.Event(), asyncio.Event()
        transport, trace = e.bot._call_multipart, []
        async def slow(method, fields, files, **kw):
            if method == 'editMessageMedia':
                trace.append('old.start');entered.set();await release.wait();trace.append('old.done')
            else:trace.append('result')
            return await transport(method, fields, files, **kw)
        e.bot._call_multipart = slow
        await e.bot._dispatch_update(nn_update(e, row, 'join', 1, 'first', original))
        await asyncio.wait_for(entered.wait(), 3)
        started = time.perf_counter()
        await asyncio.wait_for(e.bot._dispatch_update(nn_update(e, row, 'start', 0, 'second', original)), .3)
        elapsed = time.perf_counter()-started
        answer = next(p for m, p in e.tg.calls if m == 'answerCallbackQuery' and p['callback_query_id'] == 'second')
        assert answer['show_alert'] is False and not release.is_set()
        assert svc(e).get(row['nonce'])['state'] == 'settled'
        ledger = copy.deepcopy(e.db.query('SELECT * FROM points_ledger'))
        await e.bot._dispatch_update(nn_update(e, row, 'start', 0, 'duplicate', original))
        assert e.db.query('SELECT * FROM points_ledger') == ledger
        release.set();await drain_ui(e.bot)
        current = svc(e).get(row['nonce'])
        assert current['result_state'] == 'sent' and current['rendered_revision'] == current['revision']
        assert e.tg.actions(GROUP, current['card_message_id']) == []
        assert current['card_delete_state'] == 'deleted'
        assert trace == ['old.start', 'old.done', 'result']
        assert e.db.query('SELECT * FROM points_ledger') == ledger
        print(f'NN ACK while upload gated: {elapsed*1000:.2f} ms')
    asyncio.run(run())


@pytest.mark.parametrize('serial_wait_control', [True, False])
def test_same_real_handler_latency_serial_wait_control_vs_deferred(nn_env, serial_wait_control):
    async def run():
        e = nn_env
        row = await create(e)
        original = card(e, row)
        network = e.bot._call_multipart
        async def delayed(method, fields, files, **kw):
            await asyncio.sleep(.4)
            return await network(method, fields, files, **kw)
        e.bot._call_multipart = delayed
        # Single-variable control restores waiting for UI inside the chat lock.
        # Business handler/SQLite/render/network are identical in both cases.
        handler = e.bot._niuniu_callback
        if serial_wait_control:
            async def waiting(self, *args):
                await handler(*args)
                await drain_ui(self)
            e.bot._niuniu_callback = MethodType(waiting, e.bot)
        call = e.bot._call
        answer_at = {}
        start = time.perf_counter()
        async def measured(method, payload, **kw):
            if method == 'answerCallbackQuery':answer_at[payload['callback_query_id']] = time.perf_counter()-start
            return await call(method, payload, **kw)
        e.bot._call = measured
        await asyncio.gather(*(e.bot._dispatch_update(nn_update(e, row, 'join', i, f'cb{i}', original)) for i in (1, 2)))
        await drain_ui(e.bot)
        assert len(svc(e).players(row['nonce'])) == 3
        if serial_wait_control:assert answer_at['cb2'] >= .4
        else:assert answer_at['cb2'] < .3
        print('Same-handler serial-control' if serial_wait_control else 'Same-handler deferred', json.dumps({key: round(value*1000, 2) for key, value in answer_at.items()}))
    asyncio.run(run())


def test_packet_edit_does_not_hold_next_claim_ack_and_final_keeps_order(packet_env):
    async def run():
        e = packet_env
        row = await issue(e, parts=2)
        original = packet_card(e, row)
        entered, release = asyncio.Event(), asyncio.Event()
        call = e.bot._call
        async def gated(method, payload, **kw):
            if method == 'editMessageText' and payload['message_id'] == row['card_message_id'] and not entered.is_set():
                entered.set();await release.wait()
            return await call(method, payload, **kw)
        e.bot._call = gated
        def update(i):return {'callback_query': {'id': f'claim-{i}', 'data': 'rpclaim:'+row['nonce'], 'from': e.actors[i], 'message': original}}
        await e.bot._dispatch_update(update(0))
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(e.bot._dispatch_update(update(2)), .3)
        assert any(m == 'answerCallbackQuery' and p['callback_query_id'] == 'claim-2' for m, p in e.tg.calls)
        assert e.bot._packet_service().get(row['nonce'])['status'] == 'exhausted'
        release.set();await drain_ui(e.bot)
        current = e.bot._packet_service().get(row['nonce'])
        assert current['receipt_state'] == 'sent' and current['rendered_version'] == current['render_version']
        assert e.tg.actions(GROUP, current['card_message_id']) == []
        assert e.db.one('SELECT COUNT(*) n FROM red_packet_claims')['n'] == 2
    asyncio.run(run())


def test_old_packet_result_page_answers_before_edit(packet_env):
    async def run():
        e = packet_env
        row = await issue(e, parts=1)
        from test_packet_permanent import press
        row = await press(e, row, 'rpclaim', 2)
        # Synthetic already-sent legacy paginated receipt; no new money operation.
        for i in range(1, 12):
            e.db.execute('INSERT INTO red_packet_claims(nonce,user_id,tg_user_id,amount,slot,claimed_at,display_name) VALUES(?,?,?,?,?,?,?)',
                         (row['nonce'], f'page-fixture-{i}', str(5000+i), 10, i, 1, f'玩家{i}'))
        e.db.execute('UPDATE red_packets SET parts=12,claimed_count=12 WHERE nonce=?', (row['nonce'],))
        row = e.bot._packet_service().get(row['nonce'])
        view = receipt_view(row, e.bot._packet_service().claims(row['nonce'], 12))
        e.tg.message(GROUP, row['receipt_message_id']).update(view)
        entered, release = asyncio.Event(), asyncio.Event()
        call, trace = e.bot._call, []
        async def gated(method, payload, **kw):
            if method == 'answerCallbackQuery':trace.append('answer')
            if method == 'editMessageText':
                trace.append('edit');entered.set();await release.wait()
            return await call(method, payload, **kw)
        e.bot._call = gated
        await asyncio.wait_for(e.bot._dispatch_update({'callback_query': {'id': 'page', 'data': f'rpresult:{row["nonce"]}:1', 'from': e.actors[0], 'message': packet_card(e, row, receipt=True)}}), .3)
        await asyncio.wait_for(entered.wait(), 2)
        assert trace == ['answer', 'edit']
        release.set();await drain_ui(e.bot)
    asyncio.run(run())


def test_pure_render_heartbeat_font_cache_and_thread_limit(nn_env):
    async def run():
        e = nn_env
        row = {'config_json': '{"game":"niuniu-banker-v1"}', 'state': 'settled', 'stake': 10, 'actor_user_id': 'fixture-0'}
        players = [{'user_id': f'fixture-{i}', 'display_name': f'测试玩家{i}', 'cards_json': json.dumps(list(range(i*5, (i+1)*5))), 'result_amount': 20} for i in range(5)]
        gaps, alive = [], True
        async def beat():
            previous = time.perf_counter()
            while alive:
                await asyncio.sleep(.005)
                now = time.perf_counter();gaps.append(now-previous);previous = now
        ticker = asyncio.create_task(beat())
        await asyncio.sleep(.02)
        samples = []
        for _ in range(4):
            start = time.perf_counter();await render_image(e.bot, render_room, row, players);samples.append(time.perf_counter()-start)
        alive = False;await ticker
        assert len(gaps) > 8 and max(gaps) < sum(samples)*.5
        main_thread, seen, active, peak = threading.get_ident(), [], 0, 0
        guard = threading.Lock()
        def controlled():
            nonlocal active, peak
            with guard:active += 1;peak = max(peak, active);seen.append(threading.get_ident())
            time.sleep(.04)
            with guard:active -= 1
            return b'fixture'
        await asyncio.gather(*(render_image(e.bot, controlled) for _ in range(8)))
        assert peak == 2 and main_thread not in seen
        from app.modules.poker_image import _font, font
        assert font(24) is font(24) and _font.cache_info().currsize <= 64
        print(f'Render median={statistics.median(samples)*1000:.2f} ms heartbeat_max={max(gaps)*1000:.2f} ms threads_peak={peak}')
    asyncio.run(run())


def test_ui_queue_bounded_and_full_queue_keeps_durable_terminal_for_tick(nn_env, monkeypatch):
    async def run():
        e = nn_env
        release = asyncio.Event()
        async def waiting():await release.wait()
        for i in range(MAX_PENDING+20):defer_ui(e.bot, ('fixture', str(i)), waiting)
        assert len(e.bot._game_ui['pending']) <= MAX_PENDING
        assert len(e.bot._game_ui['workers']) <= 2
        release.set();await drain_ui(e.bot)
        row = await create(e)
        original = card(e, row)
        from test_niuniu import click
        row = await click(e, row, 'join', 1)
        monkeypatch.setattr('app.modules.game_ui.MAX_PENDING', 0)
        await e.bot._dispatch_update(nn_update(e, row, 'start', 0, 'full-queue', original))
        current = svc(e).get(row['nonce'])
        assert current['state'] == 'settled' and current['result_state'] == 'pending'
        ledger = copy.deepcopy(e.db.query('SELECT * FROM points_ledger'))
        await e.bot._niuniu_tick()
        assert svc(e).get(row['nonce'])['result_state'] == 'sent'
        assert e.db.query('SELECT * FROM points_ledger') == ledger
    asyncio.run(run())


def test_menu_network_outside_click_lock_retains_permission_and_cache(nn_env):
    async def run():
        e = nn_env
        row = await create(e)
        original = card(e, row)
        entered, release = asyncio.Event(), asyncio.Event()
        call = e.bot._call
        async def gated(method, payload, **kw):
            if method == 'setMyCommands' and payload.get('scope', {}).get('user_id') == e.actors[1]['id']:
                entered.set();await release.wait()
            return await call(method, payload, **kw)
        e.bot._call = gated
        await e.bot._dispatch_update(nn_update(e, row, 'join', 1, 'menu-first', original))
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.wait_for(e.bot._dispatch_update(nn_update(e, row, 'join', 2, 'menu-second', original)), .3)
        assert any(m == 'answerCallbackQuery' and p['callback_query_id'] == 'menu-second' for m, p in e.tg.calls)
        e.cfg['group_interaction_chats'] = []
        release.set();await drain_ui(e.bot)
        scopes = [p for m, p in e.tg.calls if m == 'setMyCommands' and p.get('scope', {}).get('type') == 'chat_member']
        assert not any(p['scope']['user_id'] == e.actors[2]['id'] for p in scopes)
        user_commands = [p for p in scopes if p['scope']['user_id'] == e.actors[1]['id']]
        assert len(user_commands) == 1
        assert not any(c['command'] == 'manage' for c in user_commands[0]['commands'])
        e.cfg['group_interaction_chats'] = [str(GROUP)]
        await e.bot._dispatch_update(nn_update(e, row, 'join', 1, 'menu-cached', original))
        await drain_ui(e.bot)
        assert sum(m == 'setMyCommands' and p.get('scope', {}).get('user_id') == e.actors[1]['id'] for m, p in e.tg.calls) == 1
    asyncio.run(run())


def test_stop_after_external_accept_leaves_unknown_without_resend_or_redeal(nn_env):
    async def run():
        e = nn_env
        row = await create(e)
        from test_niuniu import click
        row = await click(e, row, 'join', 1)
        original = card(e, row)
        transport = e.bot._call_multipart
        accepted, never = asyncio.Event(), asyncio.Event()
        async def lost(method, fields, files, **kw):
            response = await transport(method, fields, files, **kw)
            if method == 'sendPhoto' and '本局战报' in fields.get('caption', ''):
                accepted.set();await never.wait()
            return response
        e.bot._call_multipart = lost
        await e.bot._dispatch_update(nn_update(e, row, 'start', 0, 'lost-ack', original))
        await asyncio.wait_for(accepted.wait(), 3)
        ledger = copy.deepcopy(e.db.query('SELECT * FROM points_ledger'))
        await e.bot.stop()
        assert svc(e).get(row['nonce'])['result_state'] == 'sending'
        e.db.execute('UPDATE niuniu_rounds SET result_lease=0 WHERE nonce=?', (row['nonce'],))
        await e.bot._niuniu_tick()
        assert svc(e).get(row['nonce'])['result_state'] == 'unknown'
        assert sum(m == 'sendPhoto' and '本局战报' in p.get('caption', '') for m, p in e.photos) == 1
        assert e.db.query('SELECT * FROM points_ledger') == ledger
    asyncio.run(run())


def test_cancel_before_api_send_is_known_retry_not_unknown(nn_env, monkeypatch):
    async def run():
        e = nn_env
        row = await create(e)
        from test_niuniu import click
        row = await click(e, row, 'join', 1)
        original = card(e, row)
        entered = threading.Event()
        def rendering(snapshot, players):
            entered.set();time.sleep(.1)
            return render_room(snapshot, players)
        monkeypatch.setattr('app.modules.bot_niuniu.render_room', rendering)
        await e.bot._dispatch_update(nn_update(e, row, 'start', 0, 'cancel-before-send', original))
        for _ in range(100):
            if entered.is_set():break
            await asyncio.sleep(.005)
        assert entered.is_set()
        ledger = copy.deepcopy(e.db.query('SELECT * FROM points_ledger'))
        await e.bot.stop()
        assert svc(e).get(row['nonce'])['result_state'] == 'retry'
        assert not any(m == 'sendPhoto' and '本局战报' in p.get('caption', '') for m, p in e.photos)
        e.db.execute('UPDATE niuniu_rounds SET result_due=0 WHERE nonce=?', (row['nonce'],))
        await e.bot._niuniu_tick()
        assert svc(e).get(row['nonce'])['result_state'] == 'sent'
        assert e.db.query('SELECT * FROM points_ledger') == ledger
    asyncio.run(run())
