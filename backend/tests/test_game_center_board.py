"""Board adapters on the real SQLite/economy/HTTP-independent table shell."""
import asyncio
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_game_center import gc as game_env  # noqa: F401
from test_game_center import total
from test_gomoku_engine import full_draw
from test_xiangqi_engine import CHECK_CYCLE, LONG_CHECK, quiet_history

from app.modules import xiangqi_engine
from app.modules.game_center import GameCenter, GameError
from app.modules.game_center_config import DEFAULTS, GameCenterPlugin, tiers
from app.modules.points import reason_label


@pytest.fixture
def gc(request):
    return request.getfixturevalue('game_env')


@pytest.fixture
def board(gc, monkeypatch):
    gc.cfg.update(gomoku_enabled=True, xiangqi_enabled=True)
    monkeypatch.setattr('app.modules.game_center.monotonic', lambda: gc.clock)
    monkeypatch.setattr('app.modules.game_center_board.secrets.randbelow', lambda _: 0)
    monkeypatch.delenv('MEDIADECK_RAPFI_BIN', raising=False)
    monkeypatch.delenv('MEDIADECK_PIKAFISH_BIN', raising=False)
    gc.s._started_at = gc.clock
    yield gc
    gc.s.board_robots.pool.shutdown(wait=True)


def start(e, game='gomoku', tier='novice', **kw):
    t = e.op('u0', 'create', game=game, tier=tier, **kw)
    if tier != 'practice':
        t = e.op('u1', 'seat', t['id'])
        t = e.op('u1', 'ready', t['id'], ready=True)
    return e.op('u0', 'start', t['id'])


def act(e, t, action='move', uid=None, **kw):
    current = t['hand']['current_player']
    uid = uid or ('u0' if t['seats'][0]['player'] == current else 'u1')
    return e.op(uid, 'actions', t['id'], hand_no=t['hand_no'], turn=t['hand']['turn'], action=action, **kw)


def refresh(e, t, uid='u0'):
    return e.s.snapshot(t['id'], uid, now=e.clock)['table']


def advance(e, t, seconds):
    e.clock += seconds
    e.s.tick(now=e.clock, table_id=t['id'])
    return refresh(e, t)


def raw(e, t):
    return json.loads(e.db.one('SELECT state_json FROM gc_tables WHERE id=?', (t['id'],))['state_json'])


def test_config_gates_tiers_and_frozen_rules(board):
    e = board
    assert DEFAULTS['gomoku_enabled'] is DEFAULTS['xiangqi_enabled'] is False
    for game in ('gomoku', 'xiangqi'):
        rows = tiers(e.cfg, game)
        assert [r['stake'] for r in rows] == [100, 10, 50, 200, 500, 2000]
    plugin = GameCenterPlugin(None)
    cfg = copy.deepcopy(e.cfg)
    rows = json.loads(cfg['gomoku_tiers'])
    rows[0]['stake'] = 101
    cfg['gomoku_tiers'] = json.dumps(rows)
    with pytest.raises(ValueError):
        plugin.normalize_config(cfg)
    with pytest.raises(ValueError):
        plugin.normalize_config({**e.cfg, 'gomoku_total_seconds': True})
    e.cfg['gomoku_enabled'] = False
    with pytest.raises(GameError) as exc:
        e.op('u0', 'create', game='gomoku', tier='novice')
    assert exc.value.code == 'disabled'
    game = next(g for g in e.s.lobby('u0')['games'] if g['id'] == 'gomoku')
    assert game['available'] and not game['enabled'] and game['max_players'] == 2
    e.cfg['gomoku_enabled'] = True
    t = start(e)
    e.cfg.update(gomoku_total_seconds=99, gomoku_enabled=False)
    assert t['rules']['total_seconds'] == 300
    assert t['rules']['auto_play'] is False
    assert all(not s['auto'] for s in t['seats'])
    t = act(e, t, 'resign')  # Closing the plugin does not strand existing stakes.
    assert t['last_hand']['result']['reason'] == 'resign'


