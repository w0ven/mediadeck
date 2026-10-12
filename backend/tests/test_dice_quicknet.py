"""Pinned public samples, no production access, no probabilistic fairness patches."""
import asyncio
import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_dice import message, svc
from test_dice_banker import bw_env, economy_env, interaction_env, opened, place  # noqa: F401
from test_dice_banker import env as banker_env  # noqa: F401

from app.modules import bot_dice, dice, dice_beacon, dice_group
from app.modules import dice_quicknet as quick
from app.modules.dice import DiceService, migrate
from app.modules.dice_banker import BANKER_VERSION, QUICK_BANKER_VERSION, is_banker
from app.modules.dice_caption_view import pages
from app.modules.dice_group import is_group
from app.modules.play_money import PlayError

FIXTURES = Path(__file__).parent / 'fixtures'
SIGNED = json.loads((FIXTURES / 'drand_quicknet_1000000.json').read_text())
INFO = json.loads((FIXTURES / 'drand_quicknet_info.json').read_text())


@pytest.fixture
def env(request):
    return request.getfixturevalue('banker_env')


def test_official_info_and_real_signature():
    assert (quick.CHAIN, quick.PUBLIC_KEY, quick.GENESIS, quick.PERIOD, quick.SCHEME) == (
        INFO['chain_hash'], INFO['public_key'], INFO['genesis_time'], INFO['period'], INFO['scheme'])
    assert quick.verify(SIGNED, 1000000) == SIGNED


@pytest.mark.parametrize('change', [
    {'round': 1000001}, {'round': True}, {'signature': '00'*48},
    {'signature': 'c0'+'00'*47},  # point at infinity
    {'signature': SIGNED['signature'][:-2]+'00'},
    {'randomness': '00'*32}, {'signature': SIGNED['signature']+'00'},
    {'chain_hash': dice_beacon.CHAIN}, {'signature': None},
])
def test_tampered_wrong_round_wrong_chain_and_noncanonical_rejected(change):
    with pytest.raises(ValueError):
        quick.verify({**SIGNED, **change}, 1000000)


def test_same_scheme_different_chain_key_is_not_accepted(monkeypatch):
    from py_ecc.bls.g2_primitives import G2_to_signature
    from py_ecc.optimized_bls12_381 import G2, multiply
    monkeypatch.setattr(quick, 'PUBLIC_KEY', G2_to_signature(multiply(G2, 9)).hex())
    with pytest.raises(ValueError, match='invalid'):
        quick.verify(SIGNED, 1000000)


def test_signature_binds_round_even_if_response_round_is_forged():
    with pytest.raises(ValueError):
        quick.verify({**SIGNED, 'round': 1000001}, 1000001)
    with pytest.raises(ValueError):
        quick.verify([], 1000000)
    from test_dice import SIGNED as OLD
    with pytest.raises(ValueError):
        quick.verify(OLD, OLD['round'])
    with pytest.raises(ValueError):
        dice_beacon.verify(SIGNED, 1000000)


@pytest.mark.parametrize('offset', [0, .001, .999, 1, 2, 2.999, 3])
def test_selection_strict_boundary_and_two_second_clock_margin(offset):
    clock = quick.GENESIS+300+offset
    number = quick.future_round(clock)
    assert 2 < quick.round_time(number)-clock <= 5
    assert quick.round_time(number-1) <= clock+2


def test_seed_spec_independent_reconstruction_and_domain_separation():
    secret, nonce = 'ab'*32, 'cd'*12
    seed, rolled = quick.derive(secret, nonce, SIGNED)
    expected = hashlib.sha256(json.dumps([quick.VERSION, quick.CHAIN, 1000000, secret, nonce, SIGNED['randomness']], separators=(',', ':')).encode('ascii')).digest()
    values = [b % 6+1 for b in hashlib.sha256(expected+(0).to_bytes(8, 'big')).digest() if b < 252][:3]
    assert seed == expected.hex() and rolled == values
    assert quick.derive(secret, 'ef'*12, SIGNED)[0] != seed
    assert quick.derive('12'*32, nonce, SIGNED)[0] != seed
    assert quick.derive(secret, nonce, {**SIGNED, 'round': 1000001})[0] != seed
    assert dice_beacon.derive(secret, nonce, SIGNED)[0] != seed


