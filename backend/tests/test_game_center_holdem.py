"""Hold'em integration: real SQLite money, continuous play and durable deadlines."""
import asyncio
import hashlib
import json
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_game_center import gc, total  # noqa: F401 - shared real SQLite fixture
from test_game_center_api import api, headers  # noqa: F401 - shared authenticated HTTP fixture

from app.core.db import Database
from app.modules import holdem_engine as engine
from app.modules.game_center import GameCenter, GameError
from app.modules.game_center_api import stream
from app.modules.game_center_config import GameCenterPlugin, tiers
from app.modules.game_center_delivery import GameDelivery
from app.modules.game_center_holdem import robot_action
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.play_money import PlayError
from app.modules.points import PointsService


@pytest.fixture
def hc(request):
    env = request.getfixturevalue('gc')
    env.cfg['holdem_enabled'] = True
    return env


def create(e, n=2, buy=100, tier='novice'):
    amounts = buy if isinstance(buy, list) else [buy] * n
    t = e.op('u0', 'create', game='holdem', tier=tier, buy_in=amounts[0])
    for i in range(1, n):
        t = e.op(f'u{i}', 'seat', t['id'], buy_in=amounts[i])
    return t


def start(e, n=2, buy=100):
    t = create(e, n, buy)
    return e.op('u0', 'start', t['id']) if t['phase'] == 'waiting' else t


def raw(e, t):
    return json.loads(e.db.one('SELECT state_json FROM gc_tables WHERE id=?', (t['id'],))['state_json'])


def snap(e, t, uid='u0'):
    return e.s.snapshot(t['id'], uid, e.clock)['table']


def act(e, t, action, amount=None, **kwargs):
    r = raw(e, t)
    pid = engine.current_player(r['hand']['engine'])
    return e.op(r['hand']['users'][pid]['uid'], 'actions', t['id'], hand_no=r['hand_no'],
                turn=r['hand']['turn'], action=action, amount=amount, **kwargs)


def finish(e, t, policy='check'):
    for _ in range(200):
        if t['hand'] is None:
            return t
        state = raw(e, t)['hand']['engine']
        opts = engine.legal_actions(state, engine.current_player(state))
        t = act(e, t, 'fold' if policy == 'fold' else 'check' if opts['check'] else 'call')
    raise AssertionError('hand failed to terminate')


def next_hand(e, t):
    assert t['phase'] == 'between'
    e.clock = t['countdown_at']
    e.s.tick(e.clock)
    return snap(e, t)


def held(e, t):
    return {r['user_id']: r['amount'] for r in e.db.query("SELECT user_id,amount FROM play_escrows WHERE scope='gc_holdem' AND ref=?", (t['id'],))}


def test_full_flow_freeze_start_continuous_topup_away_cashout(hc):
    e = hc
    before = total(e)
    t = create(e)
    assert t['phase'] == 'waiting' and t['hand_no'] == 0
    assert held(e, t) == {'u0': 100, 'u1': 100}
    assert e.points.balance('u0') == 4900 and total(e) == before
    with pytest.raises(GameError, match='房主'):
        e.op('u1', 'start', t['id'])
    with pytest.raises(GameError, match='无需准备'):
        e.op('u0', 'ready', t['id'], ready=True)
    t = e.op('u0', 'start', t['id'])
    assert t['button'] == 0 and t['hand']['engine']['small_blind_seat'] == 0
    t = finish(e, t, 'fold')
    assert t['phase'] == 'between' and t['countdown_at'] == e.clock + 4
    assert held(e, t) == {'u0': 99, 'u1': 101}
    assert e.points.balance('u0') == 4900  # no per-hand spendable changes
    t = e.op('u0', 'top-up', t['id'], amount=50)
    assert held(e, t)['u0'] == 149 and e.points.balance('u0') == 4850
    t = e.op('u1', 'away', t['id'], enabled=True)
    assert t['phase'] == 'waiting' and t['countdown_at'] is None
    t = e.op('u1', 'away', t['id'], enabled=False)
    t = next_hand(e, t)
    assert t['hand_no'] == 2 and t['button'] == 1
    t = finish(e, t, 'fold')
    e.op('u0', 'leave', t['id'])
    t = e.op('u1', 'leave', t['id'])
    assert t['phase'] == 'closed' and sum(held(e, t).values()) == 0
    assert total(e) == before and sum(e.points.balances().values()) == before
    assert {r['reason'] for r in e.points.ledger('u0') if r['reason'] != 'isolated.fixture'} == {
        'game.buy_in', 'game.top_up', 'game.cash_out'}


@pytest.mark.parametrize('amount', [None, True, 0, 39, 201, -1, 2.1, '100'])
def test_bad_buy_in_is_atomic(hc, amount):
    before = total(hc)
    with pytest.raises(PlayError):
        create(hc, 1, amount)
    assert hc.db.one('SELECT COUNT(*) n FROM gc_tables')['n'] == 0
    assert hc.db.one('SELECT COUNT(*) n FROM play_escrows')['n'] == 0
    assert total(hc) == before


