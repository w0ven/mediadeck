"""All-in resolution is serialized with receipts, risk and escrow in SQLite."""
import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_dice import cash, create, message, svc
from test_dice_banker import (  # noqa: F401
    bw_env,
    economy_env,
    fixed,
    interaction_env,
    opened,
    place,
)
from test_dice_banker import env as banker_env  # noqa: F401
from test_dice_size_group import send

from app.modules.account_deletion import self_deletion_guard
from app.modules.dice_group import migrate
from app.modules.dice_rules import MAX_INT
from app.modules.play_money import PlayError


@pytest.fixture
def env(request):
    return request.getfixturevalue('banker_env')


def receipt(e, mid=8100):
    return e.db.one('SELECT * FROM dice_group_receipts WHERE message_id=?', (mid,))


def test_balance_limit_and_case_insensitive_real_dispatch(env):
    async def run():
        await create(env)
        env.services.points.add(env.uids[1], -950, 'admin.adjust')
        before = cash(env)
        msg = message(env, '/大 AlLiN', index=1, mid=8100)
        await send(env, msg)
        assert receipt(env)['amount'] == 50 and receipt(env)['input_mode'] == 'allin'
        assert env.services.points.balance(env.uids[1]) == 0
        await send(env, msg)
        assert env.db.one('SELECT COUNT(*) n FROM dice_tickets')['n'] == 1
        assert cash(env) == before
    asyncio.run(run())


def test_direction_capacity_and_reverse_offset_exceed_bankroll(env):
    opened(env, 100)
    place(env, '大', 'allin')
    assert receipt(env)['amount'] == 100
    place(env, '小', 'ALLIN', index=2, mid=8101)
    assert receipt(env, 8101)['amount'] == 200
    place(env, '大', 'allin', mid=8102)
    assert receipt(env, 8102)['amount'] == 200
    assert env.db.one('SELECT accepted FROM dice_pools')['accepted'] == 500


@pytest.mark.parametrize('first,next_amount,next_side', [('allin', 100, '大'), (100, 'allin', '大'), ('allin', 'allin', '小'), (100, 99, '大')])
def test_edit_mode_amount_or_direction_is_not_new_bet(env, first, next_amount, next_side):
    opened(env, 100)
    place(env, '大', first)
    with pytest.raises(PlayError, match='不能修改旧注'):
        place(env, next_side, next_amount)
    assert receipt(env)['amount'] == 100
    assert env.services.points.balance(env.uids[1]) == 900


def test_legacy_receipt_migration_defaults_to_numeric_and_replays(env):
    opened(env)
    place(env, '大', 30)
    env.db._conn.execute('ALTER TABLE dice_group_receipts DROP COLUMN input_mode')
    migrate(env.db)
    migrate(env.db)
    assert receipt(env)['input_mode'] == 'amount'
    place(env, '大', 30)
    with pytest.raises(PlayError, match='不能修改旧注'):
        place(env, '大', 'allin')
    assert env.services.points.balance(env.uids[1]) == 970


@pytest.mark.parametrize('case,match', [('balance', '可用积分为0'), ('capacity', '已满盘'), ('integer', '安全整数额度')])
def test_zero_capacity_balance_and_safe_integer_room_do_not_reserve(env, case, match):
    if case == 'integer':
        env.services.points.add(env.uids[0], MAX_INT-1000, 'admin.adjust')
        opened(env, MAX_INT)
    else:
        opened(env, 100)
    if case == 'balance': env.services.points.add(env.uids[1], -1000, 'admin.adjust')
    if case == 'capacity': place(env, '大', 100, index=2, mid=8099)
    before = cash(env)
    with pytest.raises(PlayError, match=match):
        place(env, '大', 'allin')
    assert receipt(env) is None and cash(env) == before


def test_allin_clamps_to_safe_integer_room(env):
    env.services.points.add(env.uids[0], MAX_INT-1050, 'admin.adjust')
    opened(env, MAX_INT-50)
    place(env, '小', 'allin')
    assert receipt(env)['amount'] == 50
    assert env.services.points.balance(env.uids[1]) == 950
    assert env.db.one('SELECT accepted FROM dice_pools')['accepted'] == 50


def test_same_side_concurrent_connections_and_restart_cross_round_replay(env):
    from app.core.db import Database
    from app.modules.dice import DiceService
    from app.modules.groups import GroupService
    from app.modules.members import MemberService
    from app.modules.points import PointsService
    env.services.points.add(env.uids[0], 500, 'admin.adjust')
    row = opened(env, 1500)
    db = Database(env.db.one('PRAGMA database_list')['file'])
    other = DiceService(db, MemberService(db, GroupService(db)), PointsService(db), lambda: True, lambda c: True, '123')
    def put(args):
        service, index, mid = args
        return service.group_bet(message(env, '/大 allin', index=index, mid=mid), '大', 'allin')
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(put, [(svc(env), 1, 8100), (other, 2, 8101)]))
        assert sorted([receipt(env, 8100)['amount'], receipt(env, 8101)['amount']]) == [500, 1000]
        assert env.db.one('SELECT accepted FROM dice_pools')['accepted'] == 1500
        old_amount = receipt(env)['amount']
        fixed(env, row, [2,3,6])
        second = other.create(message(env, '/猜大小 100', index=3, mid=9000))
        assert put((other, 1, 8100))['nonce'] == row['nonce']
        assert receipt(env)['amount'] == old_amount
        assert not db.query('SELECT * FROM dice_tickets WHERE nonce=?', (second['nonce'],))
        assert len(db.query('SELECT * FROM dice_tickets')) == 2
    finally:
        db.close()


@pytest.mark.parametrize('case', ['banker', 'closed', 'deleting', 'legacy', 'thread'])
def test_allin_preserves_existing_admission_guards(env, case):
    opened(env, 100)
    if case == 'closed': env.db.execute("UPDATE dice_rounds SET state='locked'")
    if case == 'legacy': env.db.execute("UPDATE dice_rounds SET rules=json_set(rules,'$.version','dice-size-group-v1')")
    before = cash(env)
    if case == 'deleting':
        with self_deletion_guard(env.db, env.uids[1]), pytest.raises(PlayError, match='注销'):
            place(env, '大', 'allin')
    else:
        with pytest.raises(PlayError):
            place(env, '大', 'allin', index=0 if case == 'banker' else 1, thread=22 if case == 'thread' else 21)
    assert not env.db.query('SELECT * FROM dice_tickets')
    assert cash(env) == before


def test_concurrent_same_allin_message_has_one_reserve(env):
    row = opened(env, 100)
    def repeat(_):
        return place(env, '大', 'allin')['nonce']
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(repeat, range(16))) == [row['nonce']]*16
    assert env.db.one('SELECT COUNT(*) n FROM dice_tickets')['n'] == 1
    assert env.db.one('SELECT COUNT(*) n FROM dice_group_receipts')['n'] == 1
    assert env.services.points.balance(env.uids[1]) == 900