def test_start_ready_countdown_and_swapped_colors(board):
    e = board
    t = e.op('u0', 'create', game='gomoku', tier='novice')
    t = e.op('u1', 'seat', t['id'])
    assert not t['start_status']['can_start']
    with pytest.raises(GameError) as exc:
        e.op('u0', 'start', t['id'])
    assert exc.value.code == 'not_ready'
    assert e.s.wallet('u0')['frozen'] == 0
    t = e.op('u0', 'ready', t['id'], ready=True)
    t = e.op('u1', 'ready', t['id'], ready=True)
    assert t['countdown_at'] == e.clock+5
    t = advance(e, t, 5)
    assert t['phase'] == 'playing'
    colors = {s['player']: s['color'] for s in t['seats']}
    t = act(e, t, 'resign')
    assert not any(s['ready'] for s in t['seats'])
    assert t['countdown_at'] is None and t['hand'] is None
    t = e.op('u1', 'ready', t['id'], ready=True)
    t = e.op('u0', 'start', t['id'])
    assert all(colors[s['player']] != s['color'] for s in t['seats'])
    assert t['hand_no'] == 2


def test_win_escrow_conservation_and_public_spectator(board):
    e = board
    before = total(e)
    t = start(e)
    assert e.s.wallet('u0')['frozen'] == 10
    assert e.points.balance('u0') == 4990 and total(e) == before
    for x in range(4):
        t = act(e, t, move=[x, 0])
        t = act(e, t, move=[x, 5])
    spectator = refresh(e, t, 'u2')
    assert spectator['hand']['board']['moves'] == t['hand']['board']['moves']
    assert spectator['hand']['legal_actions'] == {}
    assert spectator['me']['player'] is None
    assert 'uid' not in json.dumps(spectator)
    t = act(e, t, move=[4, 0])
    result = t['last_hand']['result']
    assert result['reason'] == 'five' and sum(result['net'].values()) == 0
    assert e.points.balance('u0') == 5010 and e.points.balance('u1') == 4990
    assert e.s.wallet('u0')['frozen'] == 0 and total(e) == before
    assert len(e.s.cash_detail(t['id'])['events']) == 5
    assert reason_label('gomoku.stake') == '五子棋本局押注冻结'
    assert t['last_hand']['clocks']['running'] is None
    with pytest.raises(GameError) as exc:
        e.s.proof(t['id'], 1)
    assert exc.value.code == 'invalid'


def test_board_full_draw_refunds_stakes(board):
    t = start(board)
    for move in full_draw():
        t = act(board, t, move=move)
    assert t['last_hand']['result']['reason'] == 'board_full'
    assert len(t['last_hand']['moves']) == 225
    assert t['last_hand']['result']['winner'] is None
    assert board.points.balance('u0') == board.points.balance('u1') == 5000
    assert total(board) == 40000


def test_draw_accept_reject_cooldown_and_clock_not_reset(board):
    e = board
    t = start(e)
    deadline = t['turn_deadline']
    e.clock += 2
    t = act(e, t, 'offer_draw', uid='u1')
    assert t['hand']['draw_offer']['player'] == t['seats'][1]['player']
    assert t['turn_deadline'] == deadline
    t = act(e, t, 'reject_draw', uid='u0')
    with pytest.raises(GameError) as exc:
        act(e, t, 'offer_draw', uid='u1')
    assert exc.value.code == 'draw_cooldown'
    for move in ([7, 7], [7, 8], [8, 7]):
        t = act(e, t, move=move)
    t = act(e, t, 'offer_draw', uid='u1')
    assert 'accept_draw' not in t['hand']['legal_actions']
    assert 'accept_draw' in refresh(e, t, 'u0')['hand']['legal_actions']


def test_draw_refunds_and_offer_removed_by_move(board):
    e = board
    t = start(e)
    t = act(e, t, 'offer_draw', uid='u1')
    t = act(e, t, move=[7, 7])
    assert t['hand']['draw_offer'] is None
    t = act(e, t, 'offer_draw', uid='u0')
    before = total(e)
    t = act(e, t, 'accept_draw', uid='u1')
    assert t['last_hand']['result'] == {'winner': None, 'reason': 'draw_agreed',
                                      'net': {s['player']: 0 for s in t['seats']}}
    assert e.points.balance('u0') == e.points.balance('u1') == 5000
    assert total(e) == before