def test_buyin_tiers_supreme_no_upper_bound_and_insufficient(hc):
    values = tiers(hc.cfg, 'holdem')
    for tier in values[1:]:
        assert tier['buy_min'] == tier['big_blind'] * 20
        assert tier['buy_max'] == (None if tier['id'] == 'supreme' else tier['big_blind'] * 100)
    with pytest.raises(GameError) as exc:
        create(hc, 1, 5001, 'supreme')
    assert exc.value.code == 'insufficient_funds'
    hc.points.add('u0', 20000, 'isolated.fixture')
    t = create(hc, 1, 20001, 'supreme')
    assert held(hc, t)['u0'] == 20001
    hc.op('u0', 'leave', t['id'])
    assert hc.points.balance('u0') == 25000


def test_first_hand_conditions_full_six_and_host_succession(hc):
    t = create(hc, 1)
    with pytest.raises(GameError, match='至少需要2'):
        hc.op('u0', 'start', t['id'])
    hc.op('u1', 'seat', t['id'], buy_in=100)
    hc.op('u1', 'away', t['id'], enabled=True)
    with pytest.raises(GameError, match='至少需要2'):
        hc.op('u0', 'start', t['id'])
    hc.clock += 180
    hc.s.tick(hc.clock)
    t = snap(hc, t, 'u1')
    assert t['me']['is_host']
    hc.op('u1', 'away', t['id'], enabled=False)
    for i in range(2, 6):
        t = hc.op(f'u{i}', 'seat', t['id'], buy_in=100)
    assert t['phase'] == 'playing' and len(t['hand']['engine']['seats']) == 6
    assert t['button'] == 1
    with pytest.raises(GameError, match='已满'):
        hc.op('u6', 'seat', t['id'], buy_in=100)
    t = hc.op('u1', 'leave', t['id'])
    assert t['host'] == t['seats'][0]['player'] and t['me']['seat'] == 1


def test_new_join_in_play_waits_next_hand_and_does_not_change_deadline(hc):
    t = start(hc)
    deadline = t['turn_deadline']
    t = hc.op('u2', 'quick', game='holdem', tier='novice', buy_in=100)
    assert len(t['seats']) == 3 and len(t['hand']['engine']['seats']) == 2
    assert t['turn_deadline'] == deadline and t['hand']['legal_actions'] is None
    t = finish(hc, t, 'fold')
    t = next_hand(hc, t)
    assert len(t['hand']['engine']['seats']) == 3 and t['button'] == 1


@pytest.mark.parametrize('absent', [0, 1, 2])
@pytest.mark.parametrize('operation', ['leave', 'away'])
def test_transition_to_heads_up_advances_big_blind(hc, absent, operation):
    t = start(hc, 3)
    previous_big = t['hand']['engine']['big_blind_seat']
    t = finish(hc, t, 'fold')
    kwargs = {'enabled': True} if operation == 'away' else {}
    t = hc.op(f'u{absent}', operation, t['id'], **kwargs)
    eligible = [p['seat'] for p in t['seats'] if not p['away']]
    next_big = min(eligible, key=lambda seat: (seat - previous_big - 1) % 6)
    t = next_hand(hc, t)
    state = t['hand']['engine']
    assert state['big_blind_seat'] == next_big
    assert state['small_blind_seat'] == state['button'] != next_big
    button = next(p for p in state['seats'] if p['seat'] == state['button'])
    assert state['current_player'] == button['player']
    t = act(hc, t, 'call')
    t = act(hc, t, 'check')
    assert t['hand']['engine']['street'] == 'flop'
    assert t['hand']['current_player'] != button['player']
    assert total(hc) == 40000


def test_heads_up_sparse_seats_resumes_using_legacy_last_hand(hc):
    t = hc.op('u0', 'create', game='holdem', tier='novice', buy_in=100, seat=4)
    for uid, seat in [('u1', 0), ('u2', 2)]:
        hc.op(uid, 'seat', t['id'], buy_in=100, seat=seat)
    t = hc.op('u0', 'start', t['id'])
    assert t['hand']['engine']['big_blind_seat'] == 2
    t = finish(hc, t, 'fold')
    t = hc.op('u0', 'away', t['id'], enabled=True)
    before = raw(hc, t)
    assert 'start_status' not in before
    hc.s = GameCenter(hc.db, hc.members, hc.points, lambda: hc.cfg, lambda: True, lambda: hc.bot)
    assert raw(hc, t) == before
    t = next_hand(hc, t)
    assert t['button'] == 2 and t['hand']['engine']['big_blind_seat'] == 0


