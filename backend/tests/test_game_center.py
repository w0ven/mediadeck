"""Real SQLite tables, escrow conservation, recovery, privacy and idempotent intent."""
import asyncio
import hashlib
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.core.db import Database
from app.core.errors import ConfigError
from app.modules import zjh_engine as engine
from app.modules.account_deletion import self_deletion_guard
from app.modules.economy_rules import economy_write, encode
from app.modules.game_center import GameCenter, GameError
from app.modules.game_center_config import DEFAULTS, GameCenterPlugin, deep_link, tiers
from app.modules.game_center_delivery import GameDelivery
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.play_money import CashBook, PlayError
from app.modules.points import PointsService
from app.modules.report_delivery import CALL_DELIVERY


class FakeBot:
    enabled = True
    _bot_username = 'example_bot'

    def __init__(self):
        self._tg_profiles = {}
        self.calls = []
        self.result = 'ok'
        self.main_app = True

    def _cfg(self):
        return {'miniapp_url': 'https://example.invalid/app', 'enabled': True}

    def _token(self):
        return '123456:isolated-test'

    def _group_chat_allowed(self, chat):
        return str(chat['id']) == '-123456'

    async def _call(self, method, payload=None):
        self.calls.append((method, payload))
        if method == 'getMe':
            return {'id':123456, 'username':'example_bot', 'has_main_web_app':self.main_app}
        if self.result == 'unknown':
            CALL_DELIVERY.set({'state': 'unknown'})
            return None
        if self.result == 'retry':
            CALL_DELIVERY.set({'state': 'retry', 'retry_after': 5})
            return None
        return {'message_id': 42} if method != 'deleteMessage' else True


@pytest.fixture
def gc(tmp_path):
    db = Database(tmp_path / 'games.db')
    groups = GroupService(db)
    groups.seed_defaults()
    members, points = MemberService(db, groups), PointsService(db)
    for i in range(8):
        members.upsert(f'u{i}', f'private-login-{i}', {'group_id': 'standard'})
        members.bind_telegram(f'u{i}', str(9000 + i))
        points.add(f'u{i}', 5000, 'isolated.fixture')
    cfg = {**DEFAULTS, 'zjh_enabled': True}
    bot = FakeBot()
    service = GameCenter(db, members, points, lambda: cfg, lambda: True, lambda: bot)
    e = SimpleNamespace(db=db, members=members, points=points, cfg=cfg, s=service, bot=bot, n=0, clock=1000)
    def op(uid, op, table=None, **body):
        e.n += 1
        key = body.pop('request_id', f'intent_{e.n:08}')
        return e.s.mutate(uid, '<公开昵称&>' + uid, op, {'request_id': key, **body}, table, now=e.clock)['table']
    e.op = op
    yield e
    db.close()


@pytest.fixture
def presence(gc, monkeypatch):
    assert GameCenter.OFFLINE_SECONDS == 180
    monkeypatch.setattr('app.modules.game_center.monotonic', lambda: gc.clock)
    monkeypatch.setattr(GameCenter, 'OFFLINE_SECONDS', 90)
    gc.s._started_at = gc.clock
    return gc


def waiting_table(gc, game, n=2, tier='novice'):
    gc.cfg[game + '_enabled'] = True
    buy = {'buy_in': 100} if game == 'holdem' and tier != 'practice' else {}
    t = gc.op('u0', 'create', game=game, tier=tier, **buy)
    for i in range(1, n):
        t = gc.op(f'u{i}', 'seat', t['id'], **buy)
    return t


def create(gc, n=2, tier='novice'):
    t = gc.op('u0', 'create', game='zjh', tier=tier)
    for i in range(1, n):
        t = gc.op(f'u{i}', 'seat', t['id'])
    return t


def start(gc, n=2, tier='novice'):
    t = create(gc, n, tier)
    for i in range(1, n):
        t = gc.op(f'u{i}', 'ready', t['id'], ready=True)
    return gc.op('u0', 'start', t['id'])


def current_uid(gc, t):
    raw = json.loads(gc.db.one('SELECT state_json FROM gc_tables WHERE id=?', (t['id'],))['state_json'])
    return raw['hand']['users'][raw['hand']['engine']['current_player']]['uid']


def act(gc, t, action, **kw):
    return gc.op(current_uid(gc, t), 'actions', t['id'], hand_no=t['hand_no'], turn=t['hand']['turn'], action=action, **kw)


def total(gc):
    return (sum(gc.points.balances().values())
            + gc.db.one('SELECT COALESCE(SUM(amount),0) n FROM play_escrows')['n']
            + gc.db.one('SELECT COALESCE(SUM(amount),0) n FROM play_funds')['n'])


def deck_for(*hands):
    head = [c for hand in hands for c in hand]
    return head + [c for c in range(52) if c not in head]


def fix_deck(monkeypatch, deck):
    class Random:
        def shuffle(self, values):
            values[:] = deck
    monkeypatch.setattr('app.modules.game_center_zjh.secrets.SystemRandom', Random)


def pure(*hands, budget=100, allow_235=False):
    return engine.new_hand([{'seat': i, 'player': str(i), 'stack': budget} for i in range(len(hands))],
                           1, deck_for(*hands), allow_235=allow_235, secret='ab' * 32)


@pytest.mark.parametrize('hand,value', [([12,25,38],5), ([0,1,12],4), ([1,5,10],3),
                                      ([12,13,27],2), ([0,13,5],1), ([0,16,33],0)])
def test_strength_and_a23(hand, value):
    assert engine.strength(hand)[0] == value
    if value in (2, 4):
        assert engine.strength(hand)[1] == 3


def test_pure_inputs_and_view_cannot_mutate_engine():
    state = pure([12,25,38], [11,24,37])
    after = engine.apply_action(state, '0', 'look')
    assert state['players'][0]['seen'] is False
    assert after['turn'] == state['turn']
    v = engine.view(after, '0')
    v['players'][0]['cards'].clear()
    assert after['players'][0]['cards'] == [12,25,38]
    assert engine.view(after, '1')['players'][0]['cards'] is None
    with pytest.raises(engine.EngineError):
        engine.apply_action(state, '1', 'call')
    with pytest.raises(engine.EngineError):
        engine.new_hand([], 1, list(range(52)), secret='aa')


def test_compare_tie_initiator_loses_and_private_views():
    s = pure([0,14,30], [13,27,4], [12,25,38])
    for actor in ('0', '1', '2'):
        s = engine.apply_action(s, actor, 'call')
    s = engine.apply_action(s, '0', 'look')
    s = engine.apply_action(s, '0', 'compare', target='1')
    assert s['players'][0]['folded']
    assert engine.view(s, '0')['comparisons'] == engine.view(s, '1')['comparisons']
    assert engine.view(s, '2')['comparisons'] == []
    assert engine.view(s, '2')['players'][0]['cards'] is None


@pytest.mark.parametrize('enabled,winner', [(False, '1'), (True, '0')])
def test_235_option(enabled, winner):
    s = pure([0,14,29], [12,25,38], budget=1, allow_235=enabled)
    s = engine.apply_action(s, '0', 'all_in')
    assert s['result']['winners'] == [winner]
    assert sum(s['result']['net'].values()) == 0


def test_betting_round_limit_raise_cap_and_all_in():
    s = pure([0,14,30], [12,25,38], budget=500)
    for _ in range(4):
        s = engine.apply_action(s, engine.current_player(s), 'raise')
    assert s['bet'] == 5 and 'raise' not in engine.legal_actions(s, engine.current_player(s))
    while not engine.is_finished(s):
        s = engine.apply_action(s, engine.current_player(s), 'call')
    assert s['round'] == 10 and s['reason'] == 'round_limit'
    assert sum(s['result']['net'].values()) == 0
    small = pure([12,25,38], [0,14,30], budget=2)
    small = engine.apply_action(small, '0', 'look')
    assert engine.legal_actions(small, '0')['all_in']['amount'] == 1
    small = engine.apply_action(small, '0', 'all_in')
    assert small['reason'] == 'all_in'
    assert sum(small['result']['awards'].values()) == 3