@pytest.mark.parametrize('op', ['auto', 'away', 'top-up'])
def test_no_autoplay_and_no_point_undo(board, op):
    t = start(board)
    with pytest.raises(GameError) as exc:
        board.op('u0', op, t['id'], enabled=True, amount=10)
    assert exc.value.code == 'invalid'
    with pytest.raises(GameError):
        act(board, t, 'undo')
    with pytest.raises(GameError):
        act(board, t, move=[2, 2], uid='u1')
    with pytest.raises(GameError) as exc:
        act(board, t, move=[True, 2])
    assert exc.value.code == 'invalid'


def test_clock_aborted_and_late_action_cannot_revive(board):
    e = board
    t = start(e)
    before = total(e)
    e.clock = t['turn_deadline']
    with pytest.raises(GameError) as exc:
        act(e, t, move=[7, 7])
    assert exc.value.code == 'stale_turn'
    t = refresh(e, t)
    assert t['last_hand']['result']['reason'] == 'aborted'
    assert e.points.balance('u0') == e.points.balance('u1') == 5000
    assert total(e) == before


def test_step_timeout_and_clock_display(board):
    e = board
    t = start(e)
    e.clock += 2
    t = act(e, t, move=[7, 7])
    assert t['hand']['clocks']['remaining'][t['seats'][0]['player']] == 298
    e.clock += 1.25
    view = refresh(e, t)
    assert view['hand']['clocks']['remaining'][t['seats'][1]['player']] == 298.75
    t = advance(e, t, 28.75)
    assert t['last_hand']['result']['reason'] == 'timeout'
    assert t['last_hand']['result']['winner'] == t['seats'][0]['player']
    assert not any(s['auto'] for s in t['seats'])


def test_total_timeout_even_with_offline_grace(board):
    e = board
    e.cfg['gomoku_total_seconds'] = 10
    t = start(e)
    e.clock += 1
    e.s._presence[t['id']]['u1'] = e.clock-31
    t = act(e, t, move=[7, 7])
    assert t['turn_deadline'] == e.clock+10
    t = advance(e, t, 10)
    assert t['last_hand']['result']['reason'] == 'timeout'
    assert t['last_hand']['clocks']['remaining'][t['seats'][1]['player']] == 0


def test_offline_120_grace_persisted_return_does_not_shorten(board):
    e = board
    t = start(e)
    e.s._presence[t['id']]['u1'] = e.clock-31
    t = act(e, t, move=[7, 7])
    assert t['turn_deadline'] == e.clock+120
    original = t['turn_deadline']
    t = advance(e, t, 60)
    assert t['phase'] == 'playing'
    view = refresh(e, t, 'u1')
    assert view['turn_deadline'] == original
    assert view['hand']['clocks']['remaining'][t['seats'][1]['player']] == 240
    t = act(e, t, move=[8, 7])
    assert t['hand']['current_player'] == t['seats'][0]['player']


def test_disconnect_during_turn_extends_once_and_sse_is_online(board):
    e = board
    e.cfg['gomoku_step_seconds'] = 60
    t = start(e)
    t = act(e, t, move=[7, 7])
    t = advance(e, t, 31)
    assert t['turn_deadline'] == 1120
    t = advance(e, t, 30)
    assert t['turn_deadline'] == 1120
    # A new game with a live SSE is never granted offline grace.
    t = act(e, t, 'resign', uid='u0')
    t = e.op('u1', 'ready', t['id'], ready=True)
    t = e.op('u0', 'start', t['id'])
    async def connected():
        sub = e.s.subscribe(t['id'], 'u1')
        try:
            return advance(e, t, 40)
        finally:
            e.s.unsubscribe(t['id'], sub, 'u1')
    view = asyncio.run(connected())
    assert view['turn_deadline'] == 1121


def test_restart_preserves_deadline_and_exactly_once_timeout(board):
    e = board
    t = start(e)
    t = act(e, t, move=[7, 7])
    due = t['turn_deadline']
    e.clock += 5
    e.s = GameCenter(e.db, e.members, e.points, lambda: e.cfg, lambda: True, lambda: e.bot)
    assert refresh(e, t)['turn_deadline'] == due
    e.clock = due+30
    e.s.tick(now=e.clock)
    result = refresh(e, t)['last_hand']['result']
    count = len(e.s.cash_detail(t['id'])['events'])
    e.s.tick(now=e.clock)
    assert len(e.s.cash_detail(t['id'])['events']) == count
    assert result['reason'] == 'timeout'
    assert total(e) == 40000


