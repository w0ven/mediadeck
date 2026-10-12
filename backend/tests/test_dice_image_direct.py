"""Direct bets and bundled production art through real handlers, fake Telegram only."""
import asyncio
import copy
import hashlib
import json
from itertools import product

import pytest
from PIL import Image
from test_dice import create, message
from test_dice_size_group import (  # noqa: F401
    bet,
    bw_env,
    click,
    economy_env,
    interaction_env,
    refresh,
    reply,
    send,
)
from test_dice_size_group import env as group_env  # noqa: F401
from test_tg_interaction_context import GROUP

from app.modules import dice_beacon as beacon
from app.modules.dice_art import ASSETS, art_bytes, art_name
from app.modules.play_money import PlayError


@pytest.fixture
def env(request):
    return request.getfixturevalue('group_env')


def test_direct_receipt_survives_next_round_and_restart(env):
    from app.core.db import Database
    from app.modules.dice import DiceService
    from app.modules.groups import GroupService
    from app.modules.members import MemberService
    from app.modules.points import PointsService

    async def run():
        await send(env, message(env, '/大 100', mid=6000))
        assert not env.db.query('SELECT * FROM dice_tickets')
        first = await create(env)
        old = message(env, '/大 100', mid=8001)
        await send(env, old)
        await send(env, message(env, '/小 30', mid=8002))
        assert env.services.points.balance(env.uids[0]) == 870
        await send(env, message(env, '/大 50', mid=8003, thread=22))
        assert env.services.points.balance(env.uids[0]) == 870
        env.db.execute('UPDATE dice_rounds SET deadline=0,timeout=0')
        await env.bot._dice_tick()
        second = await create(env, mid=9000, index=1)
        assert second['nonce'] != first['nonce']
        db = Database(env.db.one('PRAGMA database_list')['file'])
        try:
            service = DiceService(db, MemberService(db, GroupService(db)), PointsService(db), lambda: True, lambda c: True, '123')
            assert service.group_bet(old, '大', 100)['nonce'] == first['nonce']
            with pytest.raises(PlayError, match='旧注'):
                service.group_bet(old, '小', 100)
        finally:
            db.close()
        await send(env, message(env, '/大 100', mid=6000))
        await send(env, bet(env, first, '/大 100', mid=9001))
        assert not env.db.query('SELECT * FROM dice_tickets WHERE nonce=?', (second['nonce'],))
        assert env.services.points.balance(env.uids[0]) == 1000
        await send(env, message(env, '/大@MediaDeckDemoBot 100', mid=9002))
        assert env.services.points.balance(env.uids[0]) == 900
        assert env.db.one('SELECT COUNT(*) n FROM dice_tickets WHERE nonce=?', (second['nonce'],))['n'] == 1
    asyncio.run(run())


def test_new_command_only_and_direct_old_seven_pool_rejected(env):
    async def run():
        for cmd in ('/骰局', '/dice', '/猜大小@OtherBot'):
            await send(env, message(env, cmd))
        assert not env.db.query('SELECT * FROM dice_rounds')
        row = await create(env, text='/猜大小@MediaDeckDemoBot 1000')
        env.db.execute('UPDATE dice_rounds SET rules=?', (json.dumps({'version': beacon.VERSION}),))
        await send(env, message(env, '/大 100', mid=8100))
        assert not env.db.query('SELECT * FROM dice_tickets')
        assert env.services.points.balance(env.uids[0]) == 1000
        assert row['card_mid']
    asyncio.run(run())


def test_art_assets_all_results_and_no_result_never_fabricated():
    hashes = set()
    assert len(list(ASSETS.glob('*.jpg'))) == 217
    for values in product(range(1, 7), repeat=3):
        name = art_name({'dice': json.dumps(values)})
        assert name == ''.join(map(str, values))
        hashes.add(hashlib.sha256(art_bytes(name)).digest())
        with Image.open(ASSETS / (name+'.jpg')) as image:
            assert image.size == (1000, 880)
            image.verify()
        assert art_name({'dice': json.dumps(values)}, opening=True) == 'covered'
    assert len(hashes) == 216
    assert art_name({'dice': '[]'}) == 'covered'
    for invalid in ('[0,1,2]', '[1,2]', '[true,2,3]', '[1,2,7]'):
        with pytest.raises(ValueError):
            art_name({'dice': invalid})


def test_photo_true_result_and_text_ledger_not_caption(env):
    async def run():
        uploads = []
        real = env.bot._call_multipart
        async def capture(method, payload, files, **kw):
            uploads.append((method, copy.deepcopy(payload), files))
            return await real(method, payload, files, **kw)
        env.bot._call_multipart = capture
        row = await create(env)
        assert uploads[0][2]['photo'][1] == art_bytes('covered')
        photo = env.db.one("SELECT * FROM dice_messages WHERE purpose='art:open'")
        await send(env, bet(env, dict(row, card_mid=photo['mid']), '/大 10', mid=8000))
        for i in range(12):
            await send(env, message(env, '/小 10' if i % 2 else '/大 10', i % 2, 8100+i))
        await refresh(env, row)
        from app.modules.game_mentions import text_units
        body = env.tg.text(GROUP, row['card_mid'])
        assert len(body) > 1024 and text_units(body) <= 4096 and '#13' in body and '#6' in body
        assert all('完整流水' not in p[1]['caption'] for p in uploads)
        env.db.execute("UPDATE dice_rounds SET state='fixed',dice='[2,3,6]',seed=?", ('ab'*32,))
        await env.bot._dice_tick()
        assert uploads[-1][2]['photo'][1] == art_bytes('236')
        assert '11点' in uploads[-1][1]['caption']
        assert uploads[-1][1]['message_thread_id'] == row['thread_id']
        deleted = {p['message_id'] for m, p in env.tg.calls if m == 'deleteMessage'}
        result = env.db.one("SELECT * FROM dice_messages WHERE purpose='art:result'")
        assert photo['mid'] in deleted and result['mid'] not in deleted
        assert len(uploads) == 2
    asyncio.run(run())