def test_equal_showdown_fair_split_reproducible():
    s = pure([0,14,30], [13,27,4], [12,25,38], budget=1)
    s = engine.force_fold(s, '2')
    s = engine.apply_action(s, '0', 'all_in')
    assert sorted(s['result']['awards'].values()) == [0, 1, 2]
    assert s['result']['winners'] == ['0', '1']
    assert sum(s['result']['net'].values()) == 0


@pytest.mark.parametrize('hand,expected', [([12,25,38],'call'), ([0,16,33],'fold')])
def test_auto_strategy_looks_then_conservative(hand, expected):
    s = pure(hand, [1,14,27])
    assert engine.auto_action(s, '0') == 'look'
    s = engine.apply_action(s, '0', 'look')
    assert engine.auto_action(s, '0') == expected
    s['players'][0]['budget'] = s['players'][0]['invested']
    assert engine.auto_action(s, '0') == ('all_in' if expected == 'call' else 'fold')


def test_out_of_turn_look_repeat_and_folded_exclusion(gc):
    t = start(gc, 3)
    deadline, current, turn = t['turn_deadline'], t['hand']['current_player'], t['hand']['turn']
    other = gc.s.snapshot(t['id'], 'u1', gc.clock)['table']
    assert other['hand']['legal_actions'] == {'look': {}}
    gc.clock += 1
    t = gc.op('u1', 'actions', t['id'], hand_no=1, turn=turn, action='look')
    assert t['hand']['players'][1]['cards'] is not None
    assert t['hand']['turn'] == turn and t['hand']['current_player'] == current
    assert t['turn_deadline'] == deadline and t['auto_at'] is None
    revision = t['revision']
    rows = gc.db.one('SELECT COUNT(*) n FROM play_cash_events')['n']
    t = gc.op('u1', 'actions', t['id'], hand_no=1, turn=turn, action='look')
    assert t['revision'] == revision + 1 and t['hand']['turn'] == turn
    assert t['hand']['legal_actions'] == {}
    assert gc.db.one('SELECT COUNT(*) n FROM play_cash_events')['n'] == rows
    t = gc.op('u0', 'auto', t['id'], enabled=True)
    auto_at = t['auto_at']
    t = gc.op('u2', 'actions', t['id'], hand_no=1, turn=turn, action='look')
    assert t['auto_at'] == auto_at and t['turn_deadline'] == deadline
    t = gc.op('u0', 'auto', t['id'], enabled=False)
    t = act(gc, t, 'call')
    hand = gc.s.snapshot(t['id'], 'u1', gc.clock)['table']['hand']
    assert hand['legal_actions']['call']['amount'] == 2
    t = act(gc, t, 'fold')
    with pytest.raises(GameError, match='当前不能'):
        gc.op('u1', 'actions', t['id'], hand_no=1, turn=t['hand']['turn'], action='look')


def test_atomic_freeze_settlement_and_commitment(gc):
    initial = total(gc)
    t = start(gc)
    assert gc.points.balance('u0') == 4950
    assert total(gc) == initial
    proof = gc.s.proof(t['id'], 1)
    assert set(proof) == {'table_id', 'hand_no', 'commitment', 'settled'}
    t = act(gc, t, 'fold')
    assert t['phase'] == 'waiting'
    assert total(gc) == initial
    assert gc.s.wallet('u0', gc.clock)['frozen'] == 0
    proof = gc.s.proof(t['id'], 1)
    assert hashlib.sha256((proof['deck_codes'] + '|' + proof['salt']).encode()).hexdigest() == proof['commitment']
    assert sorted(proof['deck']) == list(range(52))
    assert all(not p['ready'] for p in t['seats'])
    assert sum(proof['result']['net'].values()) == 0
    details = gc.s.cash_detail(t['id'])['events']
    assert {'reserve', 'consume', 'release', 'payout'} <= {r['operation'] for r in details}
    assert not gc.db.one('SELECT 1 FROM play_funds WHERE amount<>0')


@pytest.mark.parametrize('seed', range(12))
def test_full_games_conserve_every_action(gc, seed):
    rng = random.Random(seed)
    t = start(gc, 5)
    initial = total(gc)
    for _ in range(110):
        if t['phase'] != 'playing':
            break
        uid = current_uid(gc, t)
        hand = gc.s.snapshot(t['id'], uid, gc.clock)['table']['hand']
        choices = list(hand['legal_actions'])
        action = rng.choice(choices)
        extra = {'target': rng.choice(hand['legal_actions'][action]['targets'])} if action == 'compare' else {}
        t = act(gc, t, action, **extra)
        assert total(gc) == initial
        assert all(v >= 0 for v in gc.points.balances().values())
    assert t['phase'] == 'waiting'
    assert gc.db.one('SELECT SUM(amount) n FROM play_escrows')['n'] == 0


def test_supreme_freezes_all_available_and_requires_minimum(gc):
    t = start(gc, tier='supreme')
    assert gc.points.balance('u0') == 0
    rows = gc.db.query('SELECT reserved FROM play_escrows')
    assert [r['reserved'] for r in rows] == [5000, 5000]
    act(gc, t, 'fold')
    gc.points.add('u0', -4000, 'isolated.adjust')
    with pytest.raises(GameError, match='余额不足'):
        gc.op('u0', 'ready', t['id'], ready=True)


def test_host_conditions_unready_spectates_and_transfer(gc):
    t = create(gc, 3)
    with pytest.raises(GameError, match='只有房主'):
        gc.op('u1', 'start', t['id'])
    with pytest.raises(GameError, match='至少需要2人'):
        gc.op('u0', 'start', t['id'])
    gc.op('u1', 'ready', t['id'], ready=True)
    t = gc.op('u0', 'start', t['id'])
    assert len(t['hand']['players']) == 2
    assert gc.s.snapshot(t['id'], 'u2', gc.clock)['table']['hand']['legal_actions'] == {}
    t = act(gc, t, 'fold')
    t = gc.op('u0', 'leave', t['id'])
    assert t['host'] == next(p['player'] for p in t['seats'] if p['seat'] == 1)
    assert gc.s.active_tables('u0') == []


def test_host_waiting_inactivity_does_not_follow_other_users_activity(gc):
    t = create(gc, 3)
    old_host = t['host']
    gc.clock += 179
    gc.op('u2', 'ready', t['id'], ready=True)
    gc.s.tick(now=1180)
    t = gc.s.snapshot(t['id'], 'u0', 1180)['table']
    assert t['host'] != old_host
    assert t['host'] == next(p['player'] for p in t['seats'] if p['seat'] == 1)


def test_all_ready_countdown_cancel_join_and_cancel_ready(gc):
    t = create(gc)
    gc.op('u0', 'ready', t['id'], ready=True)
    t = gc.op('u1', 'ready', t['id'], ready=True)
    assert t['countdown_at'] == 1005
    gc.clock = 1002
    t = gc.op('u2', 'seat', t['id'])
    assert t['countdown_at'] is None
    t = gc.op('u2', 'ready', t['id'], ready=True)
    assert t['countdown_at'] == 1007
    t = gc.op('u0', 'ready', t['id'], ready=False)
    assert t['countdown_at'] is None
    t = gc.op('u0', 'ready', t['id'], ready=True)
    gc.s.tick(now=1006.9)
    assert gc.s.snapshot(t['id'], 'u0', 1006.9)['table']['phase'] == 'waiting'
    gc.s.tick(now=1007)
    assert gc.s.snapshot(t['id'], 'u0', 1007)['table']['phase'] == 'playing'


def test_short_balance_at_start_recheck_does_not_debit_anyone(gc):
    t = create(gc)
    gc.op('u1', 'ready', t['id'], ready=True)
    gc.points.add('u1', -4999, 'isolated.adjust')
    initial = total(gc)
    with pytest.raises(GameError, match='至少需要2人'):
        gc.op('u0', 'start', t['id'])
    assert total(gc) == initial
    assert gc.db.one('SELECT COUNT(*) n FROM play_escrows')['n'] == 0
    with pytest.raises(GameError, match='余额不足'):
        gc.op('u1', 'ready', t['id'], ready=True)


