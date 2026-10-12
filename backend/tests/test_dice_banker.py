"""Player-funded banker: real SQLite, fake Telegram, immutable legacy rules."""
import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from itertools import product

import pytest
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_dice import cash, create, message, svc
from test_dice_size_group import refresh, send
from test_tg_interaction_context import GROUP

from app.core.errors import ConfigError
from app.modules.account_deletion import self_deletion_guard
from app.modules.dice_banker import BANKER_VERSION, QUICK_BANKER_VERSION, banker, settle_banker
from app.modules.dice_caption_view import pages
from app.modules.dice_rules import MAX_INT
from app.modules.game_mentions import text_units
from app.modules.play_money import PlayError


@pytest.fixture
def env(request):
    e = request.getfixturevalue('bw_env')
    e.services.registry.save('dice', enabled=True)
    e.services.registry.get('dice').ctx.telegram = e.bot
    return e


def opened(e, capital=100, **kw):
    return asyncio.run(create(e, text=f'/猜大小 {capital}', **kw))


def place(e, side, amount, index=1, mid=8100, **kw):
    return svc(e).group_bet(message(e, f'/{side} {amount}', index=index, mid=mid, **kw), side, amount)


def fixed(e, row, dice):
    e.db.execute("UPDATE dice_rounds SET state='fixed',dice=?,seed=? WHERE nonce=?", (json.dumps(dice), 'ab'*32, row['nonce']))
    return svc(e).settle(row['nonce'], now=row['timeout']+100)


@pytest.mark.parametrize('dice', list(product(range(1, 7), repeat=3)))
def test_all_216_fixed_odds_and_banker_conservation(dice):
    bank = {'user_id': 'bank', 'capital': 100}
    bets = [{'user_id': 'u1', 'option': '大', 'amount': 70},
            {'user_id': 'u1', 'option': '小', 'amount': 10},
            {'user_id': 'u2', 'option': '小', 'amount': 20}]
    out = settle_banker(bets, dice, bank)
    triple = len(set(dice)) == 1
    assert out['banker']['net'] == (100 if triple else -40 if sum(dice) >= 11 else 40)
    assert out['users']['u1']['returned'] == (0 if triple else 140 if sum(dice) >= 11 else 20)
    assert sum(o['returned'] for o in out['users'].values())+out['banker']['returned'] == 200
    assert out['banker']['net']+sum(o['net'] for o in out['users'].values()) == 0


@pytest.mark.parametrize('text', ['/猜大小', '/猜大小 0', '/猜大小 -1', '/猜大小 1.5', '/猜大小 1001', '/猜大小 '+str(MAX_INT+1), '/猜大小 10 20'])
def test_bad_capital_never_opens_or_charges(env, text):
    before = cash(env)
    asyncio.run(send(env, message(env, text)))
    assert not env.db.query('SELECT * FROM dice_rounds')
    assert not env.db.query('SELECT * FROM play_escrows')
    assert cash(env) == before and env.services.points.balance(env.uids[0]) == 1000


def test_banker_open_idempotency_and_multithread_collateral(env):
    before = cash(env)
    row = opened(env, 600)
    assert row['deadline']-row['created'] == 90
    assert json.loads(row['rules'])['version'] == QUICK_BANKER_VERSION
    assert banker(row)['capital'] == 600
    assert env.services.points.balance(env.uids[0]) == 400
    assert svc(env).create(message(env, '/猜大小 600'))['nonce'] == row['nonce']
    with pytest.raises(PlayError):
        svc(env).create(message(env, '/猜大小 601'))
    with pytest.raises(PlayError, match='不足'):
        svc(env).create(message(env, '/猜大小 600', mid=9000, thread=22), now=row['created']+16)
    second = svc(env).create(message(env, '/猜大小 400', mid=9001, thread=22), now=row['created']+16)
    assert second['nonce'] != row['nonce']
    assert env.services.points.balance(env.uids[0]) == 0 and cash(env) == before
    # Another caller cannot take over the active round or reserve again.
    assert svc(env).create(message(env, '/猜大小 100', index=1, mid=9100))['nonce'] == row['nonce']
    assert env.services.points.balance(env.uids[1]) == 1000