def test_full_pool_selects_once_in_same_transaction_and_replay_restart_cannot_reselect(env):
    row = opened(env, 100)
    assert row['round_number'] == 0 and row['closed_at'] is None
    assert is_banker(row) and is_group(row) and dice_beacon.for_row(row) is quick
    place(env, '大', 100)
    when = row['created']+10
    locked = svc(env).group_bet(message(env, '/小 100', index=2, mid=8101), '小', 100, now=when)
    assert locked['closed_at'] == when and locked['timeout'] == when+600
    assert locked['round_number'] == quick.future_round(when)
    assert quick.round_time(locked['round_number']) < row['deadline']
    service = DiceService(env.db, svc(env).members, svc(env).points, lambda: True, lambda c: True, '123')
    replay = service.group_bet(message(env, '/小 100', index=2, mid=8101), '小', 100, now=when+100)
    closed = service.close(row['nonce'], now=when+1000)
    for key in ('closed_at', 'timeout', 'round_number', 'secret', 'commitment', 'rules'):
        assert replay[key] == closed[key] == locked[key]
    assert env.db.one('SELECT accepted FROM dice_pools')['accepted'] == 200
    with pytest.raises(PlayError, match='封盘'):
        svc(env).group_bet(message(env, '/大 1', index=1, mid=8102), '大', 1, now=when+1)


def test_delayed_deadline_closure_selects_future_from_actual_transaction_not_deadline(env):
    row = opened(env)
    place(env, '大', 10)
    with pytest.raises(PlayError, match='封盘'):
        svc(env).group_bet(message(env, '/小 1', index=1, mid=8101), '小', 1, now=row['deadline'])
    late = row['deadline']+120
    row = svc(env).close(row['nonce'], now=late)
    assert row['closed_at'] == late and row['timeout'] == late+600
    assert row['round_number'] == quick.future_round(late)
    assert quick.round_time(row['round_number']) > late+2
    assert svc(env).settle(row['nonce'], now=row['timeout']-.001)['state'] == 'locked'
    assert svc(env).settle(row['nonce'], now=row['timeout'])['state'] == 'refunded'
    assert env.services.points.balance(env.uids[0]) == 1000
    assert env.services.points.balance(env.uids[1]) == 1000


def signed_round(env):
    row = opened(env)
    place(env, '大', 10)
    at = quick.round_time(SIGNED['round'])-3
    env.db.execute('UPDATE dice_rounds SET deadline=? WHERE nonce=?', (at, row['nonce']))
    return svc(env).close(row['nonce'], now=at)


def test_real_signed_fix_settle_restart_and_late_timeout_never_rerolls(env):
    row = signed_round(env)
    service = svc(env)
    published = quick.round_time(row['round_number'])
    with pytest.raises(PlayError, match='尚未'):
        service.fix(row['nonce'], SIGNED, now=published-.001)
    fixed = service.fix(row['nonce'], SIGNED, now=published)
    assert fixed['seed'] == quick.derive(row['secret'], row['nonce'], SIGNED)[0]
    assert fixed['evidence'] and len(json.loads(fixed['dice'])) == 3
    before = copy.deepcopy(env.db.query('SELECT * FROM points_ledger ORDER BY id'))
    assert service.fix(row['nonce'], SIGNED, now=row['timeout']+100)['seed'] == fixed['seed']
    assert env.db.query('SELECT * FROM points_ledger ORDER BY id') == before
    with ThreadPoolExecutor(max_workers=2) as pool:
        done = list(pool.map(lambda _: service.settle(row['nonce'], now=row['timeout']+100), range(2)))
    assert all(r['state'] == 'settled' and r['seed'] == fixed['seed'] for r in done)
    assert env.db.one("SELECT COUNT(*) n FROM play_cash_events WHERE operation='consume' AND scope='dice'")['n'] == 2
    assert sum(env.services.points.balance(uid) for uid in env.uids) == len(env.uids)*1000


def test_old_banker_keeps_original_fixed_chain_and_timeout(env):
    row = opened(env, 100)
    rules = json.loads(row['rules'])
    rules.update(version=BANKER_VERSION, draw_version=dice_beacon.VERSION, chain=dice_beacon.CHAIN)
    number = dice_beacon.future_round(row['deadline'])
    env.db.execute('UPDATE dice_rounds SET rules=?,round_number=?', (json.dumps(rules), number))
    place(env, '大', 100)
    locked = place(env, '小', 100, mid=8101, index=2)
    assert dice_beacon.for_row(locked) is dice_beacon
    assert locked['round_number'] == number and locked['timeout'] == row['timeout']
    assert locked['closed_at'] is None
    migrate(env.db)
    assert svc(env).get(row['nonce']) == locked