def test_settlement_failure_rolls_back_money_state_and_request(board, monkeypatch):
    e = board
    t = start(e)
    old = raw(e, t)
    release = e.s.cash.release
    def fail(*a, **kw):
        release(*a, **kw)
        raise RuntimeError('injected failure after release')
    monkeypatch.setattr(e.s.cash, 'release', fail)
    with pytest.raises(RuntimeError):
        act(e, t, 'resign', request_id='rollback_board_123')
    assert raw(e, t) == old
    assert e.s.wallet('u0')['frozen'] == e.s.wallet('u1')['frozen'] == 10
    assert e.points.balance('u0') == e.points.balance('u1') == 4990
    assert not e.db.one('SELECT 1 FROM gc_requests WHERE request_id=?', ('rollback_board_123',))
    assert not e.db.one('SELECT 1 FROM play_cash_moves')
    monkeypatch.setattr(e.s.cash, 'release', release)
    act(e, t, 'resign', request_id='rollback_board_123')
    assert total(e) == 40000


def test_start_reserve_failure_rolls_back_first_reservation(board, monkeypatch):
    e = board
    t = e.op('u0', 'create', game='gomoku', tier='novice')
    t = e.op('u1', 'seat', t['id'])
    t = e.op('u1', 'ready', t['id'], ready=True)
    reserve = e.s.cash.reserve
    def fail(conn, scope, ref, uid, amount, **kw):
        reserve(conn, scope, ref, uid, amount, **kw)
        if uid == 'u1':
            raise RuntimeError('second reservation failed')
    monkeypatch.setattr(e.s.cash, 'reserve', fail)
    with pytest.raises(RuntimeError):
        e.op('u0', 'start', t['id'])
    assert e.points.balance('u0') == e.points.balance('u1') == 5000
    assert refresh(e, t)['hand_no'] == 0
    assert e.s.wallet('u0')['frozen'] == 0


def test_concurrent_idempotent_resign_and_competing_actions(board):
    e = board
    t = start(e)
    body = {'request_id': 'concurrent_board_123', 'hand_no': 1, 'turn': 1, 'action': 'resign'}
    with ThreadPoolExecutor(max_workers=8) as pool:
        views = list(pool.map(lambda _: e.s.mutate('u0', '玩家', 'actions', body, t['id'], now=e.clock), range(16)))
    assert all(v['table']['last_hand']['result']['reason'] == 'resign' for v in views)
    assert len(e.s.cash_detail(t['id'])['events']) == 5
    assert total(e) == 40000
    with pytest.raises(GameError) as exc:
        e.s.mutate('u0', '玩家', 'actions', {**body, 'action': 'offer_draw'}, t['id'], now=e.clock)
    assert exc.value.code == 'idempotency_conflict'
    t = e.op('u1', 'ready', t['id'], ready=True)
    t = e.op('u0', 'start', t['id'])
    def competing(i):
        try:
            return act(e, t, move=[i, 0], request_id=f'competing_board_{i}')['hand']['turn']
        except GameError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(competing, range(2)))
    assert sorted(map(str, outcomes)) == ['2', 'stale_turn']


def test_practice_undo_no_clock_refund_bot_draw_and_new_hand(board):
    e = board
    t = start(e, tier='practice', robot_level='easy')
    assert t['rules']['robot_level'] == 'easy' and t['me']['wallet']['beans'] == 9900
    e.clock += 2
    t = act(e, t, move=[7, 7])
    assert t['hand']['current_player'].startswith('bot_')
    t = advance(e, t, 1.5)
    assert len(t['hand']['moves']) == 2
    before = t['hand']['clocks']['remaining']
    e.clock += 1
    t = act(e, t, 'undo')
    assert t['hand']['board']['moves'] == []
    assert t['hand']['clocks']['remaining'][t['seats'][0]['player']] == before[t['seats'][0]['player']]-1
    assert 'undo' not in t['hand']['legal_actions']
    t = act(e, t, 'offer_draw')
    t = advance(e, t, .5)
    assert t['hand']['draw_offer'] is None
    t = advance(e, t, 30)
    assert t['last_hand']['result']['reason'] == 'timeout'  # Undo does not revive opening abort.
    assert t['me']['wallet']['beans'] == 9900
    t = e.op('u0', 'start', t['id'])
    assert t['hand']['current_player'].startswith('bot_')
    assert len(advance(e, t, 1.5)['hand']['moves']) == 1


