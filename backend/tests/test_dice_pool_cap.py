"""Immutable per-round pool ceilings and early sealing, never an earlier beacon."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_dice import cash, create, message, svc
from test_dice_allin import receipt
from test_dice_banker import bw_env, economy_env, interaction_env, opened, place  # noqa: F401
from test_dice_banker import env as banker_env  # noqa: F401
from test_dice_countdown import Timer
from test_dice_size_group import send

from app.modules import dice_beacon as beacon
from app.modules.dice_caption_view import pages
from app.modules.dice_group_view import banker_help
from app.modules.dice_rules import MAX_INT
from app.modules.game_mentions import text_units
from app.modules.play_money import PlayError


@pytest.fixture
def env(request):
    return request.getfixturevalue('banker_env')


def test_default_cap_config_is_saved_only_in_new_rounds(env):
    row = opened(env, 100)
    rules = json.loads(row['rules'])
    assert (rules['pool_multiplier'], rules['pool_cap']) == (2, 200)
    plugin = env.services.registry.get('dice')
    field = next(f for f in plugin.spec.fields if f.key == 'pool_multiplier')
    assert (field.default, field.min, field.max) == (2, 1, 10)
    assert '仅新局' in field.help
    env.services.registry.save('dice', config={'pool_multiplier': '3'})
    assert svc(env).get(row['nonce'])['rules'] == row['rules']
    next_row = svc(env).create(message(env, '/猜大小 100', mid=9000, index=2, thread=22))
    assert json.loads(next_row['rules'])['pool_cap'] == 300
    assert plugin.readonly_status()['总投注上限倍数'] == 3
    assert '庄本×3' in banker_help(env.services.registry.config('dice'))


@pytest.mark.parametrize('value', [0, 11, -1, 1.5, 'bad'])
def test_invalid_multiplier_rejected(env, value):
    with pytest.raises(ValueError):
        env.services.registry.save('dice', config={'pool_multiplier': value})
    assert env.services.registry.config('dice')['pool_multiplier'] == 2


def test_product_overflow_never_reserves_or_creates(env):
    env.services.points.add(env.uids[0], MAX_INT-1000, 'admin.adjust')
    before = env.services.points.balance(env.uids[0])
    with pytest.raises(PlayError, match='安全整数'):
        svc(env).create(message(env, f'/猜大小 {MAX_INT}'))
    assert not env.db.query('SELECT * FROM dice_rounds')
    assert not env.db.query('SELECT * FROM play_escrows')
    assert env.services.points.balance(env.uids[0]) == before


def test_numeric_over_cap_rejects_whole_bet_with_remaining_amount(env):
    row = opened(env, 100)
    place(env, '大', 50)
    before = cash(env)
    with pytest.raises(PlayError, match='还可押 150 分'):
        place(env, '小', 151, index=2, mid=8101)
    assert receipt(env, 8101) is None
    assert env.db.one('SELECT accepted FROM dice_pools')['accepted'] == 50
    assert svc(env).get(row['nonce'])['state'] == 'open' and cash(env) == before


def test_allin_hits_pool_before_direction_capacity_and_replay_is_idempotent(env):
    row = opened(env, 100)
    place(env, '大', 50)
    place(env, '小', 50, index=2, mid=8101)
    # Big direction has 100 room, but after a small extra bet its 150 room exceeds pool's 50.
    place(env, '小', 50, index=2, mid=8102)
    done = place(env, '大', 'allin', mid=8103)
    assert receipt(env, 8103)['amount'] == 50 and done['state'] == 'locked'
    assert svc(env).get(row['nonce'])['group_due'] == 0
    for key in ('deadline', 'round_number', 'timeout'):
        assert done[key] == row[key]
    place(env, '大', 'allin', mid=8103)
    assert env.db.one('SELECT COUNT(*) n FROM dice_tickets')['n'] == 4
    with pytest.raises(PlayError, match='封盘'):
        place(env, '小', 1, index=2, mid=8104)


def test_legacy_uncapped_banker_keeps_its_original_capacity(env):
    row = opened(env, 100)
    old_rules = json.loads(row['rules'])
    old_rules.pop('pool_cap')
    old_rules.pop('pool_multiplier')
    env.db.execute('UPDATE dice_rounds SET rules=?', (json.dumps(old_rules),))
    env.services.registry.save('dice', config={'pool_multiplier': 1})
    place(env, '大', 'allin')
    place(env, '小', 'allin', index=2, mid=8101)
    done = place(env, '大', 'allin', mid=8102)
    assert env.db.one('SELECT accepted FROM dice_pools')['accepted'] == 500
    assert done['state'] == 'open' and json.loads(done['rules']) == old_rules


def test_opposite_direction_concurrent_bets_only_one_can_fill(env):
    from app.core.db import Database
    from app.modules.dice import DiceService
    from app.modules.groups import GroupService
    from app.modules.members import MemberService
    from app.modules.points import PointsService
    row = opened(env, 100)
    place(env, '大', 80)
    place(env, '小', 80, index=2, mid=8101)
    db = Database(env.db.one('PRAGMA database_list')['file'])
    other = DiceService(db, MemberService(db, GroupService(db)), PointsService(db), lambda: True, lambda c: True, '123')
    def put(args):
        service, side, index, mid = args
        try:
            service.group_bet(message(env, f'/{side} 40', index=index, mid=mid), side, 40)
            return True
        except PlayError:
            return False
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sum(pool.map(put, [(svc(env), '大', 1, 8102), (other, '小', 2, 8103)])) == 1
        assert svc(env).get(row['nonce'])['state'] == 'locked'
        assert env.db.one('SELECT accepted FROM dice_pools')['accepted'] == 200
        assert env.db.one('SELECT COUNT(*) n FROM dice_tickets')['n'] == 3
    finally:
        db.close()


def test_full_caption_immediately_seals_timer_and_worker_waits_original_beacon(env, monkeypatch):
    async def run():
        timer = Timer(monkeypatch)
        row = await create(env, text='/猜大小 100')
        task = env.bot._dice_group_tasks[row['nonce']]
        await send(env, message(env, '/大 100', index=1, mid=8100))
        await send(env, message(env, '/小 allin', index=2, mid=8101))
        current = svc(env).get(row['nonce'])
        assert current['state'] == 'locked'
        from test_tg_interaction_context import GROUP
        body = env.tg.text(GROUP, row['card_mid'])
        assert '已满额封盘 · 等待开奖' in body and '200 / 200' in body
        assert '剩余 00:00' in body
        await timer.step()
        assert task.done() and not env.bot._dice_group_tasks
        async def forbidden(_):
            pytest.fail('must not fetch before the original beacon round')
        monkeypatch.setattr(beacon, 'fetch', forbidden)
        await env.bot._dice_tick()
        assert svc(env).get(row['nonce'])['state'] == 'locked'
        for key in ('deadline', 'round_number', 'timeout'):
            assert current[key] == row[key]
        assert current['timeout'] == row['deadline']+600
        assert len([m for m, _ in env.tg.calls if m == 'sendPhoto']) == 1
    asyncio.run(run())


def test_caption_limit_includes_long_banker_and_pool_ceiling(env):
    row = opened(env, 1000)
    for i in range(19):
        place(env, '大' if i % 2 else '小', 10, index=1+i%2, mid=8100+i)
    row, snapshot = svc(env).group_snapshot(row['nonce'])
    rules = json.loads(row['rules'])
    rules['banker']['display_name'] = '🎲'*40
    row['rules'] = json.dumps(rules)
    for t in snapshot['tickets']:
        t['display_name'] = '🎲'*40
    bodies = pages(row, snapshot)
    assert all(text_units(b) <= 1024 and '190 / 2,000' in b for b in bodies)
    import re
    assert [int(n) for b in bodies for n in re.findall(r'#(\d+)</code>', b)] == list(range(19,0,-1))
