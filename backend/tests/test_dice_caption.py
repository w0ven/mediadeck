"""Single-message captions, exact paging, durable delivery and legacy coexistence."""
import asyncio
import copy
import json
import re

import pytest
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_dice import create, message, svc
from test_dice_size_group import bet, click, refresh, reply, send
from test_tg_interaction_context import GROUP

from app.modules.dice_art import art_bytes
from app.modules.dice_caption_view import card, is_caption, pages
from app.modules.game_mentions import text_units
from app.modules.report_delivery import CALL_DELIVERY


@pytest.fixture
def env(request, monkeypatch):
    e = request.getfixturevalue('bw_env')
    e.services.registry.save('dice', enabled=True)
    e.services.registry.get('dice').ctx.telegram = e.bot
    from dice_legacy import legacy_group_create

    from app.modules.dice import DiceService
    monkeypatch.setattr(DiceService, 'create', legacy_group_create)
    return e


def numbers(body):
    return [int(n) for n in re.findall(r'#(\d+)</code>', body)]


def test_open_and_updates_are_one_photo_without_extra_controls(env):
    async def run():
        uploads = []
        real = env.bot._call_multipart
        async def capture(method, payload, files, **kw):
            uploads.append((copy.deepcopy(payload), files))
            return await real(method, payload, files, **kw)
        env.bot._call_multipart = capture
        row = await create(env)
        assert is_caption(row)
        assert uploads[0][1]['photo'][1] == art_bytes('covered')
        assert uploads[0][0]['message_thread_id'] == row['thread_id']
        assert env.tg.actions(GROUP, row['card_mid']) == []
        assert not [m for m, _ in env.tg.calls if m == 'sendMessage']
        assert [j['purpose'] for j in env.db.query('SELECT * FROM dice_messages')] == ['open']
        await send(env, message(env, '/大 100', mid=8100))
        await send(env, bet(env, row, '/小 20', mid=8101))
        await refresh(env, row)
        body = env.tg.text(GROUP, row['card_mid'])
        assert numbers(body) == [2, 1]
        assert body.count('完整流水') == 1 and '最新入场' not in body
        assert 'tg://user?id=' in body and text_units(body) <= 1024
        edits = [(m, p) for m, p in env.tg.calls if m.startswith('editMessage')]
        assert edits and all(m == 'editMessageCaption' for m, _ in edits)
        assert all(p['message_id'] == row['card_mid'] for _, p in edits)
        assert len(uploads) == 1 and env.services.points.balance(env.uids[0]) == 880
    asyncio.run(run())


@pytest.mark.parametrize('terminal', [False, True])
def test_utf16_dynamic_pages_cover_all_original_tickets_and_net_results(terminal):
    tickets = [{'tg_id': str(10000+i), 'user_id': str(i), 'display_name': '🎲'*38+'<&',
                'items': json.dumps([{'option': '大' if i % 2 else '小'}]),
                'amount': 99999999999999} for i in range(73)]
    row = {'rules': '{"presentation":"caption-v1"}', 'nonce': 'a'*24,
           'state': 'settled' if terminal else 'open', 'deadline': 1791749123,
           'dice': '[2,3,6]' if terminal else '[]', 'group_page': -1,
           'result': json.dumps({'size': {'users': {str(i): {'net': i-36} for i in range(73)}}})}
    snapshot = {'tickets': list(reversed(tickets)), 'totals': [
        {'option': '大', 'people': 36, 'amount': 99999999999999*36},
        {'option': '小', 'people': 37, 'amount': 99999999999999*37}]}
    bodies = pages(row, snapshot)
    assert all(text_units(body) <= 1024 for body in bodies)
    assert [n for body in bodies for n in numbers(body)] == list(range(73, 0, -1))
    assert max(len(numbers(body)) for body in bodies) <= 8
    if not terminal:
        assert max(len(numbers(body)) for body in bodies) < 8  # UTF-16 budget reduces page size
    assert all('最新入场' not in body for body in bodies)
    assert all('&lt;' in body for body in bodies)
    if terminal:
        profit_parts = [body.split('本局净盈亏</b>', 1)[1] for body in bodies if '本局净盈亏</b>' in body]
        assert sum(part.count('tg://user?id=') for part in profit_parts) == 73
    for i in range(len(bodies)):
        body, keys = card(dict(row, group_page=i), snapshot)
        assert body == bodies[i]
        assert all(b['callback_data'].endswith(':latest') or ':page' in b['callback_data'] for line in keys for b in line)