def test_exposure_rejects_whole_bet_then_reverse_offsets_and_self_bet_forbidden(env):
    env.services.registry.save('dice', config={'pool_multiplier': 10})
    row = opened(env, 100)
    before = cash(env)
    with pytest.raises(PlayError, match='庄家'):
        place(env, '大', 1, index=0)
    place(env, '大', 100)
    with pytest.raises(PlayError, match='整笔'):
        place(env, '大', 1, mid=8101)
    assert env.services.points.balance(env.uids[1]) == 900
    place(env, '小', 200, mid=8102)
    place(env, '大', 200, index=2, mid=8103)
    assert env.db.one('SELECT accepted FROM dice_pools')['accepted'] == 500
    assert cash(env) == before
    done = fixed(env, row, [2,3,6])
    out = json.loads(done['result'])['size']
    assert out['banker']['net'] == -100
    assert out['users'][env.uids[1]] == {'invested': 300, 'returned': 200, 'net': -100}
    assert env.services.points.balance(env.uids[0]) == 900
    assert env.services.points.balance(env.uids[1]) == 900
    assert env.services.points.balance(env.uids[2]) == 1200
    assert cash(env) == before
    assert not env.db.query('SELECT * FROM play_escrows WHERE amount>0')
    assert svc(env).settle(row['nonce'])['result'] == done['result']


@pytest.mark.parametrize('dice,net,player', [([2,3,6], -80, 1080), ([1,2,3], 80, 920), ([6,6,6], 80, 920)])
def test_single_side_and_triples_do_not_refund(env, dice, net, player):
    row = opened(env, 100)
    before = cash(env)
    place(env, '大', 80)
    done = fixed(env, row, dice)
    assert json.loads(done['result'])['size']['banker']['net'] == net
    assert env.services.points.balance(env.uids[0]) == 1000+net
    assert env.services.points.balance(env.uids[1]) == player
    assert done['state'] == 'settled' and cash(env) == before


@pytest.mark.parametrize('with_bets', [False, True])
def test_empty_and_timeout_return_collateral_and_all_principals(env, with_bets):
    row = opened(env, 1000)
    before = cash(env)
    if with_bets:
        place(env, '小', 55)
    svc(env).close(row['nonce'], now=row['deadline'])
    done = svc(env).settle(row['nonce'], now=row['timeout'] if with_bets else row['deadline'])
    assert done['state'] == ('refunded' if with_bets else 'empty')
    assert done['dice'] == '[]'
    assert env.services.points.balance(env.uids[0]) == env.services.points.balance(env.uids[1]) == 1000
    assert json.loads(done['result'])['size']['banker']['net'] == 0 and cash(env) == before


def test_banker_deletion_mutual_exclusion_and_failed_settlement_rollback(env, monkeypatch):
    with self_deletion_guard(env.db, env.uids[0]), pytest.raises(PlayError, match='注销'):
        svc(env).create(message(env))
    row = opened(env, 100)
    with pytest.raises(ConfigError, match='庄本'), self_deletion_guard(env.db, env.uids[0]):
        pytest.fail('banker cannot delete while money is held')
    place(env, '大', 50)
    before = cash(env)
    original = env.bot._points._apply
    def fail(conn, uid, amount, *a, **kw):
        if amount > 0 and uid == env.uids[0]:
            raise RuntimeError('crash during banker payout')
        return original(conn, uid, amount, *a, **kw)
    monkeypatch.setattr(env.bot._points, '_apply', fail)
    with pytest.raises(RuntimeError, match='crash'):
        fixed(env, row, [1,2,3])
    assert env.db.one('SELECT held FROM dice_pools')['held'] == 50 and cash(env) == before
    assert env.db.one("SELECT amount FROM play_escrows WHERE ref=?", (row['nonce']+':banker',))['amount'] == 100
    monkeypatch.setattr(env.bot._points, '_apply', original)
    svc(env).settle(row['nonce'])
    with self_deletion_guard(env.db, env.uids[0]):
        pass
    assert cash(env) == before