def test_waiting_status_distinguishes_first_start_pause_and_practice(hc):
    t = create(hc, 1)
    assert t['start_status'] == {
        'eligible_players': 1, 'min_players': 2, 'can_start': False, 'reason': 'waiting_for_players',
    }
    t = hc.op('u1', 'seat', t['id'], buy_in=100)
    assert t['start_status']['can_start']
    assert t['start_status']['reason'] == 'waiting_for_host'
    tier = next(tier for tier in hc.s.list_tiers('holdem')['tiers'] if tier['id'] == 'novice')
    assert tier['waiting_tables'][0]['start_status'] == t['start_status']
    t = hc.op('u0', 'start', t['id'])
    assert t['start_status']['reason'] is None and not t['start_status']['can_start']
    t = finish(hc, t, 'fold')
    assert t['start_status']['reason'] == 'next_hand'
    t = hc.op('u1', 'away', t['id'], enabled=True)
    assert t['start_status']['reason'] == 'paused_for_players'
    assert t['start_status']['eligible_players'] == 1
    tier = next(tier for tier in hc.s.list_tiers('holdem')['tiers'] if tier['id'] == 'novice')
    assert tier['waiting_tables'][0]['start_status'] == t['start_status']
    t = hc.op('u2', 'seat', t['id'], buy_in=100)
    assert t['start_status']['reason'] == 'next_hand' and t['countdown_at'] == hc.clock + 4
    for uid in ('u0', 'u1', 'u2'):
        t = hc.op(uid, 'leave', t['id'])
    assert t['start_status']['reason'] == 'closed' and not t['start_status']['can_start']
    t = create(hc, 1, 2000, 'practice')
    hc.op('u0', 'away', t['id'], enabled=True)
    for _ in range(200):
        if not t['hand']:
            break
        r = raw(hc, t)
        current = engine.current_player(r['hand']['engine'])
        if r['hand']['users'][current]['bot']:
            hc.clock = r['auto_at']
            hc.s.tick(hc.clock)
            t = snap(hc, t)
        else:
            t = act(hc, t, 'fold')
    assert t['start_status'] == {
        'eligible_players': 0, 'min_players': 2, 'can_start': False, 'reason': 'practice_player_unavailable',
    }


def test_topup_phase_limit_balance_concurrency_and_reentry(hc):
    t = create(hc)
    def topup():
        return hc.s.mutate('u0', '昵称', 'top-up', {'amount':50, 'request_id':'same_topup_001'}, t['id'], hc.clock)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: topup(), range(8)))
    assert held(hc, t)['u0'] == 150
    assert len([x for x in hc.points.ledger('u0') if x['reason'] == 'game.top_up']) == 1
    with pytest.raises(GameError, match='ID'):
        hc.op('u0', 'top-up', t['id'], amount=1, request_id='same_topup_001')
    with pytest.raises(GameError, match='最高'):
        hc.op('u0', 'top-up', t['id'], amount=51)
    hc.op('u0', 'leave', t['id'])
    assert held(hc, t)['u0'] == 0
    t = hc.op('u0', 'seat', t['id'], buy_in=70, request_id='reseat_same_001')
    hc.op('u0', 'seat', t['id'], buy_in=70, request_id='reseat_same_001')
    assert held(hc, t)['u0'] == 70 and hc.points.balance('u0') == 4930
    assert len([x for x in hc.points.ledger('u0') if x['reason'] == 'game.buy_in']) == 2
    assert hc.db.one("SELECT COUNT(*) n FROM play_escrows WHERE user_id='u0'")['n'] == 1
    t = hc.op('u1', 'start', t['id'])
    with pytest.raises(GameError, match='两手之间'):
        hc.op('u0', 'top-up', t['id'], amount=1)
    assert total(hc) == 40000


def test_topup_insufficient_balance_and_nonpositive(hc):
    hc.points.add('u0', -4950, 'isolated.fixture')
    t = create(hc, buy=40)
    for amount in (0, -1, True, 1.2):
        with pytest.raises(PlayError):
            hc.op('u0', 'top-up', t['id'], amount=amount)
    with pytest.raises(GameError) as exc:
        hc.op('u0', 'top-up', t['id'], amount=11)
    assert exc.value.code == 'insufficient_funds' and held(hc, t)['u0'] == 40


def test_departing_participant_defers_release_including_already_folded(hc):
    t = start(hc, 3)
    t = act(hc, t, 'fold')  # seat 0 folds but is still a participant awaiting results
    t = hc.op('u0', 'leave', t['id'])
    assert t['me']['seat'] == 0 and t['seats'][0]['leave_after']
    assert '自动弃牌' not in t['me']['notice']
    assert held(hc, t)['u0'] == 100 and hc.points.balance('u0') == 4900
    t = finish(hc, t, 'fold')
    assert all(p['seat'] != 0 for p in t['seats']) and held(hc, t)['u0'] == 0
    assert hc.points.balance('u0') == 5000 and t['button'] == 0
    t = next_hand(hc, t)
    assert t['button'] == 2  # Previous big blind becomes the heads-up button, not big blind again.


def test_departing_turn_folds_on_auto_tick_and_spectator_leaves_now(hc):
    t = start(hc)
    hc.op('u2', 'seat', t['id'], buy_in=100)
    hc.op('u2', 'leave', t['id'])
    assert hc.points.balance('u2') == 5000
    t = hc.op('u0', 'leave', t['id'])
    assert t['auto_at'] == hc.clock + 1.5 and held(hc, t)['u0'] == 100
    with pytest.raises(GameError, match='已申请离桌'):
        hc.op('u0', 'auto', t['id'], enabled=False)
    hc.clock += 1.5
    hc.s.tick(hc.clock)
    t = snap(hc, t)
    assert t['me']['seat'] is None and hc.points.balance('u0') == 4999
    assert held(hc, t)['u1'] == 101