def test_paging_edits_same_photo_then_one_true_result_with_complete_history(env):
    async def run():
        row = await create(env)
        for i in range(19):
            await send(env, message(env, '/大 10' if i % 2 else '/小 10', i % 2, 8100+i))
        await refresh(env, row)
        assert numbers(env.tg.text(GROUP, row['card_mid'])) == list(range(19, 11, -1))
        await click(env, row, 'page1')
        await refresh(env, row)
        assert numbers(env.tg.text(GROUP, row['card_mid'])) == list(range(11, 3, -1))
        uploads = []
        real = env.bot._call_multipart
        async def capture(method, payload, files, **kw):
            uploads.append((payload, files))
            return await real(method, payload, files, **kw)
        env.bot._call_multipart = capture
        env.db.execute("UPDATE dice_rounds SET state='fixed',dice='[2,3,6]',seed=?", ('ab'*32,))
        await env.bot._dice_tick()
        report = env.db.one("SELECT * FROM dice_messages WHERE purpose='report:0'")
        assert report['state'] == 'sent'
        assert uploads[0][1]['photo'][1] == art_bytes('236')
        assert '11点' in uploads[0][0]['caption']
        assert numbers(env.tg.text(GROUP, report['mid'])) == list(range(19, 11, -1))
        assert len(env.db.query("SELECT * FROM dice_messages WHERE purpose LIKE 'report:%'")) == 1
        assert not env.db.query("SELECT * FROM dice_messages WHERE purpose LIKE 'art:%'")
        bodies = []
        for i in range(10):
            body = env.tg.text(GROUP, report['mid'])
            bodies.append(body)
            actions = env.tg.actions(GROUP, report['mid'])
            next_button = next((b for b in actions if b['text'] == '下页 ›'), None)
            if not next_button:
                break
            await click(env, row, next_button['callback_data'].rsplit(':', 1)[1], reply(env, dict(row, card_mid=report['mid'])))
            await refresh(env, row)
        assert [n for body in bodies for n in numbers(body)] == list(range(19, 0, -1))
        assert sum(body.split('本局净盈亏</b>')[-1].count('tg://user?id=') for body in bodies if '本局净盈亏</b>' in body) == 2
        deleted = {p['message_id'] for m, p in env.tg.calls if m == 'deleteMessage'}
        assert row['card_mid'] in deleted and report['mid'] not in deleted
        assert all(mid in {7001, row['card_mid'], *range(8100, 8119)} for mid in deleted)
        assert len([m for m, _ in env.tg.calls if m == 'sendPhoto']) == 2
        assert not [m for m, _ in env.tg.calls if m == 'sendMessage']
    asyncio.run(run())


def test_unknown_single_page_open_never_replays_send_or_accepts_bet(env):
    async def run():
        real = env.bot._call_multipart
        async def lose(method, payload, files, **kw):
            await real(method, payload, files, **kw)
            CALL_DELIVERY.set({'state': 'unknown', 'reason': 'lost ACK'})
        env.bot._call_multipart = lose
        row = await create(env)
        assert row['card_mid'] is None
        assert env.db.one("SELECT state FROM dice_messages WHERE purpose='open'")['state'] == 'unknown'
        await env.bot._dice_publish_open(row)
        await send(env, message(env, '/大 100', mid=8100))
        assert not env.db.query('SELECT * FROM dice_tickets')
        assert len([m for m, _ in env.tg.calls if m == 'sendPhoto']) == 1
    asyncio.run(run())


def test_unknown_report_ack_keeps_original_and_recovers_without_duplicate(env):
    async def run():
        row = await create(env)
        await send(env, message(env, '/大 10', mid=8100))
        real = env.bot._call_multipart
        async def lose(method, payload, files, **kw):
            assert files['photo'][1] == art_bytes('covered')  # timeout: never fabricated pips
            await real(method, payload, files, **kw)
            CALL_DELIVERY.set({'state': 'unknown', 'reason': 'lost ACK'})
        env.bot._call_multipart = lose
        env.db.execute('UPDATE dice_rounds SET deadline=0,timeout=0')
        await env.bot._dice_tick()
        report = env.db.one("SELECT * FROM dice_messages WHERE purpose='report:0'")
        assert report['state'] == 'unknown'
        assert not env.db.one('SELECT 1 FROM play_jobs WHERE job_key=?', (f'dice:{GROUP}:{row["card_mid"]}',))
        await env.bot._dice_tick()
        assert len([m for m, _ in env.tg.calls if m == 'sendPhoto']) == 2
        actual = max(mid for (chat, mid), p in env.tg.messages.items() if '本局全额退回' in p.get('caption', ''))
        svc(env).recover_delivery(report['id'], confirmed_mid=actual)
        env.bot._call_multipart = real
        await env.bot._dice_tick()
        assert env.db.one('SELECT 1 FROM play_jobs WHERE job_key=?', (f'dice:{GROUP}:{row["card_mid"]}',))
        assert len([m for m, _ in env.tg.calls if m == 'sendPhoto']) == 2
    asyncio.run(run())


def test_caption_rate_limit_and_cross_round_receipt_replay(env):
    async def run():
        row = await create(env)
        old = message(env, '/大 100', mid=8100)
        await send(env, old)
        real = env.bot._call
        async def limited(method, payload=None, **kw):
            if method == 'editMessageCaption':
                CALL_DELIVERY.set({'state': 'retry', 'retry_after': 30})
                return None
            return await real(method, payload, **kw)
        env.bot._call = limited
        await refresh(env, row)
        dirty = svc(env).get(row['nonce'])
        assert dirty['group_rendered'] < dirty['group_revision'] and dirty['group_retry'] > 0
        env.bot._call = real
        env.db.execute('UPDATE dice_rounds SET deadline=0,timeout=0,group_retry=0,group_due=0')
        await env.bot._dice_tick()
        second = await create(env, index=1, mid=9000)
        await send(env, old)
        await send(env, bet(env, row, '/大 50', mid=9001))
        assert not env.db.query('SELECT * FROM dice_tickets WHERE nonce=?', (second['nonce'],))
        assert env.services.points.balance(env.uids[0]) == 1000
    asyncio.run(run())