def test_expired_all_ready_countdown_removes_unfunded_readiness(gc):
    t = create(gc)
    gc.op('u0', 'ready', t['id'], ready=True)
    gc.op('u1', 'ready', t['id'], ready=True)
    gc.points.add('u1', -4999, 'isolated.adjust')
    gc.s.tick(now=1005)
    t = gc.s.snapshot(t['id'], 'u1', 1005)['table']
    assert t['phase'] == 'waiting' and t['countdown_at'] is None
    assert not t['seats'][1]['ready'] and t['me']['notice']


def test_idle_dissolution_and_no_debit(gc):
    t = create(gc, 3)
    initial = total(gc)
    gc.s.tick(now=1600)
    assert gc.s.snapshot(t['id'], 'u0', 1600)['table']['phase'] == 'closed'
    assert gc.s.active_tables('u0') == []
    assert total(gc) == initial


def test_quick_start_one_table_capacity_and_deep_link(gc):
    t = create(gc, 5)
    assert 'startapp=gc_' + t['id'] in t['deep_link']
    assert 'private-login' not in encode(gc.s.list_tiers('zjh'))
    with pytest.raises(GameError, match='已满'):
        gc.op('u5', 'seat', t['id'])
    other = gc.op('u5', 'quick', game='zjh', tier='novice')
    assert other['id'] != t['id']
    joined = gc.op('u6', 'quick', game='zjh', tier='novice')
    assert joined['id'] == other['id']
    assert gc.op('u6', 'quick', game='zjh', tier='novice')['id'] == other['id']
    with pytest.raises(GameError, match='另一张'):
        gc.op('u0', 'seat', other['id'])
    with pytest.raises(GameError, match='另一张'):
        gc.op('u0', 'create', game='zjh', tier='normal')
    assert gc.db.one('SELECT COUNT(*) n FROM gc_tables')['n'] == 2


def test_running_join_spectates_and_leaving_preserves_money(gc):
    t = start(gc, 3)
    deadline = t['turn_deadline']
    t = gc.op('u3', 'seat', t['id'])
    assert t['turn_deadline'] == deadline and len(t['hand']['players']) == 3
    initial = total(gc)
    t = gc.op('u2', 'leave', t['id'])
    assert t['seats'][2]['leave_after']
    assert t['turn_deadline'] == deadline
    t = act(gc, t, 'fold')
    assert t['phase'] == 'waiting'
    assert gc.s.active_tables('u2') == []
    assert total(gc) == initial


def test_restart_recovers_original_deadline_and_auto_limit(gc, monkeypatch):
    fix_deck(monkeypatch, list(range(52)))
    t = start(gc)
    first = t['turn_deadline']
    original_commitment = t['hand']['commitment']
    gc.s = GameCenter(gc.db, gc.members, gc.points, lambda: gc.cfg, lambda: True)
    gc.s.tick(now=first - .01)
    assert gc.s.snapshot(t['id'], 'u0', first - .01)['table']['turn_deadline'] == first
    gc.s.tick(now=first)
    after = gc.s.snapshot(t['id'], 'u0', first)['table']
    assert after['hand']['commitment'] == original_commitment
    assert after['seats'][0]['auto'] and '已超时' in after['me']['notice']
    assert after['hand']['turn'] > 1
    before = total(gc)
    gc.s.tick(now=first + 1500)
    assert total(gc) == before
    assert not gc.s.active_tables('u0') and not gc.s.active_tables('u1')
    hands = gc.db.query('SELECT * FROM gc_hands ORDER BY hand_no')
    assert len(hands) == 3 and all(h['settled_at'] for h in hands)
    assert gc.db.one('SELECT SUM(amount) n FROM play_escrows')['n'] == 0


def test_disk_reopen_does_not_redeal_or_reset_countdown(gc):
    t = create(gc)
    gc.op('u0', 'ready', t['id'], ready=True)
    gc.op('u1', 'ready', t['id'], ready=True)
    # A second real SQLite connection models process reconstruction, not a copied in-memory fixture.
    path = gc.db._conn.execute('PRAGMA database_list').fetchone()['file']
    other_db = Database(path)
    try:
        other_members = MemberService(other_db, GroupService(other_db))
        s = GameCenter(other_db, other_members, PointsService(other_db), lambda: gc.cfg, lambda: True)
        s.tick(now=1005)
        state = s.snapshot(t['id'], 'u0', 1005)['table']
        assert state['turn_deadline'] == 1035
        commit = state['hand']['commitment']
        rebuilt = GameCenter(other_db, other_members, PointsService(other_db), lambda: gc.cfg, lambda: True)
        rebuilt.tick(now=1006)
        assert rebuilt.snapshot(t['id'], 'u0', 1006)['table']['hand']['commitment'] == commit
    finally:
        other_db.close()


def test_auto_switch_no_deadline_extension_and_late_action_rolls_back_only_intent(gc, monkeypatch):
    fix_deck(monkeypatch, list(range(52)))
    t = start(gc)
    deadline = t['turn_deadline']
    gc.clock += 1
    t = gc.op('u0', 'auto', t['id'], enabled=True)
    assert t['auto_at'] == gc.clock + 1.5
    gc.clock += 1
    t = gc.op('u0', 'auto', t['id'], enabled=False)
    assert t['auto_at'] is None and t['turn_deadline'] == deadline
    gc.clock = deadline + 1
    with pytest.raises(GameError) as exc:
        gc.op('u0', 'actions', t['id'], hand_no=1, turn=1, action='fold')
    assert exc.value.code == 'stale_turn'
    assert gc.s.snapshot(t['id'], 'u0', gc.clock)['table']['seats'][0]['auto']


def test_concurrent_idempotency_and_multiple_windows(gc):
    t = create(gc)
    gc.op('u1', 'ready', t['id'], ready=True)
    body = {'request_id': 'start_shared_123', 'ignored': 'stable'}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: gc.s.mutate('u0', '公开', 'start', body, t['id'], now=gc.clock), range(4)))
    assert {r['table']['hand_no'] for r in results} == {1}
    assert gc.db.one('SELECT COUNT(*) n FROM play_escrows')['n'] == 2
    with pytest.raises(GameError) as exc:
        gc.s.mutate('u0', '公开', 'start', {**body, 'ignored': 'different'}, t['id'], now=gc.clock)
    assert exc.value.code == 'idempotency_conflict'
    def window(i):
        try:
            return gc.s.mutate('u0', '公开', 'actions', {'request_id': f'window_{i:08}', 'hand_no':1, 'turn':1, 'action':'call'}, t['id'], now=gc.clock)
        except GameError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(window, range(4)))
    assert sum(isinstance(v, dict) for v in outcomes) == 1
    assert outcomes.count('stale_turn') == 3
    assert total(gc) == 40000


def test_private_look_compare_no_other_cards_in_any_snapshot(gc, monkeypatch):
    fix_deck(monkeypatch, deck_for([12,25,38], [11,24,37], [0,16,33]))
    t = start(gc, 3)
    for _ in range(3):
        t = act(gc, t, 'call')
    t = act(gc, t, 'look')
    assert t['hand']['players'][0]['cards'] == [12,25,38]
    assert gc.s.snapshot(t['id'], 'u1', gc.clock)['table']['hand']['players'][0]['cards'] is None
    target = t['seats'][1]['player']
    t = act(gc, t, 'compare', target=target)
    assert t['hand']['comparisons'][0]['cards'][target] == [11,24,37]
    for uid in ('u2', 'u7'):
        v = gc.s.snapshot(t['id'], uid, gc.clock)['table']
        assert v['hand']['comparisons'] == []
        assert all(p['cards'] is None and p['hand_name'] is None for p in v['hand']['players'])
        assert all(secret not in encode(v) for secret in ('private-login', 'owner_uid', 'salt', 'deck_codes', 'budget'))
    assert 'deck' not in gc.s.proof(t['id'], 1)