def test_all_in_departure_still_eligible_to_win(hc, monkeypatch):
    # 0=AA, 1=KK, 2=QQ. Deal left of button twice; low nonflush board.
    head = [11, 10, 12, 24, 23, 25, 51, 0, 14, 28, 50, 5, 49, 19]
    class Fixed:
        def shuffle(self, values):
            values[:] = head + [c for c in range(52) if c not in head]
    monkeypatch.setattr('app.modules.game_center_holdem.secrets.SystemRandom', Fixed)
    t = start(hc, 3, buy=[40, 80, 120])
    t = act(hc, t, 'all_in')
    t = hc.op('u0', 'leave', t['id'])
    assert '仍参与本手结算' in t['me']['notice'] and '自动弃牌' not in t['me']['notice']
    assert held(hc, t)['u0'] == 40
    t = act(hc, t, 'all_in')
    t = act(hc, t, 'call')  # no opponent can contest another raise
    assert t['last_hand']['result']['players'][t['last_hand']['engine']['seats'][0]['player']]['stack'] == 120
    assert hc.points.balance('u0') == 5080 and held(hc, t)['u0'] == 0
    assert sum(p['amount'] for p in t['last_hand']['result']['pots']) == 200
    assert total(hc) == 40000


def test_away_current_hand_unchanged_no_blinds_next_and_auto_leave(hc):
    hc.cfg['holdem_away_seconds'] = 20
    t = start(hc, 3)
    before = t['hand']
    t = hc.op('u0', 'away', t['id'], enabled=True)
    assert t['seats'][0]['away'] and t['seats'][0]['away_since'] is None
    assert t['hand'] == before  # no forced fold or blind refund mid-hand
    t = finish(hc, t, 'fold')
    away_since = t['seats'][0]['away_since']
    old = held(hc, t)['u0']
    t = next_hand(hc, t)
    assert all(p['seat'] != 0 for p in t['hand']['engine']['seats'])
    t = finish(hc, t, 'fold')
    assert held(hc, t)['u0'] == old and t['seats'][0]['away_since'] == away_since
    hc.clock = away_since + 20
    hc.s.tick(hc.clock)
    assert snap(hc, t)['me']['seat'] is None and held(hc, t)['u0'] == 0


def test_timeout_engine_policy_cancel_keeps_original_deadline(hc):
    t = start(hc, 3)
    state = raw(hc, t)['hand']['engine']
    pid = engine.current_player(state)
    action, amount = engine.auto_action(state, pid)
    expected = engine.apply_action(state, pid, action, amount)
    hc.clock = t['turn_deadline']
    hc.s.tick(hc.clock)
    t = snap(hc, t)
    assert raw(hc, t)['hand']['engine'] == expected
    assert t['seats'][0]['auto'] and t['seats'][0]['auto_since'] == hc.clock
    assert '已超时' in t['me']['notice']
    current = engine.current_player(expected)
    uid = raw(hc, t)['hand']['users'][current]['uid']
    deadline = t['turn_deadline']
    t = hc.op(uid, 'auto', t['id'], enabled=True)
    assert t['auto_at'] == hc.clock + 1.5
    t = hc.op(uid, 'auto', t['id'], enabled=False)
    assert t['auto_at'] is None and t['turn_deadline'] == deadline


def test_auto_hand_threshold_away_timestamp_does_not_slide(hc):
    hc.cfg['holdem_auto_hands'] = 1
    hc.cfg['holdem_away_seconds'] = 90
    t = start(hc, 3)
    hc.op('u0', 'auto', t['id'], enabled=True)
    t = finish(hc, t, 'fold')
    assert t['seats'][0]['auto_hands'] == 1 and t['seats'][0]['away']
    away_since = t['seats'][0]['away_since']
    for _ in range(3):
        t = next_hand(hc, t)
        t = finish(hc, t, 'fold')
        assert t['seats'][0]['away_since'] == away_since
    t = hc.op('u0', 'away', t['id'], enabled=False)
    assert t['seats'][0]['auto_hands'] == 0 and t['seats'][0]['auto']
    assert t['seats'][0]['auto_since'] == hc.clock


def test_auto_time_threshold_mid_hand_waits_until_boundary(hc):
    hc.cfg['holdem_auto_seconds'] = 1
    hc.cfg['holdem_away_seconds'] = 10
    t = start(hc)
    t = hc.op('u1', 'auto', t['id'], enabled=True)
    hc.clock += 1
    hc.s.tick(hc.clock)
    t = snap(hc, t)
    assert t['seats'][1]['away'] and t['seats'][1]['away_since'] is None
    assert t['hand']['turn'] == 1 and len(t['hand']['engine']['seats']) == 2
    t = finish(hc, t, 'fold')
    assert t['seats'][1]['away_since'] == hc.clock and t['phase'] == 'waiting'
    hc.clock += 10
    hc.s.tick(hc.clock)
    assert held(hc, t)['u1'] == 0 and hc.points.balance('u1') == 5001