def test_robot_nonblocking_other_table_and_late_result_ignored(board, monkeypatch):
    e = board
    entered, released = threading.Event(), threading.Event()
    game = e.s.games['gomoku']
    def slow(*_):
        entered.set()
        released.wait(3)
        return [0, 0]
    monkeypatch.setattr(game, 'robot_move', slow)
    t = start(e, tier='practice')
    t = act(e, t, move=[7, 7])
    assert entered.wait(1)
    try:
        began = time.monotonic()
        other = e.op('u2', 'create', game='gomoku', tier='novice')
        assert time.monotonic()-began < .5
        assert other['phase'] == 'waiting'
        t = advance(e, t, 1.5)
        assert len(t['hand']['moves']) == 2  # local fallback, no wait for engine
        t = act(e, t, 'undo')
        released.set()
        t = advance(e, t, .5)
        assert t['hand']['moves'] == []  # completed result belongs to old position_no
    finally:
        released.set()


def test_quick_start_validates_level_before_joining_and_matches_points(board):
    e = board
    t = e.op('u0', 'create', game='gomoku', tier='novice')
    with pytest.raises(GameError) as exc:
        e.op('u1', 'quick', game='gomoku', tier='novice', robot_level='easy')
    assert exc.value.code == 'invalid'
    t2 = e.op('u1', 'quick', game='gomoku', tier='novice')
    assert t2['id'] == t['id'] and len(t2['seats']) == 2
    assert not any(s['bot'] for s in t2['seats'])


def test_waiting_offline_removal_card_stake_and_leave_resigns(board):
    e = board
    e.cfg.update(broadcast_enabled=True, broadcast_groups='-123456')
    t = start(e)
    card = json.loads(e.db.one('SELECT payload_json FROM gc_outbox WHERE table_id=?', (t['id'],))['payload_json'])
    assert '五子棋' in card['text'] and '每人押 10 积分' in card['text']
    t = e.op('u0', 'leave', t['id'])
    assert t['last_hand']['result']['reason'] == 'resign'
    assert t['me']['player'] is None
    t = advance(e, t, 181)
    assert t['phase'] == 'closed'
    assert total(e) == 40000


def test_xiangqi_adapter_privacy_history_and_illegal_move(board):
    e = board
    t = start(e, game='xiangqi')
    assert t['hand']['players'][0]['color'] == 'red'
    assert t['hand']['clocks']['remaining'][t['seats'][0]['player']] == 600
    assert t['hand']['legal_moves'] == xiangqi_engine.Position.from_fen(t['hand']['board']['fen']).legal_moves()
    assert 'legal_moves' not in refresh(e, t, 'u1')['hand']
    assert 'legal_moves' not in refresh(e, t, 'u2')['hand']
    fen = t['hand']['board']['fen']
    with pytest.raises(GameError) as exc:
        act(e, t, move='a0a9')
    assert exc.value.code == 'illegal_action'
    t = act(e, t, move=t['hand']['legal_moves'][0])
    assert t['hand']['board']['fen'] != fen and len(t['hand']['moves']) == 1
    assert 'legal_moves' not in t['hand']  # mover is now the non-current viewer
    view = refresh(e, t, 'u1')
    assert view['hand']['legal_moves']
    assert isinstance(view['hand']['in_check'], bool)
    t = act(e, t, 'resign')
    assert 'legal_moves' not in t['last_hand']


# Fixed legal game generated from the standard initial position against Pikafish.
# No starting-position injection or mocked move/outcome is used for this game.
MATE_GAME = [
    'h2e2', 'b7b3', 'e2e6', 'i9i8', 'h0g2', 'c6c5', 'b2e2', 'h7h3',
    'b0c2', 'h9i7', 'e6i6', 'f9e8', 'i6i8', 'b3b7', 'i0h0', 'h3e3',
    'f0e1', 'e9f9', 'a0b0', 'b7g7', 'i8i9', 'g9e7', 'b0b4', 'e3e1',
    'g0i2', 'e1b1', 'c2e3', 'e8d7', 'b4f4', 'f9e9', 'e3f5', 'e9f9', 'f5g7',
]