def test_cross_connection_concurrent_risk_and_restart_replay(env):
    from app.core.db import Database
    from app.modules.dice import DiceService
    from app.modules.groups import GroupService
    from app.modules.members import MemberService
    from app.modules.points import PointsService
    row = opened(env, 100)
    db = Database(env.db.one('PRAGMA database_list')['file'])
    second = DiceService(db, MemberService(db, GroupService(db)), PointsService(db), lambda: True, lambda c: True, '123')
    def put(args):
        service, mid = args
        try:
            service.group_bet(message(env, '/大 70', index=1, mid=mid), '大', 70)
            return True
        except PlayError:
            return False
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sum(pool.map(put, [(svc(env), 8100), (second, 8101)])) == 1
        receipt = db.one('SELECT * FROM dice_group_receipts')
        assert put((second, receipt['message_id']))
        done = fixed(env, row, [1,2,3])
        next_row = second.create(message(env, '/猜大小 100', mid=9000, index=2))
        assert next_row['nonce'] != row['nonce']
        assert put((second, receipt['message_id']))
        assert not db.query('SELECT * FROM dice_tickets WHERE nonce=?', (next_row['nonce'],))
        assert second.settle(row['nonce'])['result'] == done['result']
    finally:
        db.close()


def test_safety_integer_boundaries(env):
    env.services.registry.save('dice', config={'pool_multiplier': 1})
    env.services.points.add(env.uids[0], MAX_INT-1000, 'admin.adjust')
    row = opened(env, MAX_INT)
    with pytest.raises(PlayError, match='安全整数'):
        place(env, '大', 1)
    assert env.services.points.balance(env.uids[1]) == 1000
    svc(env).close(row['nonce'], now=row['deadline'])
    svc(env).settle(row['nonce'], now=row['deadline'])
    assert env.services.points.balance(env.uids[0]) == MAX_INT


def test_banker_caption_real_result_complete_history_and_unknown_ack(env):
    from app.modules.report_delivery import CALL_DELIVERY
    async def run():
        row = await create(env)
        body = env.tg.text(GROUP, row['card_mid'])
        assert '👑 庄家' in body and '庄本 <b>1,000</b>' in body and 'tg://user?id=' in body
        assert '赢返2倍含本金' in body and '豹子通吃' in body and '最新入场' not in body
        for i in range(19):
            await send(env, message(env, '/大 10' if i % 2 else '/小 10', index=1+i%2, mid=8100+i))
        await refresh(env, row)
        fixed(env, row, [6,6,6])
        row, snapshot = svc(env).group_snapshot(row['nonce'])
        snapshot = copy.deepcopy(snapshot)
        for t in snapshot['tickets']:
            t['display_name'] = '🎲'*40
        bodies = pages(row, snapshot)
        assert all(text_units(b) <= 1024 for b in bodies)
        assert bodies[0].index('庄家净盈亏') < bodies[0].index('完整流水')
        assert '+190' in bodies[0]
        import re
        assert [int(n) for b in bodies for n in re.findall(r'#(\d+)</code>', b)] == list(range(19,0,-1))
        real = env.bot._call_multipart
        async def lose(method, payload, files, **kw):
            assert files['photo'][0] == '666.jpg'
            await real(method, payload, files, **kw)
            CALL_DELIVERY.set({'state': 'unknown', 'reason': 'lost ACK'})
        env.bot._call_multipart = lose
        await env.bot._dice_tick()
        task = env.bot._dice_draw_tasks.get((env.bot._active_bot_id, row['nonce']))
        if task:
            await asyncio.wait_for(task[0], 2)
        report = env.db.one("SELECT * FROM dice_messages WHERE purpose='report:0'")
        assert report['state'] == 'unknown'
        assert not env.db.one('SELECT 1 FROM play_jobs WHERE job_key=?', (f'dice:{GROUP}:{row["card_mid"]}',))
        await env.bot._dice_tick()
        task = env.bot._dice_draw_tasks.get((env.bot._active_bot_id, row['nonce']))
        if task:
            await asyncio.wait_for(task[0], 2)
        assert len([m for m,p in env.tg.calls if m == 'sendPhoto']) == 2
        actual = max(mid for (_,mid), p in env.tg.messages.items() if '庄家净盈亏' in p.get('caption',''))
        svc(env).recover_delivery(report['id'], confirmed_mid=actual)
        env.bot._call_multipart = real
        await env.bot._dice_tick()
        task = env.bot._dice_draw_tasks.get((env.bot._active_bot_id, row['nonce']))
        if task:
            await asyncio.wait_for(task[0], 2)
        assert env.db.one('SELECT 1 FROM play_jobs WHERE job_key=?', (f'dice:{GROUP}:{row["card_mid"]}',))
    asyncio.run(run())