def test_idle_scatter_unfreezes_every_seat(hc):
    before = total(hc)
    t = create(hc, 3)
    hc.clock += 600
    hc.s.tick(hc.clock)
    assert snap(hc, t)['phase'] == 'closed' and not hc.s.active_tables('u0')
    assert held(hc, t) == {'u0':0, 'u1':0, 'u2':0}
    assert total(hc) == before and hc.points.balance('u0') == 5000


def test_restart_actual_db_connection_mid_hand_preserves_deck_deadlines_and_settlement(hc):
    t = start(hc, 3)
    t = act(hc, t, 'call')
    before = raw(hc, t)
    path = hc.db._conn.execute('PRAGMA database_list').fetchone()[2]
    hc.db.close()
    hc.db = Database(path)
    groups = GroupService(hc.db)
    hc.members, hc.points = MemberService(hc.db, groups), PointsService(hc.db)
    hc.s = GameCenter(hc.db, hc.members, hc.points, lambda: hc.cfg, lambda: True, lambda: hc.bot)
    assert raw(hc, t) == before
    hc.clock = t['turn_deadline'] - .01
    hc.s.tick(hc.clock)
    assert raw(hc, t)['hand'] == before['hand']
    hc.clock += .01
    hc.s.tick(hc.clock)
    t = snap(hc, t)
    assert t['hand_no'] == 1
    current = raw(hc, t)['hand'] or raw(hc, t)['last_hand']
    assert current['deck'] == before['hand']['deck']
    t = finish(hc, t, 'fold')
    balances = held(hc, t)
    count = hc.db.one('SELECT COUNT(*) n FROM play_cash_events')['n']
    hc.s = GameCenter(hc.db, hc.members, hc.points, lambda: hc.cfg, lambda: True, lambda: hc.bot)
    hc.s.tick(hc.clock)
    assert held(hc, t) == balances and hc.db.one('SELECT COUNT(*) n FROM play_cash_events')['n'] == count
    assert snap(hc, t)['countdown_at'] == t['countdown_at']
    t = next_hand(hc, t)
    assert t['hand_no'] == 2 and total(hc) == 40000
    hc.db.close()


def test_restart_catches_up_auto_away_and_releases_all_frozen_points(hc):
    t = start(hc, 6)
    before = raw(hc, t)
    hc.s = GameCenter(hc.db, hc.members, hc.points, lambda: hc.cfg, lambda: True, lambda: hc.bot)
    assert raw(hc, t) == before
    hc.clock += 1800
    for _ in range(10):
        hc.s.tick(hc.clock)
        if snap(hc, t)['phase'] == 'closed':
            break
    assert snap(hc, t)['phase'] == 'closed'
    assert not hc.db.query('SELECT * FROM gc_seats')
    assert all(amount == 0 for amount in held(hc, t).values())
    assert total(hc) == 40000
    count = hc.db.one('SELECT COUNT(*) n FROM play_cash_events')['n']
    hc.s.tick(hc.clock)
    assert hc.db.one('SELECT COUNT(*) n FROM play_cash_events')['n'] == count


def test_many_hands_keep_live_state_bounded_and_preserve_archives(hc):
    t = start(hc)
    first_size = None
    for hand in range(100):
        t = finish(hc, t, 'fold')
        row = hc.db.one('SELECT state_json FROM gc_tables WHERE id=?', (t['id'],))
        size = len(row['state_json'])
        if first_size is None:
            first_size = size
        assert size < first_size + 512
        if hand < 99:
            t = next_hand(hc, t)
    assert hc.db.one('SELECT COUNT(*) n FROM gc_hands WHERE table_id=?', (t['id'],))['n'] == 100
    assert hc.s.proof(t['id'], 1)['settled']
    assert total(hc) == 40000


@pytest.mark.parametrize('seed', range(6))
def test_random_multihand_actual_escrows_zero_sum_and_final_cashout(hc, seed):
    rng = random.Random(seed)
    initial = total(hc)
    t = start(hc, 6, [40, 60, 80, 100, 150, 200])
    for hand in range(30):
        previous = held(hc, t)
        ledger = hc.db.one('SELECT COUNT(*) n FROM points_ledger')['n']
        for _ in range(200):
            if not t['hand']:
                break
            state = raw(hc, t)['hand']['engine']
            opts = engine.legal_actions(state, engine.current_player(state))
            choices = [('fold', None), ('check', None) if opts['check'] else ('call', None)]
            if opts['all_in']:
                choices.append(('all_in', None))
            if opts['min_raise_to'] is not None:
                choices.append(('bet' if state['current_bet'] == 0 else 'raise', rng.randint(opts['min_raise_to'], opts['max_raise_to'])))
            t = act(hc, t, *rng.choice(choices))
        assert not t['hand']
        after = held(hc, t)
        assert sum(after[u] - previous[u] for u in previous) == 0
        assert total(hc) == initial and all(n >= 0 for n in after.values())
        assert hc.db.one('SELECT COUNT(*) n FROM points_ledger')['n'] == ledger
        for p in raw(hc, t)['seats']:
            assert p['stack'] == after[p['uid']]
            if p['stack'] < 40:
                t = hc.op(p['uid'], 'top-up', t['id'], amount=100 - p['stack'])
        assert not hc.db.one('SELECT 1 FROM play_escrows WHERE amount+spent+released<>reserved')
        if hand < 29:
            t = next_hand(hc, t)
    for p in raw(hc, t)['seats'][:]:
        t = hc.op(p['uid'], 'leave', t['id'])
    assert t['phase'] == 'closed' and sum(held(hc, t).values()) == 0
    assert sum(hc.points.balances().values()) == initial