def test_practice_daily_top_up_private_bots_and_no_point_ledger(gc):
    point_rows = gc.db.one('SELECT COUNT(*) n FROM points_ledger')['n']
    t = gc.op('u0', 'create', game='zjh', tier='practice')
    assert sum(p['bot'] for p in t['seats']) == 4
    with pytest.raises(GameError) as exc:
        gc.s.snapshot(t['id'], 'u1', gc.clock)
    assert exc.value.status == 404
    t = gc.op('u0', 'start', t['id'])
    assert t['me']['wallet']['beans'] == 9500
    gc.op('u0', 'leave', t['id'])
    gc.s.tick(now=2000)
    assert gc.db.one('SELECT COUNT(*) n FROM points_ledger')['n'] == point_rows
    assert gc.db.one('SELECT COUNT(*) n FROM play_cash_events')['n'] == 0
    gc.db.execute('UPDATE gc_beans SET balance=100 WHERE user_id=?', ('u0',))
    assert gc.s.wallet('u0', 2000)['beans'] == 100
    assert gc.s.wallet('u0', 2000 + 86400)['beans'] == 10000
    gc.db.execute('UPDATE gc_beans SET balance=12000 WHERE user_id=?', ('u0',))
    assert gc.s.wallet('u0', 2000 + 2*86400)['beans'] == 12000


def test_beans_recovery_clock_never_rolls_daily_refill_back(gc):
    gc.s.wallet('u0', 1000 + 86400)
    gc.db.execute('UPDATE gc_beans SET balance=100 WHERE user_id=?', ('u0',))
    assert gc.s.wallet('u0', 1000)['beans'] == 100
    assert gc.s.wallet('u0', 1000 + 86400)['beans'] == 100
    assert gc.s.wallet('u0', 1000 + 2*86400)['beans'] == 10000


def test_start_midway_cash_failure_is_fully_atomic(gc, monkeypatch):
    t = create(gc)
    gc.op('u1', 'ready', t['id'], ready=True)
    before = total(gc)
    reserve = gc.s.cash.reserve
    def fail_second(conn, scope, ref, uid, amount, **kwargs):
        if uid == 'u1':
            raise PlayError('injected local transaction failure')
        return reserve(conn, scope, ref, uid, amount, **kwargs)
    monkeypatch.setattr(gc.s.cash, 'reserve', fail_second)
    with pytest.raises(PlayError):
        gc.op('u0', 'start', t['id'], request_id='retry_atomic_123')
    assert gc.db.one('SELECT COUNT(*) n FROM play_escrows')['n'] == 0
    assert gc.points.balance('u0') == 5000 and total(gc) == before
    assert gc.s.snapshot(t['id'], 'u0', gc.clock)['table']['hand_no'] == 0
    monkeypatch.setattr(gc.s.cash, 'reserve', reserve)
    t = gc.op('u0', 'start', t['id'], request_id='retry_atomic_123')
    assert t['hand_no'] == 1 and total(gc) == before


def test_off_switch_does_not_trap_funds_and_also_gates_holdem(gc):
    t = start(gc)
    gc.s.enabled = lambda: False
    with pytest.raises(GameError) as exc:
        gc.op('u2', 'seat', t['id'])
    assert exc.value.code == 'disabled'
    gc.op('u0', 'leave', t['id'])
    assert gc.s.wallet('u0', gc.clock)['frozen'] == 0
    with pytest.raises(GameError) as exc:
        gc.op('u0', 'create', game='holdem', tier='novice', buy_in=100)
    assert exc.value.code == 'disabled'
    assert len(gc.s.list_tiers('holdem')['tiers']) == 6


def test_cash_transfer_top_up_audit_idempotency_and_no_per_hand_ledger(gc):
    cash = CashBook(gc.points)
    before = total(gc)
    with economy_write(gc.db) as conn:
        for uid in ('u0', 'u1'):
            cash.reserve(conn, 'gc_holdem', 'table', uid, 100, reason='game.buy_in')
    rows = gc.db.one('SELECT COUNT(*) n FROM points_ledger')['n']
    with economy_write(gc.db) as conn:
        assert cash.transfer(conn, 'gc_holdem', 'table', 'u0', 'u1', 90, 'hand1')
        assert not cash.transfer(conn, 'gc_holdem', 'table', 'u0', 'u1', 90, 'hand1')
    assert gc.db.one('SELECT COUNT(*) n FROM points_ledger')['n'] == rows
    assert total(gc) == before
    for args in [('u0', 'u1', 11, 'overdraw'), ('u0', 'missing', 1, 'missing'), ('u0', 'u1', 89, 'hand1')]:
        with pytest.raises(PlayError), economy_write(gc.db) as conn:
            cash.transfer(conn, 'gc_holdem', 'table', *args)
    assert total(gc) == before
    with economy_write(gc.db) as conn:
        assert cash.top_up(conn, 'gc_holdem', 'table', 'u0', 50, 'topup')
        assert not cash.top_up(conn, 'gc_holdem', 'table', 'u0', 50, 'topup')
        assert cash.held(conn, 'gc_holdem', 'table', 'u0') == 60
        assert cash.held(conn, 'gc_holdem', 'table', 'u1') == 190
        cash.release(conn, 'gc_holdem', 'table', 'u0', reason='game.cash_out')
        cash.release(conn, 'gc_holdem', 'table', 'u1', reason='game.cash_out')
    assert total(gc) == before
    assert gc.points.balance('u0') == 4910 and gc.points.balance('u1') == 5090
    reasons = {r['reason'] for r in gc.points.ledger('u0') if r['reason'] != 'isolated.fixture'}
    assert reasons == {'game.buy_in', 'game.top_up', 'game.cash_out'}
    assert {r['operation'] for r in gc.s.cash_detail('table')['events']} == {'reserve', 'transfer_in', 'transfer_out', 'top_up', 'release'}
    assert not gc.db.one('SELECT 1 FROM play_escrows WHERE amount+spent+released<>reserved')


def test_deletion_exclusion_both_directions(gc):
    t = create(gc)
    with pytest.raises(ConfigError, match='游戏中心'), self_deletion_guard(gc.db, 'u0'):
        pass
    gc.op('u0', 'leave', t['id'])
    with self_deletion_guard(gc.db, 'u0'), pytest.raises(GameError, match='注销'):
        gc.op('u0', 'seat', t['id'])


def test_config_validation_and_status(gc):
    plugin = GameCenterPlugin(SimpleNamespace(db=gc.db))
    assert not DEFAULTS['zjh_enabled'] and not DEFAULTS['broadcast_enabled']
    assert len(tiers(DEFAULTS, 'zjh')) == len(tiers(DEFAULTS, 'holdem')) == 6
    with pytest.raises(ValueError):
        plugin.normalize_config({**DEFAULTS, 'zjh_tiers': '[]'})
    with pytest.raises(ValueError):
        plugin.normalize_config({**DEFAULTS, 'broadcast_groups': '123'})
    t = start(gc)
    assert plugin.readonly_status()['在桌人数'] == 2
    assert gc.s.cash_detail(user_id='u0')['events']
    assert all(r['user_id'] == 'u0' for r in gc.s.cash_detail(t['id'], 'u0')['events'])
    assert deep_link('https://example.invalid/app?x=1', 'games').endswith('x=1&tgWebAppStartParam=games')
    assert not deep_link('javascript:alert(1)')


def test_outbox_send_edit_delete_same_card(gc):
    gc.cfg.update(broadcast_enabled=True, broadcast_groups='-123456\n-7890')
    delivery = GameDelivery(gc.s)
    t = create(gc, 1)
    assert gc.db.one('SELECT COUNT(*) n FROM gc_outbox')['n'] == 1
    asyncio.run(delivery.deliver())
    assert gc.bot.calls[-1][0] == 'sendMessage'
    sent = gc.bot.calls[-1][1]
    assert 'private-login' not in encode(sent) and '5000' not in encode(sent)
    assert t['id'] in sent['reply_markup']['inline_keyboard'][0][0]['url']
    gc.op('u1', 'seat', t['id'])
    asyncio.run(delivery.deliver())
    assert gc.bot.calls[-1][0] == 'editMessageText'
    assert gc.bot.calls[-1][1]['message_id'] == 42
    gc.op('u1', 'ready', t['id'], ready=True)
    gc.op('u0', 'start', t['id'])
    asyncio.run(delivery.deliver())
    assert gc.bot.calls[-1] == ('deleteMessage', {'chat_id': '-123456', 'message_id': 42})
    assert gc.db.one('SELECT state FROM gc_outbox')['state'] == 'deleted'