def test_unknown_open_collateral_is_not_reused_and_returns_on_empty_close(env):
    from app.modules.report_delivery import CALL_DELIVERY
    async def run():
        real = env.bot._call_multipart
        async def lose(method, payload, files, **kw):
            await real(method, payload, files, **kw)
            CALL_DELIVERY.set({'state': 'unknown', 'reason': 'lost opening ACK'})
        env.bot._call_multipart = lose
        row = await create(env)
        assert row['card_mid'] is None and env.services.points.balance(env.uids[0]) == 0
        await env.bot._dice_publish_open(row)
        await send(env, message(env, '/大 100', index=1, mid=8100))
        assert not env.db.query('SELECT * FROM dice_tickets')
        assert len([m for m,p in env.tg.calls if m == 'sendPhoto']) == 1
        with pytest.raises(ConfigError, match='庄本'), self_deletion_guard(env.db, env.uids[0]):
            pytest.fail('unacknowledged card still owns the collateral')
        env.bot._call_multipart = real
        env.db.execute('UPDATE dice_rounds SET deadline=0')
        await env.bot._dice_tick()
        task = env.bot._dice_draw_tasks.get((env.bot._active_bot_id, row['nonce']))
        if task:
            await asyncio.wait_for(task[0], 2)
        assert svc(env).get(row['nonce'])['state'] == 'empty'
        assert env.services.points.balance(env.uids[0]) == 1000
    asyncio.run(run())


def test_signed_beacon_legacy_banker_uses_existing_verification_and_never_rerolls(env):
    import time

    from test_dice import SIGNED

    from app.modules import dice_beacon as beacon
    row = opened(env)
    place(env, '大', 100)
    rules = json.loads(row['rules'])
    rules.update(version=BANKER_VERSION, draw_version=beacon.VERSION, chain=beacon.CHAIN)
    env.db.execute("UPDATE dice_rounds SET rules=?,state='locked',round_number=?,deadline=0,timeout=?", (json.dumps(rules), SIGNED['round'], time.time()+600))
    fixed_row = svc(env).fix(row['nonce'], SIGNED)
    expected_seed, expected_dice = beacon.derive(row['secret'], row['nonce'], beacon.verify(SIGNED, SIGNED['round']))
    assert fixed_row['seed'] == expected_seed and json.loads(fixed_row['dice']) == expected_dice
    done = svc(env).settle(row['nonce'], now=fixed_row['timeout']+1)
    assert done['state'] == 'settled' and not json.loads(done['result'])['size']['reason']
    assert svc(env).fix(row['nonce'], SIGNED)['result'] == done['result']


def test_bankruptcy_boundary_and_stale_reply_do_not_touch_next_banker(env):
    row = opened(env, 100)
    place(env, '大', 100)
    fixed(env, row, [2,3,6])
    assert env.services.points.balance(env.uids[0]) == 900
    second = opened(env, 500, index=2, mid=9000)
    old_reply = message(env, '/小 50', index=1, mid=9001)
    old_reply['reply_to_message'] = {'chat': old_reply['chat'], 'from': {'id': 123, 'is_bot': True},
                                   'message_id': row['card_mid'], 'is_topic_message': True, 'message_thread_id': row['thread_id']}
    with pytest.raises(PlayError, match='封盘'):
        svc(env).group_bet(old_reply, '小', 50)
    assert not env.db.query('SELECT * FROM dice_tickets WHERE nonce=?', (second['nonce'],))
    assert env.services.points.balance(env.uids[2]) == 500