def test_multiway_sidepots_exact_escrow_transfers_and_uncalled_return(hc, monkeypatch):
    head = [11, 10, 12, 24, 23, 25, 51, 0, 14, 28, 50, 5, 49, 19]
    class Fixed:
        def shuffle(self, values):
            values[:] = head + [c for c in range(52) if c not in head]
    monkeypatch.setattr('app.modules.game_center_holdem.secrets.SystemRandom', Fixed)
    t = start(hc, 3, buy=[40, 80, 120])
    ledger = hc.db.one('SELECT COUNT(*) n FROM points_ledger')['n']
    t = act(hc, t, 'call')
    for _ in range(3):
        t = act(hc, t, 'all_in')
    assert held(hc, t) == {'u0':120, 'u1':80, 'u2':40}
    result = t['last_hand']['result']
    assert [p['amount'] for p in result['pots']] == [120, 80]
    assert sorted(result['uncalled'].values()) == [40]
    assert hc.db.one('SELECT COUNT(*) n FROM points_ledger')['n'] == ledger
    events = hc.s.cash_detail(t['id'])['events']
    assert sum(r['amount'] for r in events if r['operation'] == 'transfer_out') == 80
    assert sum(r['amount'] for r in events if r['operation'] == 'transfer_in') == 80
    assert total(hc) == 40000


def test_settlement_failure_rolls_back_action_engine_cash_and_idempotency(hc, monkeypatch):
    t = start(hc)
    before = raw(hc, t)
    transfer = hc.s.cash.transfer
    def fail(*a, **k):
        transfer(*a, **k)
        raise PlayError('injected settlement failure')
    monkeypatch.setattr(hc.s.cash, 'transfer', fail)
    with pytest.raises(PlayError, match='injected'):
        act(hc, t, 'fold', request_id='settlement_retry_001')
    assert raw(hc, t) == before and held(hc, t) == {'u0':100, 'u1':100}
    monkeypatch.setattr(hc.s.cash, 'transfer', transfer)
    t = act(hc, t, 'fold', request_id='settlement_retry_001')
    assert held(hc, t) == {'u0':99, 'u1':101}


def test_deck_commitment_deal_order_and_all_private_views(hc):
    t = start(hc, 3)
    r = raw(hc, t)
    h = r['hand']
    verify = hc.s.proof(t['id'], 1)
    assert not verify['settled'] and 'salt' not in verify and 'deck' not in verify
    assert hashlib.sha256((h['deck_codes'] + '|' + h['salt']).encode()).hexdigest() == verify['commitment']
    for uid in ('u0', 'u1', 'u2', 'u7'):
        v = snap(hc, t, uid)
        player = v['me']['player']
        for p in v['hand']['engine']['seats']:
            assert ('hole' in p) == (p['player'] == player)
        wire = json.dumps(v)
        assert all(field not in wire for field in ('"deck"', '"salt"', '"burned"', '"cursor"', '"users"', 'private-login'))
    t = finish(hc, t)
    verify = hc.s.proof(t['id'], 1)
    assert verify['settled'] and len(verify['deck']) == 52
    n = len(verify['deal_order'])
    for index, pid in enumerate(verify['deal_order']):
        p = next(p for p in t['last_hand']['engine']['seats'] if p['player'] == pid)
        assert p['hole'] == [verify['deck'][index], verify['deck'][index+n]]
    assert t['last_hand']['engine']['board'] == [verify['deck'][i] for i in (2*n+1, 2*n+2, 2*n+3, 2*n+5, 2*n+7)]


def test_http_sse_reconnect_uses_latest_view_without_folded_holes(hc, request, monkeypatch):
    client, _ = request.getfixturevalue('api')
    monkeypatch.setattr('app.modules.game_center.time.time', lambda: hc.clock)
    t = start(hc, 3)
    path = '/api/app/games/tables/' + t['id']
    async def connected():
        return False
    async def snapshot(uid):
        events = stream(hc.s, t['id'], uid, lambda: True, hc.clock + 30, connected)
        try:
            return json.loads((await anext(events)).split('data: ')[1])['table']
        finally:
            await events.aclose()
    for uid, telegram in [('u0', 9000), ('u1', 9001), ('u7', 9007)]:
        public = client.get(path, headers=headers(telegram)).json()['table']
        assert asyncio.run(snapshot(uid)) == public
        assert all(('hole' in p) == (p['player'] == public['me']['player'])
                   for p in public['hand']['engine']['seats'])
    folded = t['seats'][0]['player']
    t = act(hc, t, 'fold')
    t = finish(hc, t)
    for uid, telegram in [('u1', 9001), ('u7', 9007)]:
        public = client.get(path, headers=headers(telegram)).json()['table']
        restored = asyncio.run(snapshot(uid))
        assert restored == public and restored['revision'] == t['revision']
        assert restored['start_status']['reason'] == 'next_hand'
        hidden = next(p for p in restored['last_hand']['engine']['seats'] if p['player'] == folded)
        assert 'hole' not in hidden and not hidden['shown']
    assert not hc.s._listeners