def test_opening_caption_public_commitment_selection_but_not_secret(env):
    row = opened(env)
    row, snapshot = svc(env).group_snapshot(row['nonce'])
    body = pages(row, snapshot)[0]
    assert row['commitment'] in body and row['secret'] not in body
    assert '封盘时刻＋2秒后的首轮 quicknet（3秒/轮）' in body
    assert json.loads(row['rules'])['version'] == QUICK_BANKER_VERSION


def fake_clock(monkeypatch, value):
    clock = SimpleNamespace(value=value)
    timer = SimpleNamespace(time=lambda: clock.value)
    for module in (dice, bot_dice, dice_group):
        monkeypatch.setattr(module, 'time', timer)
    return clock


async def stop_draws(env):
    tasks = list(env.bot._in_flight)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def test_independent_runner_wakes_full_pool_no_worker_and_one_report(env, monkeypatch):
    # Close on the historical sample's round; only the public fixture is fetched.
    row = signed_round(env)
    clock = fake_clock(monkeypatch, quick.round_time(row['round_number']))
    calls = []
    async def fetch(number):
        assert not env.db._conn.in_transaction
        calls.append(number)
        return SIGNED
    monkeypatch.setattr(quick, 'fetch', fetch)
    async def run():
        for _ in range(10):
            env.bot._dice_draw_schedule(row['nonce'], wake=True)
        task = env.bot._dice_draw_tasks[('123', row['nonce'])][0]
        await asyncio.wait_for(task, 3)
        assert calls == [SIGNED['round']]
        done = svc(env).get(row['nonce'])
        assert done['state'] == 'settled' and not done['error']
        assert env.db.one("SELECT state,attempts FROM dice_messages WHERE purpose='report:0'") == {'state': 'sent', 'attempts': 1}
        assert len([m for m,p in env.tg.calls if m == 'sendPhoto']) == 2
        assert env.db.one('SELECT 1 FROM play_jobs WHERE job_key=?', (f'dice:{row["chat_id"]}:{row["card_mid"]}',))
        clock.value += 1000
        await env.bot._dice_tick()
        await asyncio.sleep(0)
        assert calls == [SIGNED['round']]
        await stop_draws(env)
    asyncio.run(run())


def test_slow_fetch_does_not_block_other_round_or_worker_and_retry_is_durable(env, monkeypatch):
    first = signed_round(env)
    published = quick.round_time(first['round_number'])
    clock = fake_clock(monkeypatch, published)
    second = svc(env).create(message(env, '/猜大小 100', index=2, mid=9000, thread=22), now=published-100)
    env.db.execute("UPDATE dice_rounds SET state='open',deadline=? WHERE nonce=?", (published-3, second['nonce']))
    # No bets: second returns collateral immediately, independently of slow first.
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []
    async def slow(number):
        assert not env.db._conn.in_transaction
        calls.append(number)
        started.set()
        await release.wait()
        raise ValueError('offline')
    monkeypatch.setattr(quick, 'fetch', slow)
    async def run():
        env.bot._dice_draw_schedule(first['nonce'])
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(env.bot._dice_tick(), 1)
        other = env.bot._dice_draw_tasks.get(('123', second['nonce']))
        if other:
            await asyncio.wait_for(other[0], 1)
        assert svc(env).get(second['nonce'])['state'] == 'empty'
        release.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if svc(env).get(first['nonce'])['beacon_attempts']:
                break
        failed = svc(env).get(first['nonce'])
        assert failed['beacon_attempts'] == 1 and failed['beacon_due'] == clock.value+1
        for _ in range(5):
            await env.bot._dice_tick()
            await asyncio.sleep(0)
        assert calls == [first['round_number']]
        await stop_draws(env)
        # A new recovery task respects persisted backoff, not an in-memory counter.
        env.bot._dice_draw_schedule(first['nonce'])
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(calls) == 1
        clock.value = failed['timeout']
        env.bot._dice_draw_schedule(first['nonce'], wake=True)
        await asyncio.wait_for(env.bot._dice_draw_tasks[('123', first['nonce'])][0], 2)
        assert svc(env).get(first['nonce'])['state'] == 'refunded'
        assert len(calls) == 1
        await stop_draws(env)
    asyncio.run(run())