@pytest.mark.parametrize('outcome', ['unknown', 'crash', 'server_error'])
def test_outbox_unknown_initial_send_never_replayed(gc, outcome):
    gc.cfg.update(broadcast_enabled=True, broadcast_groups='-123456')
    t = create(gc, 1)
    delivery = GameDelivery(gc.s)
    job = delivery.claim('123456', now=1001)
    assert job['op'] == 'send' and delivery.claim('123456', now=1002) is None
    if outcome != 'crash':
        error = {'state':'unknown'} if outcome == 'unknown' else {'state':'retry', 'reason':'Telegram 服务暂不可用'}
        delivery.done(job, error=error, now=1002)
    recovered = GameDelivery(gc.s)
    assert recovered.claim('123456', now=1200) is None
    assert gc.db.one('SELECT state FROM gc_outbox')['state'] == 'unknown'
    gc.clock = 1201
    gc.op('u1', 'seat', t['id'])
    assert recovered.claim('123456', now=1300) is None


def test_outbox_definite_failure_retries_and_late_ack_deletes_after_close(gc):
    gc.cfg.update(broadcast_enabled=True, broadcast_groups='-123456')
    t = create(gc, 1)
    d = GameDelivery(gc.s)
    job = d.claim('123456', now=1000)
    d.done(job, error={'state':'retry'}, now=1000)
    job = d.claim('123456', now=1100)
    assert job and job['op'] == 'send'
    gc.op('u0', 'leave', t['id'])
    assert d.done(job, {'message_id':42}, now=1101)
    delete = d.claim('123456', now=1102)
    assert delete['op'] == 'delete'
    d.done(delete, error={'state':'unknown'}, now=1102)
    retry = d.claim('123456', now=1500)
    assert retry['op'] == 'delete' and retry['message_id'] == 42
    assert not d.done(delete, True, now=1501)  # stale lease cannot acknowledge newer work
    d.done(retry, True, now=1502)
    assert d.claim('123456', now=2000) is None


def test_outbox_never_sends_closed_unpublished_table_or_other_bot(gc):
    gc.cfg.update(broadcast_enabled=True, broadcast_groups='-123456')
    t = create(gc, 1)
    d = GameDelivery(gc.s)
    assert d.claim('654321', now=1001) is None
    gc.op('u0', 'leave', t['id'])
    assert d.claim('123456', now=1001) is None
    assert gc.db.one('SELECT state FROM gc_outbox')['state'] == 'deleted'


def test_outbox_requires_verified_main_mini_app_and_deletion_not_found_is_success(gc):
    from app.modules.telegram import _CALL_ERROR
    gc.cfg.update(broadcast_enabled=True, broadcast_groups='-123456')
    t = create(gc, 1)
    d = GameDelivery(gc.s)
    gc.bot.main_app = False
    asyncio.run(d.deliver())
    assert [method for method, _ in gc.bot.calls] == ['getMe']
    assert gc.db.one('SELECT state FROM gc_outbox')['state'] == 'pending'
    gc.bot.main_app = True
    gc.db.execute('UPDATE gc_outbox SET due_at=0')
    asyncio.run(d.deliver())
    gc.op('u0', 'leave', t['id'])
    original = gc.bot._call
    async def missing(method, payload=None):
        if method == 'deleteMessage':
            _CALL_ERROR.set('Bad Request: message to delete not found')
            return None
        return await original(method, payload)
    gc.bot._call = missing
    asyncio.run(d.deliver())
    assert gc.db.one('SELECT state FROM gc_outbox')['state'] == 'deleted'


@pytest.mark.parametrize('allowed', [True, False])
def test_outbox_resolves_authorized_group_username_before_sending(gc, allowed):
    gc.cfg.update(broadcast_enabled=True, broadcast_groups='-123456')
    gc.bot._group_allowlist = lambda: ['@example_group']
    gc.bot._group_chat_allowed = lambda chat: chat.get('username') == 'example_group'
    original = gc.bot._call
    async def lookup(method, payload=None):
        if method == 'getChat':
            return {'id':-123456, 'type':'supergroup', 'username':'example_group' if allowed else 'other_group'}
        return await original(method, payload)
    gc.bot._call = lookup
    create(gc, 1)
    asyncio.run(GameDelivery(gc.s).deliver())
    assert any(method == 'sendMessage' for method, _ in gc.bot.calls) == allowed
    assert gc.db.one('SELECT state FROM gc_outbox')['state'] == ('sent' if allowed else 'pending')


def test_bot_entry_gates(gc):
    from app.modules.bot_play_hub import PlayHubBotMixin
    class Hub(FakeBot, PlayHubBotMixin):
        on = False
        def _plugin_on(self, plugin):
            return plugin == 'game_center' and self.on
    hub = Hub()
    panel = {'nonce':'test', 'chat_id':'9', 'tg_id':'9'}
    assert '游戏中心' not in encode(hub._play_hub_view(panel, {'view':'playhub'}))
    hub.on = True
    assert 'startapp=games' in encode(hub._play_hub_view(panel, {'view':'playhub'}))
    hub._cfg = lambda: {'miniapp_url':''}
    assert '游戏中心' not in encode(hub._play_hub_view(panel, {'view':'playhub'}))


def test_existing_renewal_notice_includes_table_frozen_amount(gc):
    from app.modules.level_lifecycle import LevelLifecyclePlugin
    expiry = int(time.time()) + 100
    member = {'emby_user_id':'u0', 'tg_user_id':'9000', 'activity_managed':True, 'expires_at_effective':expiry}
    calls = []
    async def send(chat, body):
        calls.append(body)
        return True
    levels = SimpleNamespace(db=gc.db, members=SimpleNamespace(list=lambda **kw:[member]),
                             status=lambda *a:{'renewal_price':10000,'shortfall':5050})
    plugin = LevelLifecyclePlugin(SimpleNamespace(telegram=SimpleNamespace(enabled=True,send=send)))
    start(gc)
    asyncio.run(plugin.notices(levels, int(time.time())))
    assert '有 49 积分正冻结在牌桌' in calls[0]


def test_running_zjh_host_leave_transfers_before_settlement(gc):
    t = start(gc, 3)
    deadline = t['turn_deadline']
    t = act(gc, t, 'call')
    successor = t['seats'][1]['player']
    t = gc.op('u0', 'leave', t['id'])
    assert t['phase'] == 'playing' and t['seats'][0]['leave_after']
    assert t['host'] == successor
    assert t['turn_deadline'] == deadline
    assert total(gc) == 40000


def test_transfer_host_never_retains_a_departing_host(gc):
    t = start(gc, 3)
    raw = json.loads(gc.db.one('SELECT state_json FROM gc_tables WHERE id=?', (t['id'],))['state_json'])
    for p in raw['seats']:
        p['leave_after'] = True
    gc.s._transfer_host(raw, gc.clock)
    assert raw['host'] is None


def test_start_status_distinguishes_missing_seats_ready_and_funds(gc):
    t = create(gc, 1)
    assert t['start_status']['reason'] == 'waiting_players'
    assert not t['start_status']['can_start']
    t = gc.op('u1', 'seat', t['id'])
    assert t['start_status']['reason'] == 'waiting_ready'
    t = gc.op('u1', 'ready', t['id'], ready=True)
    assert t['start_status']['reason'] == 'waiting_host'
    t = gc.s.snapshot(t['id'], 'u0', gc.clock)['table']
    assert t['start_status']['can_start'] and t['start_status']['eligible_players'] == 2
    gc.points.add('u1', -4999, 'isolated.adjust')
    t = gc.s.snapshot(t['id'], 'u0', gc.clock)['table']
    assert t['start_status']['reason'] == 'ineligible_players'
    assert t['start_status']['eligible_players'] == 1
    assert not t['start_status']['can_start']
    assert 'u1' not in encode(t['start_status']) and '4999' not in encode(t['start_status'])
    gc.points.add('u1', 49, 'isolated.adjust')
    t = gc.op('u0', 'start', t['id'])
    assert t['start_status']['reason'] == 'playing' and not t['start_status']['can_start']