def test_folded_cards_never_in_others_snapshot_even_when_member_reseats(hc):
    t = start(hc)
    pid = t['seats'][0]['player']
    t = finish(hc, t, 'fold')
    assert 'hole' in snap(hc, t)['last_hand']['engine']['seats'][0]
    assert 'hole' not in snap(hc, t, 'u1')['last_hand']['engine']['seats'][0]
    hc.op('u0', 'leave', t['id'])
    t = hc.op('u0', 'seat', t['id'], buy_in=100)
    assert t['me']['player'] != pid
    assert 'hole' in t['last_hand']['engine']['seats'][0]
    assert 'hole' not in snap(hc, t, 'u7')['last_hand']['engine']['seats'][0]


@pytest.mark.parametrize('seed', range(10))
def test_robot_legal_decisions_all_streets_and_only_own_cards(seed):
    rng = random.Random(seed)
    actions = set()
    for hand in range(20):
        deck = list(range(52))
        rng.shuffle(deck)
        s = engine.new_hand(seats=[{'seat':i,'player':str(i),'stack':rng.randint(1,2000)} for i in range(6)],
                            button=hand % 6, small_blind=10, big_blind=20, deck=deck)
        for _ in range(200):
            if engine.is_finished(s):
                break
            who = engine.current_player(s)
            action, amount = robot_action(s, who, rng)
            actions.add(action)
            s = engine.apply_action(s, who, action, amount)
        assert engine.is_finished(s)
        assert sum(p['net'] for p in engine.results(s)['players'].values()) == 0
    assert {'check', 'call', 'fold'} <= actions


def test_practice_beans_only_bot_ticks_and_midhand_leave(hc):
    t = create(hc, 1, 2000, 'practice')
    assert t['phase'] == 'playing' and len(t['seats']) == 6
    assert t['me']['wallet']['beans'] == 8000 and all(p['bot'] for p in t['seats'][1:])
    with pytest.raises(GameError, match='不存在'):
        hc.s.snapshot(t['id'], 'u1', hc.clock)
    t = hc.op('u0', 'leave', t['id'])
    for _ in range(100):
        hc.clock += 2
        hc.s.tick(hc.clock)
        t = snap(hc, t)
        if t['phase'] == 'closed':
            break
    assert t['phase'] == 'closed'
    assert 8000 <= t['me']['wallet']['beans'] <= 20000
    assert hc.db.one('SELECT COUNT(*) n FROM play_cash_events')['n'] == 0
    assert hc.db.one('SELECT COUNT(*) n FROM play_escrows')['n'] == 0
    assert total(hc) == 40000


def test_holdem_broadcast_blinds_update_delete_and_statistics(hc):
    hc.cfg.update(broadcast_enabled=True, broadcast_groups='-123456')
    t = create(hc, 1)
    d = GameDelivery(hc.s)
    asyncio.run(d.deliver())
    card = next(payload for method, payload in hc.bot.calls if method == 'sendMessage')
    assert '德州扑克' in card['text'] and '盲注 1/2' in card['text'] and '1/6' in card['text']
    assert '余额' not in card['text'] and 'MediaDeck' not in card['text']
    hc.op('u1', 'seat', t['id'], buy_in=100)
    asyncio.run(d.deliver())
    assert any(method == 'editMessageText' for method, _ in hc.bot.calls)
    t = hc.op('u0', 'start', t['id'])
    asyncio.run(d.deliver())
    assert hc.bot.calls[-1][0] == 'deleteMessage'
    t = finish(hc, t, 'fold')
    plugin = GameCenterPlugin(SimpleNamespace(db=hc.db))
    # Move only the test timestamps into today's statistical window.
    import time
    hc.db.execute('UPDATE gc_hands SET settled_at=?', (time.time(),))
    hc.db.execute('UPDATE play_cash_events SET created_at=?', (time.time(),))
    status = plugin.readonly_status()
    assert status['在桌人数'] == 2 and status['今日局数'] == 1
    assert status['今日积分投入流水'] == 2 and status['今日德州冻结转移'] == 1