def test_http_mirror_fallback_checks_signature_and_429_is_retained(monkeypatch):
    import httpx
    real_client = httpx.AsyncClient
    calls = []
    async def respond(request):
        calls.append(str(request.url))
        if request.url.host == 'api.drand.sh':
            return httpx.Response(200, json={**SIGNED, 'randomness': '00'*32})
        return httpx.Response(200, json=SIGNED)
    monkeypatch.setattr(quick.httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    assert asyncio.run(quick.fetch(1000000)) == SIGNED
    assert len(calls) == 2 and all('/'+quick.CHAIN+'/public/1000000' in url for url in calls)
    async def limited(request):
        return httpx.Response(429, headers={'Retry-After': '80'})
    monkeypatch.setattr(quick.httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(limited), **kw))
    with pytest.raises(quick.Unavailable) as failure:
        asyncio.run(quick.fetch(1000000))
    assert failure.value.retry_after == 80


def test_full_command_wakes_sleeping_deadline_runner_and_finishes_before_original_deadline(env, monkeypatch):
    from test_dice import create
    from test_dice_size_group import send
    clock = fake_clock(monkeypatch, quick.round_time(1000000)-20)
    reached = asyncio.Event()
    calls = []
    async def fetch(number):
        calls.append(number)
        reached.set()
        return SIGNED
    monkeypatch.setattr(quick, 'fetch', fetch)
    async def run():
        row = await create(env, text='/猜大小 100')
        await asyncio.sleep(0)  # independent runner is waiting for original deadline
        original = env.bot._dice_draw_tasks[('123', row['nonce'])][0]
        clock.value = quick.round_time(1000000)-3
        await send(env, message(env, '/大 100', index=1, mid=8100))
        await send(env, message(env, '/小 allin', index=2, mid=8101))
        locked = svc(env).get(row['nonce'])
        assert locked['round_number'] == 1000000 and locked['closed_at'] == clock.value
        assert locked['timeout'] == clock.value+600
        # Wake was emitted by the actual full-pool command, not the recovery worker.
        clock.value = quick.round_time(1000000)
        await asyncio.wait_for(reached.wait(), 1)
        await asyncio.wait_for(original, 2)
        assert svc(env).get(row['nonce'])['state'] == 'settled'
        assert clock.value < row['deadline'] and calls == [1000000]
        await stop_draws(env)
    asyncio.run(run())


def test_deadline_runner_closes_and_draws_without_any_worker_poll(env, monkeypatch):
    import time
    row = opened(env)
    place(env, '大', 10)
    started = time.monotonic()
    published = quick.round_time(1000000)
    timer = SimpleNamespace(time=lambda: published-3+(time.monotonic()-started))
    for module in (dice, bot_dice, dice_group):
        monkeypatch.setattr(module, 'time', timer)
    env.db.execute('UPDATE dice_rounds SET deadline=?', (published-3,))
    async def fetch(number):
        assert number == 1000000 and timer.time() >= published
        return SIGNED
    monkeypatch.setattr(quick, 'fetch', fetch)
    async def run():
        env.bot._dice_draw_schedule(row['nonce'])
        await asyncio.wait_for(env.bot._dice_draw_tasks[('123', row['nonce'])][0], 5)
        done = svc(env).get(row['nonce'])
        assert done['state'] == 'settled' and done['round_number'] == 1000000
        assert published-3 <= done['closed_at'] < published-2
        assert done['timeout'] == done['closed_at']+600
        await stop_draws(env)
    asyncio.run(run())


def test_real_result_caption_keeps_public_verification_and_utf16_budget(env):
    from app.modules.game_mentions import text_units
    row = signed_round(env)
    svc(env).fix(row['nonce'], SIGNED, now=quick.round_time(row['round_number']))
    svc(env).settle(row['nonce'], now=row['timeout']+1)
    done, snapshot = svc(env).group_snapshot(row['nonce'])
    rules = json.loads(done['rules'])
    rules['banker']['display_name'] = '🎲'*40
    done['rules'] = json.dumps(rules)
    snapshot['tickets'] = [copy.deepcopy(snapshot['tickets'][0]) for _ in range(19)]
    for ticket in snapshot['tickets']:
        ticket['display_name'] = '🎲'*40
    bodies = pages(done, snapshot)
    assert len(bodies) > 1
    assert all(text_units(body) <= 1024 for body in bodies)
    assert all(done['secret'] in body and done['commitment'] in body and 'quicknet #1000000' in body and '<blockquote expandable>' in body for body in bodies)
    import re
    assert [int(i) for body in bodies for i in re.findall(r'#(\d+)</code>', body)] == list(range(19, 0, -1))