def test_start_status_allows_underfunded_host_to_start_two_ready_players(gc):
    t = create(gc, 3)
    for uid in ('u1', 'u2'):
        gc.op(uid, 'ready', t['id'], ready=True)
    gc.points.add('u0', -4999, 'isolated.adjust')
    t = gc.s.snapshot(t['id'], 'u0', gc.clock)['table']
    assert t['start_status']['can_start'] and t['start_status']['eligible_players'] == 2
    t = gc.op('u0', 'start', t['id'])
    assert len(t['hand']['players']) == 2
    assert t['me']['wallet']['points'] == 1 and t['me']['wallet']['frozen'] == 0


def test_supreme_insufficient_balance_explains_minimum_not_nonexistent_cap(gc):
    t = create(gc, tier='supreme')
    gc.points.add('u0', -4999, 'isolated.adjust')
    with pytest.raises(GameError) as exc:
        gc.op('u0', 'ready', t['id'], ready=True)
    assert '2500' in str(exc.value) and '全部可用积分' in str(exc.value)
    assert '封顶' not in str(exc.value)


@pytest.mark.parametrize('automatic', [False, True])
def test_successful_readiness_clears_old_funding_notice(gc, automatic):
    t = create(gc)
    for uid in ('u0', 'u1'):
        gc.op(uid, 'ready', t['id'], ready=True)
    gc.op('u1', 'auto', t['id'], enabled=automatic)
    gc.points.add('u1', -4999, 'isolated.adjust')
    gc.clock += 5
    gc.s.tick(now=gc.clock)
    assert gc.s.snapshot(t['id'], 'u1', gc.clock)['table']['me']['notice']
    gc.points.add('u1', 49, 'isolated.adjust')
    t = gc.op('u1', 'ready', t['id'], ready=True)
    assert t['me']['notice'] == ''


def test_successful_manual_start_clears_old_host_funding_notice(gc):
    t = create(gc)
    for uid in ('u0', 'u1'):
        gc.op(uid, 'ready', t['id'], ready=True)
    gc.points.add('u0', -4999, 'isolated.adjust')
    gc.clock += 5
    gc.s.tick(now=gc.clock)
    assert gc.s.snapshot(t['id'], 'u0', gc.clock)['table']['me']['notice']
    gc.points.add('u0', 49, 'isolated.adjust')
    t = gc.op('u0', 'start', t['id'])
    assert t['phase'] == 'playing' and t['me']['notice'] == ''


def test_repeated_snapshots_do_not_write_beans_or_other_state(gc):
    t = start(gc)
    gc.s.snapshot(t['id'], 'u0', gc.clock)
    before = gc.db._conn.total_changes
    for _ in range(10):
        gc.s.snapshot(t['id'], 'u0', gc.clock)
    assert gc.db._conn.total_changes == before


def test_settled_hand_and_unchanged_outbox_are_not_rewritten(gc):
    gc.cfg.update(broadcast_enabled=True, broadcast_groups='-123456')
    t = start(gc)
    t = act(gc, t, 'fold')
    gc.s.snapshot(t['id'], 'u0', gc.clock)
    raw = json.loads(gc.db.one('SELECT state_json FROM gc_tables WHERE id=?', (t['id'],))['state_json'])
    before = gc.db._conn.total_changes
    with economy_write(gc.db) as conn:
        gc.s._save(conn, raw, gc.clock)
    assert gc.db._conn.total_changes - before == 1  # table revision only


def test_ready_and_auto_toggles_do_not_reedit_identical_recruitment_card(gc):
    gc.cfg.update(broadcast_enabled=True, broadcast_groups='-123456')
    t = create(gc)
    d = GameDelivery(gc.s)
    job = d.claim('123456', now=gc.clock)
    d.done(job, {'message_id': 42}, now=gc.clock)
    gc.op('u0', 'ready', t['id'], ready=True)
    gc.op('u1', 'auto', t['id'], enabled=True)
    assert d.claim('123456', now=gc.clock) is None


def test_removed_broadcast_group_cannot_receive_pending_card(gc):
    gc.cfg.update(broadcast_enabled=True, broadcast_groups='-123456')
    create(gc, 1)
    gc.cfg['broadcast_groups'] = ''
    asyncio.run(GameDelivery(gc.s).deliver())
    assert not any(method == 'sendMessage' for method, _ in gc.bot.calls)


def test_wallet_held_query_uses_live_user_index(gc):
    start(gc)
    statements = []
    gc.db._conn.set_trace_callback(statements.append)
    try:
        assert gc.s.wallet('u0', gc.clock)['frozen'] == 49
    finally:
        gc.db._conn.set_trace_callback(None)
    sql = next(s for s in statements if 'SUM(amount)' in s and 'play_escrows' in s)
    plan = gc.db.query('EXPLAIN QUERY PLAN ' + sql)
    assert any('gc_escrows_held_user' in row['detail'] for row in plan)


@pytest.mark.parametrize('high,low', [
    ([12, 25, 38], [11, 24, 37]),
    ([10, 11, 12], [13, 14, 25]),
    ([12, 9, 5], [25, 21, 20]),
    ([0, 14, 28], [12, 13, 27]),
    ([12, 25, 0], [11, 24, 38]),
    ([0, 13, 12], [26, 39, 11]),
    ([12, 21, 4], [25, 20, 5]),
])
def test_same_category_compares_all_rank_tiebreakers(high, low):
    assert engine.compare(high, low) == 1
    assert engine.compare(low, high) == -1
    other_suits = [(c + 13) % 52 for c in high]
    assert engine.compare(high, other_suits) == 0


def test_235_is_mixed_only_and_multiplayer_showdown_is_order_independent():
    mixed, flush, trips, pair = [0, 14, 29], [0, 1, 3], [12, 25, 38], [2, 15, 6]
    assert engine.compare(flush, trips, True) == -1
    assert engine.compare(mixed, pair, True) == -1
    other_mixed = [13, 27, 42]
    for hands in ((mixed, trips, other_mixed), (trips, other_mixed, mixed)):
        s = pure(*hands, budget=1, allow_235=True)
        s = engine.apply_action(s, '0', 'all_in')
        expected = [str(i) for i, cards in enumerate(hands) if cards != trips]
        assert s['result']['winners'] == expected
        assert sorted(s['result']['awards'].values()) == [0, 1, 2]
        assert sum(s['result']['net'].values()) == 0


def test_compare_requires_seen_second_round_and_double_seen_call_cost():
    s = pure([12, 25, 38], [11, 24, 37], [0, 16, 33])
    s = engine.apply_action(s, '0', 'look')
    assert 'compare' not in engine.legal_actions(s, '0')
    assert engine.legal_actions(s, '0')['call']['amount'] == 2
    for actor in ('0', '1', '2'):
        s = engine.apply_action(s, actor, 'call')
    assert s['round'] == 2
    options = engine.legal_actions(s, '0')
    assert options['compare']['amount'] == 4
    with pytest.raises(engine.EngineError):
        engine.apply_action(s, '0', 'compare', target='0')
    s = engine.apply_action(s, '0', 'compare', target='1')
    assert s['players'][1]['folded'] and s['players'][0]['invested'] == 7
    assert s['current_player'] == '2'
    s = engine.force_fold(s, '2')
    assert s['reason'] == 'last_player' and s['result']['winners'] == ['0']
    assert s['showdown'] == []