def test_zero_stack_skipped_pauses_and_topup_resumes_even_short_blind_auto_finish(hc, monkeypatch):
    head = [11, 12, 24, 25, 51, 0, 14, 28, 50, 5, 49, 19]
    class Fixed:
        def shuffle(self, values):
            values[:] = head + [c for c in range(52) if c not in head]
    monkeypatch.setattr('app.modules.game_center_holdem.secrets.SystemRandom', Fixed)
    t = start(hc, 2, 40)
    t = act(hc, t, 'all_in')
    t = act(hc, t, 'call')
    assert t['seats'][1]['stack'] == 0 and t['phase'] == 'waiting'
    assert '补码或离桌' in snap(hc, t, 'u1')['me']['notice']
    before = raw(hc, t)
    hc.clock += 10
    hc.s.tick(hc.clock)
    assert raw(hc, t) == before  # no new hand, no blinds from the busted seat
    t = hc.op('u1', 'top-up', t['id'], amount=1)
    assert t['phase'] == 'between'
    t = next_hand(hc, t)
    # At button=1 the one-chip small blind is all-in; no real decision remains.
    assert t['hand_no'] == 2 and t['hand'] is None and t['last_hand']['finished']
    assert t['last_hand']['turn'] == 1
    assert sum(held(hc, t).values()) == 81 and total(hc) == 40000


def test_practice_away_pauses_entire_table_and_returns_beans_after_timeout(hc):
    hc.cfg['holdem_away_seconds'] = 10
    t = create(hc, 1, 2000, 'practice')
    hc.op('u0', 'away', t['id'], enabled=True)
    # Drive each permitted action directly; timers use the same engine path.
    for _ in range(200):
        r = raw(hc, t)
        if r['hand'] is None:
            break
        pid = engine.current_player(r['hand']['engine'])
        if r['hand']['users'][pid]['bot']:
            hc.clock = r['auto_at']
            hc.s.tick(hc.clock)
            t = snap(hc, t)
        else:
            t = act(hc, t, 'fold')
    assert t['phase'] == 'waiting' and t['countdown_at'] is None
    since = t['seats'][0]['away_since']
    hc.clock = since + 9
    hc.s.tick(hc.clock)
    assert snap(hc, t)['hand_no'] == 1
    hc.clock += 1
    hc.s.tick(hc.clock)
    assert snap(hc, t)['phase'] == 'closed' and hc.db.one('SELECT COUNT(*) n FROM play_cash_events')['n'] == 0


def test_multiple_windows_competing_turn_executes_once(hc):
    t = start(hc)
    def action(key):
        try:
            return hc.s.mutate('u0', '昵称', 'actions', {'request_id':key,'hand_no':1,'turn':1,'action':'call'}, t['id'], hc.clock)
        except GameError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(action, ['window_one_001', 'window_two_001']))
    assert len([r for r in results if isinstance(r, dict)]) == 1
    assert 'stale_turn' in results
    assert raw(hc, t)['hand']['turn'] == 2
    assert held(hc, t) == {'u0':100, 'u1':100}


def test_off_switch_still_allows_holdem_completion_and_cashout(hc):
    t = start(hc)
    hc.s.enabled = lambda: False
    with pytest.raises(GameError) as exc:
        hc.op('u2', 'seat', t['id'], buy_in=100)
    assert exc.value.code == 'disabled'
    t = finish(hc, t, 'fold')
    hc.op('u0', 'leave', t['id'])
    hc.op('u1', 'leave', t['id'])
    assert sum(held(hc, t).values()) == 0 and total(hc) == 40000


def test_robot_decision_does_not_read_hidden_cards_or_deck():
    from copy import deepcopy
    s = engine.new_hand(seats=[{'seat':i,'player':str(i),'stack':100} for i in range(3)],
                        button=0, small_blind=1, big_blind=2, deck=list(range(52)))
    changed = deepcopy(s)
    changed['deck'].reverse()
    for p in changed['seats']:
        if p['player'] != engine.current_player(s):
            p['hole'] = [51, 50]
    assert robot_action(s, '0', random.Random(9)) == robot_action(changed, '0', random.Random(9))


def test_holdem_config_frozen_per_table_and_practice_buy_fixed(hc):
    cfg = GameCenterPlugin(None).normalize_config(hc.cfg)
    new_tiers = json.loads(cfg['holdem_tiers'])
    new_tiers[1].update(small_blind=2, big_blind=4, buy_min=80, buy_max=400)
    hc.cfg['holdem_tiers'] = json.dumps(new_tiers)
    t = start(hc, buy=100)
    assert t['rules']['big_blind'] == 4
    hc.cfg['step_seconds'] = 5
    hc.cfg['holdem_tiers'] = cfg['holdem_tiers']
    assert snap(hc, t)['rules']['big_blind'] == 4 and snap(hc, t)['rules']['step_seconds'] == 30
    new_tiers[0]['buy_min'] = 100
    with pytest.raises(ValueError):
        GameCenterPlugin(None).normalize_config({**hc.cfg, 'holdem_tiers':json.dumps(new_tiers)})


def test_member_facing_game_modules_do_not_brand_the_admin_panel():
    root = Path(__file__).parents[1] / 'app/modules'
    for name in ['game_center.py', 'game_center_holdem.py', 'game_center_zjh.py', 'zjh_engine.py', 'holdem_engine.py']:
        assert 'MediaDeck' not in (root / name).read_text()