def test_xiangqi_complete_game_capture_check_checkmate_and_settlement(board):
    e = board
    t = start(e, game='xiangqi')
    position = xiangqi_engine.Position.from_fen(xiangqi_engine.INITIAL_FEN)
    captures, checks = [], []
    for ply, move in enumerate(MATE_GAME, 1):
        uid = 'u0' if position.side == 'w' else 'u1'
        current = refresh(e, t, uid)
        assert current['hand']['legal_moves'] == position.legal_moves()
        assert 'legal_moves' not in refresh(e, t, 'u1' if uid == 'u0' else 'u0')['hand']
        assert 'legal_moves' not in refresh(e, t, 'u2')['hand']
        if position.is_capture(move):
            captures.append(ply)
        position = position.make(move)
        e.clock += .1
        t = act(e, t, move=move)
        hand = t['hand'] or t['last_hand']
        assert hand['board']['fen'] == position.fen()
        assert len(hand['board']['fen'].split()) == 2
        assert hand['in_check'] == position.in_check()
        assert hand['last_move']['move'] == move
        if hand['in_check']:
            checks.append(ply)
        assert bool(hand['result']) == (ply == len(MATE_GAME))
    assert captures == [3, 11, 13, 16, 24, 33]
    assert checks == [11, 16, 21, 29, 31, 33]
    result = t['last_hand']['result']
    assert result['reason'] == 'checkmate' and result['winner'] == t['seats'][0]['player']
    assert not position.legal_moves()
    assert e.points.balance('u0') == 5010 and e.points.balance('u1') == 4990
    assert total(e) == 40000 and e.s.wallet('u0')['frozen'] == 0


@pytest.mark.parametrize('reason,fen,moves,winner', [
    ('checkmate', '4k4/3R1R3/9/9/9/9/9/9/9/3K5 w', ['d8e8'], 'w'),
    ('stalemate', '4k4/3R5/5R3/9/9/9/9/9/9/5K3 w', ['f7f8'], 'w'),
    ('perpetual_check', LONG_CHECK, CHECK_CYCLE*2, 'b'),
    ('repetition', xiangqi_engine.INITIAL_FEN, ['b0c2', 'b9c7', 'c2b0', 'c7b9']*2, None),
    ('move_limit', xiangqi_engine.INITIAL_FEN, None, None),
    ('insufficient', '4k4/9/9/9/9/9/9/9/9/3K5 w', ['d0d1'], None),
])
def test_xiangqi_real_outcome_settlement(board, monkeypatch, reason, fen, moves, winner):
    # Only seed the starting position of endgame fixtures. Every move, legality,
    # check and outcome is computed by the real, unmodified rules engine.
    if moves is None:
        moves = quiet_history(120)
    monkeypatch.setattr(xiangqi_engine, 'INITIAL_FEN', fen)
    t = start(board, game='xiangqi')
    for i, move in enumerate(moves):
        t = act(board, t, move=move)
        if i < len(moves)-1:
            assert t['hand'] and t['hand']['result'] is None
    result = t['last_hand']['result']
    assert result['reason'] == reason
    assert result['winner'] == (t['seats'][0 if winner == 'w' else 1]['player'] if winner else None)
    if winner is None:
        assert board.points.balance('u0') == board.points.balance('u1') == 5000
        assert set(result['net'].values()) == {0}
    else:
        assert sorted(result['net'].values()) == [-10, 10]
    assert total(board) == 40000
    assert board.s.wallet('u0')['frozen'] == board.s.wallet('u1')['frozen'] == 0


def test_xiangqi_practice_real_robot_and_undo_restores_fen(board):
    t = start(board, game='xiangqi', tier='practice')
    initial = t['hand']['board']['fen']
    t = act(board, t, move='h2e2')
    t = advance(board, t, 1.5)
    assert len(t['hand']['moves']) == 2
    t = act(board, t, 'undo')
    assert t['hand']['board']['fen'] == initial
    assert t['hand']['legal_moves'] == xiangqi_engine.Position.from_fen(initial).legal_moves()