@pytest.mark.parametrize('method,action', [('consume', 'call'), ('release', 'fold'), ('payout', 'fold')])
def test_action_and_settlement_cash_failure_roll_back_and_retry_once(gc, monkeypatch, method, action):
    t = start(gc)
    before = gc.db.one('SELECT state_json FROM gc_tables WHERE id=?', (t['id'],))['state_json']
    events = gc.db.one('SELECT COUNT(*) n FROM play_cash_events')['n']
    balances = gc.points.balances()
    original = getattr(gc.s.cash, method)
    def fail_after_movement(*args, **kwargs):
        original(*args, **kwargs)
        raise PlayError('isolated failure after cash write')
    monkeypatch.setattr(gc.s.cash, method, fail_after_movement)
    body = {'hand_no': 1, 'turn': 1, 'action': action, 'request_id': 'cash_retry_0001'}
    with pytest.raises(PlayError, match='isolated failure'):
        gc.op('u0', 'actions', t['id'], **body)
    assert gc.db.one('SELECT state_json FROM gc_tables WHERE id=?', (t['id'],))['state_json'] == before
    assert gc.db.one('SELECT COUNT(*) n FROM play_cash_events')['n'] == events
    assert gc.points.balances() == balances and total(gc) == 40000
    monkeypatch.setattr(gc.s.cash, method, original)
    t = gc.op('u0', 'actions', t['id'], **body)
    events = gc.db.one('SELECT COUNT(*) n FROM play_cash_events')['n']
    gc.op('u0', 'actions', t['id'], **body)
    assert gc.db.one('SELECT COUNT(*) n FROM play_cash_events')['n'] == events
    if t['phase'] == 'playing':
        act(gc, t, 'fold')
    assert total(gc) == 40000
    assert gc.db.one('SELECT SUM(amount) n FROM play_escrows')['n'] == 0


def test_manual_auto_looks_then_folds_and_takeover_resets_streak(gc, monkeypatch):
    fix_deck(monkeypatch, deck_for([0, 16, 33], [12, 25, 38], [11, 24, 37]))
    t = start(gc, 3)
    deadline = t['turn_deadline']
    t = gc.op('u0', 'auto', t['id'], enabled=True)
    gc.s.tick(now=1001.5)
    t = gc.s.snapshot(t['id'], 'u0', 1001.5)['table']
    assert t['hand']['players'][0]['seen'] and t['turn_deadline'] == deadline
    assert t['auto_at'] == 1003
    gc.s.tick(now=1003)
    t = gc.s.snapshot(t['id'], 'u0', 1003)['table']
    assert t['hand']['players'][0]['folded']
    assert t['hand']['players'][0]['invested'] == 1
    gc.clock = 1003
    t = act(gc, t, 'fold')
    assert t['seats'][0]['auto_hands'] == 1 and t['seats'][0]['ready']
    t = gc.op('u0', 'auto', t['id'], enabled=False)
    assert not t['seats'][0]['auto'] and t['seats'][0]['auto_hands'] == 0
    assert t['me']['notice'] == ''


def test_outbox_removed_group_still_allows_bound_card_cleanup(gc):
    gc.cfg.update(broadcast_enabled=True, broadcast_groups='-123456')
    t = create(gc, 1)
    d = GameDelivery(gc.s)
    asyncio.run(d.deliver())
    gc.cfg['broadcast_groups'] = ''
    gc.op('u0', 'leave', t['id'])
    asyncio.run(d.deliver())
    assert gc.bot.calls[-1] == ('deleteMessage', {'chat_id': '-123456', 'message_id': 42})
    assert gc.db.one('SELECT state FROM gc_outbox')['state'] == 'deleted'


def test_old_live_state_survives_additive_migration_and_keeps_money_deadline(gc):
    from app.modules.game_center_schema import migrate
    t = start(gc)
    raw = gc.db.one('SELECT state_json FROM gc_tables WHERE id=?', (t['id'],))['state_json']
    assert 'start_status' not in raw
    gc.db.execute('DROP INDEX gc_escrows_held_user')  # model the released schema in this test DB
    migrate(gc.db)
    migrate(gc.db)
    gc.s = GameCenter(gc.db, gc.members, gc.points, lambda: gc.cfg, lambda: True)
    recovered = gc.s.snapshot(t['id'], 'u0', gc.clock)['table']
    assert recovered['hand'] == t['hand'] and recovered['turn_deadline'] == t['turn_deadline']
    assert recovered['me']['wallet']['frozen'] == 49
    gc.clock = t['turn_deadline'] + 1500
    gc.s.tick(now=gc.clock)
    assert total(gc) == 40000
    assert gc.s.wallet('u0', gc.clock)['frozen'] == gc.s.wallet('u1', gc.clock)['frozen'] == 0


def test_table_state_keeps_only_current_and_previous_hand(gc):
    t = create(gc)
    sizes = []
    for _ in range(15):
        gc.op('u1', 'ready', t['id'], ready=True)
        t = gc.op('u0', 'start', t['id'])
        t = act(gc, t, 'fold')
        raw = gc.db.one('SELECT state_json FROM gc_tables WHERE id=?', (t['id'],))['state_json']
        state = json.loads(raw)
        assert state['hand'] is None and state['last_hand']['number'] == t['hand_no']
        sizes.append(len(raw))
    assert max(sizes) - min(sizes) < 100
    assert gc.db.one('SELECT COUNT(*) n FROM gc_hands')['n'] == 15
    assert total(gc) == 40000


@pytest.mark.parametrize('game', ['zjh', 'holdem'])
def test_waiting_offline_leave_refunds_transfers_host_and_closes_once(presence, game):
    gc = presence
    t = waiting_table(gc, game, 3)
    gc.clock = 1089.99
    assert gc.s.tick(gc.clock) == []
    gc.s.snapshot(t['id'], 'u1', gc.clock)
    gc.op('u2', 'auto', t['id'], enabled=False)
    gc.clock = 1090
    assert gc.s.tick(gc.clock) == [t['id']]
    t = gc.s.snapshot(t['id'], 'u0', gc.clock)['table']
    assert t['me']['seat'] is None and t['me']['wallet']['frozen'] == 0
    assert t['host'] == t['seats'][0]['player'] and t['seats'][0]['seat'] == 1
    assert len(t['seats']) == 2 and t['phase'] == 'waiting'
    assert gc.points.balance('u0') == 5000 and total(gc) == 40000
    gc.clock = 1180
    gc.s.tick(gc.clock)
    t = gc.s.snapshot(t['id'], 'u0', gc.clock)['table']
    assert t['phase'] == 'closed' and t['host'] is None and t['seats'] == []
    assert gc.s.list_tiers(game)['tiers'][1]['waiting_tables'] == []
    assert not gc.db.query('SELECT * FROM gc_seats')
    assert gc.db.one('SELECT COALESCE(SUM(amount),0) n FROM play_escrows')['n'] == 0
    events = gc.db.one('SELECT COUNT(*) n FROM play_cash_events')['n']
    assert gc.s.tick(gc.clock) == []
    assert gc.db.one('SELECT COUNT(*) n FROM play_cash_events')['n'] == events
    assert gc.s._presence == {} and total(gc) == 40000


def test_offline_zjh_leave_recalculates_countdown_for_ready_survivors(presence):
    gc = presence
    t = create(gc, 3)
    for uid in ('u1', 'u2'):
        gc.op(uid, 'ready', t['id'], ready=True)
    gc.clock = 1089
    for uid in ('u1', 'u2'):
        gc.s.snapshot(t['id'], uid, gc.clock)
    gc.clock = 1090
    gc.s.tick(gc.clock)
    t = gc.s.snapshot(t['id'], 'u7', gc.clock)['table']
    assert t['phase'] == 'waiting' and t['countdown_at'] == 1095
    assert len(t['seats']) == 2 and all(p['ready'] for p in t['seats'])
    gc.clock = 1095
    gc.s.tick(gc.clock)
    t = gc.s.snapshot(t['id'], 'u7', gc.clock)['table']
    assert t['phase'] == 'playing' and len(t['hand']['players']) == 2
    assert not gc.s.active_tables('u0') and total(gc) == 40000