def test_photo_unknown_ack_never_resends_and_callback_recovers(env):
    from app.modules.report_delivery import CALL_DELIVERY

    async def run():
        real = env.bot._call_multipart
        async def lose(method, payload, files, **kw):
            await real(method, payload, files, **kw)
            CALL_DELIVERY.set({'state': 'unknown', 'reason': 'lost photo ACK'})
        env.bot._call_multipart = lose
        row = await create(env)
        photo = env.db.one("SELECT * FROM dice_messages WHERE purpose='art:open'")
        assert photo['state'] == 'unknown'
        await env.bot._dice_deliver(row['nonce'])
        assert len([m for m, _ in env.tg.calls if m == 'sendPhoto']) == 1
        await send(env, message(env, '/大 10', mid=8100))
        env.db.execute('UPDATE dice_rounds SET deadline=0,timeout=0')
        await env.bot._dice_tick()
        result = env.db.one("SELECT * FROM dice_messages WHERE purpose='art:result'")
        assert result['state'] == 'unknown' and result['body'] == 'covered'
        assert not env.db.one('SELECT 1 FROM play_jobs WHERE job_key=?', (f'dice:{GROUP}:{row["card_mid"]}',))
        await env.bot._dice_tick()
        assert len([m for m, _ in env.tg.calls if m == 'sendPhoto']) == 2
        env.bot._call_multipart = real
        for op in ('artopen', 'artresult'):
            mid = next(mid for (chat, mid), p in env.tg.messages.items()
                       if any(b['callback_data'].endswith(':'+op) for line in p.get('reply_markup', {}).get('inline_keyboard', []) for b in line))
            await click(env, row, op, reply(env, dict(row, card_mid=mid)))
        assert not env.db.query("SELECT * FROM dice_messages WHERE state='unknown'")
        assert env.db.one('SELECT 1 FROM play_jobs WHERE job_key=?', (f'dice:{GROUP}:{row["card_mid"]}',))
        assert len([m for m, _ in env.tg.calls if m == 'sendPhoto']) == 2
    asyncio.run(run())


def test_details_rules_proof_and_return_same_text_card(env):
    async def run():
        row = await create(env)
        def ops():
            return [b['callback_data'].rsplit(':', 1)[-1] for b in env.tg.actions(GROUP, row['card_mid'])]
        assert ops() == ['latest', 'details']
        await click(env, row, 'details')
        await refresh(env, row)
        assert ops() == ['latest', 'rules', 'proof']
        await click(env, row, 'rules')
        await refresh(env, row)
        assert '/猜大小' in env.tg.text(GROUP, row['card_mid'])
        assert '直接在本群发送' in env.tg.text(GROUP, row['card_mid'])
        await click(env, row, 'proof')
        await refresh(env, row)
        assert row['commitment'] in env.tg.text(GROUP, row['card_mid'])
        await click(env, row, 'latest')
        await refresh(env, row)
        assert '完整流水' in env.tg.text(GROUP, row['card_mid'])
        assert len([1 for m, _ in env.tg.calls if m == 'sendMessage']) == 1
    asyncio.run(run())


@pytest.mark.parametrize('case', ['closed', 'unbound', 'anonymous', 'robot', 'paused', 'other_bot'])
def test_direct_invalid_context_never_debits(env, case):
    async def run():
        await create(env)
        m = message(env, '/大 100', mid=8100)
        if case == 'closed': env.db.execute("UPDATE dice_rounds SET state='locked'")
        if case == 'unbound': m['from'] = {'id': 99999, 'is_bot': False}
        if case == 'anonymous': m['sender_chat'] = {'id': GROUP}
        if case == 'robot': m['from'] = {'id': 123, 'is_bot': True}
        if case == 'paused': env.services.registry.save('dice', enabled=False)
        if case == 'other_bot': m['text'] = '/大@OtherBot 100'
        await send(env, m)
        assert not env.db.query('SELECT * FROM dice_tickets')
        assert env.services.points.balance(env.uids[0]) == 1000
    asyncio.run(run())


def test_long_final_report_is_text_not_truncated_to_caption(env):
    from app.modules.dice_group_view import report_pages
    from app.modules.economy_rules import economy_write
    from app.modules.game_mentions import text_units

    async def run():
        row = await create(env)
        tickets = [{'user_id': str(i), 'tg_id': str(10000+i), 'display_name': '🎲'*24,
                    'items': json.dumps([{'option': '大'}]), 'amount': 1} for i in range(20)]
        row.update(state='refunded', dice='[]', result=json.dumps({'size': {
            'reason': 'timeout', 'users': {str(i): {'net': 0} for i in range(20)}}}))
        pages = report_pages(row, tickets)
        assert len(pages) == 1 and 1024 < text_units(pages[0]) <= 4096
        with economy_write(env.db) as conn:
            env.bot._dice_service().queue_message(conn, row['nonce'], 'report:0', row['chat_id'], pages[0], thread=row['thread_id'])
        await env.bot._dice_deliver(row['nonce'])
        report = env.db.one("SELECT * FROM dice_messages WHERE purpose='report:0'")
        assert env.tg.text(GROUP, report['mid']) == pages[0]
        assert pages[0].count('tg://user?id=') == 20
    asyncio.run(run())