@pytest.mark.parametrize('game', ['zjh', 'holdem'])
def test_table_http_activity_precedes_offline_check_even_on_failed_or_retried_actions(presence, game):
    gc = presence
    t = waiting_table(gc, game, 1)
    gc.clock = 1090
    gc.s.snapshot(t['id'], 'u0', gc.clock)
    assert gc.s.tick(gc.clock) == []
    gc.clock = 1179
    gc.op('u0', 'auto', t['id'], enabled=False, request_id='presence_retry_001')
    gc.clock = 1268
    gc.op('u0', 'auto', t['id'], enabled=False, request_id='presence_retry_001')
    gc.clock = 1357
    with pytest.raises(GameError, match='至少需要2'):
        gc.op('u0', 'start', t['id'])
    assert gc.s.tick(gc.clock) == [] and gc.s.active_tables('u0')
    gc.clock = 1446.99
    assert gc.s.tick(gc.clock) == []
    gc.clock = 1447
    gc.s.tick(gc.clock)
    assert not gc.s.active_tables('u0') and total(gc) == 40000


@pytest.mark.parametrize('game', ['zjh', 'holdem'])
def test_lobby_wallet_other_table_and_spectator_do_not_keep_seat_online(presence, game):
    gc = presence
    t = waiting_table(gc, game, 1)
    other = gc.op('u1', 'create', game=game, tier='novice', **({'buy_in': 100} if game == 'holdem' else {}))
    gc.clock = 1089
    gc.s.lobby('u0', gc.clock)
    gc.s.wallet('u0', gc.clock)
    gc.s.snapshot(other['id'], 'u0', gc.clock)
    gc.s.snapshot(t['id'], 'u1', gc.clock)
    gc.clock = 1090
    gc.s.tick(gc.clock)
    assert not gc.s.active_tables('u0') and not gc.s.active_tables('u1')
    assert total(gc) == 40000


@pytest.mark.parametrize('game', ['zjh', 'holdem'])
def test_offline_rule_does_not_apply_during_playing(presence, game):
    gc = presence
    gc.cfg['step_seconds'] = 120
    t = waiting_table(gc, game)
    if game == 'zjh':
        gc.op('u1', 'ready', t['id'], ready=True)
    t = gc.op('u0', 'start', t['id'])
    deadline = t['turn_deadline']
    gc.clock = 1090
    assert gc.s.tick(gc.clock) == []
    t = gc.s.snapshot(t['id'], 'u7', gc.clock)['table']
    assert t['phase'] == 'playing' and len(t['seats']) == 2
    assert t['turn_deadline'] == deadline and t['hand']['turn'] == 1
    assert all(not p['leave_after'] for p in t['seats']) and total(gc) == 40000


def test_offline_rule_skips_between_but_applies_to_holdem_paused_table(presence):
    gc = presence
    gc.cfg['step_seconds'] = 120
    t = waiting_table(gc, 'holdem')
    t = gc.op('u0', 'start', t['id'])
    gc.clock = 1091
    t = gc.op('u0', 'actions', t['id'], hand_no=1, turn=1, action='fold')
    assert t['phase'] == 'between'
    gc.clock = 1092
    gc.s.tick(gc.clock)
    assert len(gc.s.snapshot(t['id'], 'u7', gc.clock)['table']['seats']) == 2
    t = gc.op('u0', 'away', t['id'], enabled=True)
    assert t['phase'] == 'waiting'
    gc.s.tick(gc.clock)
    t = gc.s.snapshot(t['id'], 'u1', gc.clock)['table']
    assert t['me']['seat'] is None and t['me']['wallet']['frozen'] == 0
    assert t['phase'] == 'waiting' and len(t['seats']) == 1 and total(gc) == 40000


@pytest.mark.parametrize('game', ['zjh', 'holdem'])
def test_restart_grants_existing_waiting_seats_full_offline_grace(presence, game):
    gc = presence
    t = waiting_table(gc, game, 1)
    raw = gc.db.one('SELECT state_json FROM gc_tables WHERE id=?', (t['id'],))['state_json']
    gc.clock = 1200
    gc.s = GameCenter(gc.db, gc.members, gc.points, lambda: gc.cfg, lambda: True, lambda: gc.bot)
    assert gc.s._presence == {} and gc.s._online == {}
    gc.clock = 1289.99
    assert gc.s.tick(gc.clock) == []
    assert gc.db.one('SELECT state_json FROM gc_tables WHERE id=?', (t['id'],))['state_json'] == raw
    assert gc.s.active_tables('u0')
    gc.clock = 1290
    gc.s.tick(gc.clock)
    assert not gc.s.active_tables('u0')
    assert gc.s.wallet('u0', gc.clock)['frozen'] == 0 and total(gc) == 40000


@pytest.mark.parametrize('game', ['zjh', 'holdem'])
def test_practice_owner_offline_closes_waiting_table_and_returns_beans(presence, game):
    gc = presence
    t = waiting_table(gc, game, 1, tier='practice')
    if game == 'holdem':
        t = gc.op('u0', 'away', t['id'], enabled=True)
        for _ in range(256):
            if t['phase'] == 'waiting':
                break
            gc.clock = t['auto_at'] or t['turn_deadline']
            gc.s.tick(gc.clock)
            t = gc.s.snapshot(t['id'], 'u0', gc.clock)['table']
    assert t['phase'] == 'waiting'
    before = t['me']['wallet']['beans']
    stack = next(p.get('stack', 0) for p in t['seats'] if not p['bot'])
    gc.clock += 90
    gc.s.tick(gc.clock)
    t = gc.s.snapshot(t['id'], 'u0', gc.clock)['table']
    assert t['phase'] == 'closed' and t['seats'] == [] and t['me']['seat'] is None
    assert t['me']['wallet']['beans'] == before + stack
    assert t['me']['wallet']['frozen'] == 0 and total(gc) == 40000


@pytest.mark.parametrize('game', ['zjh', 'holdem'])
def test_offline_tick_racing_duplicate_leave_refunds_only_once(presence, game):
    gc = presence
    t = waiting_table(gc, game, 1)
    gc.clock += 90
    def race(i):
        if i % 2:
            return gc.s.tick(gc.clock)
        try:
            return gc.s.mutate('u0', '昵称', 'leave', {'request_id': 'offline_leave_0001'}, t['id'], gc.clock)
        except GameError as exc:
            assert exc.code == 'forbidden'
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(race, range(16)))
    assert not gc.s.active_tables('u0') and gc.s.wallet('u0', gc.clock)['frozen'] == 0
    refunds = [r for r in gc.points.ledger('u0') if r['reason'] == 'game.cash_out']
    assert len(refunds) == (1 if game == 'holdem' else 0) and total(gc) == 40000


def test_offline_refund_failure_rolls_back_all_removals_and_can_retry(presence, monkeypatch):
    gc = presence
    t = waiting_table(gc, 'holdem')
    raw = gc.db.one('SELECT state_json FROM gc_tables WHERE id=?', (t['id'],))['state_json']
    original = gc.s.cash.release
    calls = []
    def fail_second(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(1)
        if len(calls) == 2:
            raise PlayError('offline refund failure')
        return result
    monkeypatch.setattr(gc.s.cash, 'release', fail_second)
    gc.clock += 90
    with pytest.raises(PlayError, match='offline refund failure'):
        gc.s.tick(gc.clock)
    assert gc.db.one('SELECT state_json FROM gc_tables WHERE id=?', (t['id'],))['state_json'] == raw
    assert gc.db.one('SELECT SUM(amount) n FROM play_escrows')['n'] == 200
    assert gc.points.balance('u0') == gc.points.balance('u1') == 4900 and total(gc) == 40000
    monkeypatch.setattr(gc.s.cash, 'release', original)
    gc.s.tick(gc.clock)
    assert gc.db.one('SELECT SUM(amount) n FROM play_escrows')['n'] == 0
    assert gc.points.balance('u0') == gc.points.balance('u1') == 5000 and total(gc) == 40000
