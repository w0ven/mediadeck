"""Fixed Pikafish 2026-09-06 perft data; CI needs no engine executable.

Counts at depths 1–4 were cross-checked locally, including the 3,290,240-node
initial position. CI runs 1–3, also with colors/coordinates reversed.
"""
from __future__ import annotations

import random

import pytest

from app.modules.xiangqi_engine import INITIAL_FEN, Position, outcome

PERFT_CASES = [
    ('initial', INITIAL_FEN, (44, 1920, 79666, 3290240)),
    ('flying_general', '4k4/9/9/9/9/4C4/9/9/9/4K4 w', (10, 27, 420, 837)),
    ('cannon_screens', '4k4/9/1r7/4p4/9/1p1p5/9/1C1A5/9/4K4 w',
     (10, 207, 2682, 56302)),
    ('horse_legs', '4k4/9/9/4p4/9/4P4/3RN4/9/9/4K4 w', (20, 60, 1408, 4605)),
    ('elephant_eyes_river', '4k4/9/9/4p4/9/2B6/1N1C5/9/9/2BAK4 w',
     (25, 96, 2392, 8674)),
    ('palace_edges', '3ak4/4a4/9/4p4/9/9/4P4/9/4A4/3AK4 w', (5, 24, 113, 530)),
    ('rook_check_evasion', '3k5/9/9/9/9/9/4r4/3N5/9/4KA3 w', (2, 37, 228, 3916)),
    ('cannon_check_evasion', '4k4/9/9/4p4/4c4/9/4P4/9/3R5/4K4 w',
     (3, 34, 494, 8429)),
    ('crossed_pawns', '3k5/9/9/4p4/2P3p2/9/6P2/9/9/4K4 b', (4, 25, 120, 775)),
    ('endgame', '3k5/9/9/9/9/4p4/9/2N6/9/4KA3 w', (10, 50, 381, 1795)),
]
FENS = {name: fen for name, fen, _ in PERFT_CASES}
LONG_CHECK = '4k4/3R5/9/9/9/9/9/9/9/5K3 w'
CHECK_CYCLE = ['d8e8', 'e9d9', 'e8d8', 'd9e9']
MUTUAL_CHECK = '4kr3/9/9/4c4/9/9/4R4/9/5K3/4C4 w'
MUTUAL_CYCLE = ['e3f3', 'e6f6', 'f3e3', 'f6e6']


def perft(position, depth):
    if depth == 0:
        return 1
    moves = position.legal_moves()
    if depth == 1:
        return len(moves)
    return sum(perft(position.make(move), depth - 1) for move in moves)


def reverse_fen(fen):
    board, side = fen.split()
    return '/'.join(rank[::-1].swapcase() for rank in reversed(board.split('/'))) + (
        ' b' if side == 'w' else ' w')


def reverse_move(move):
    return ''.join(chr(105 - (ord(move[i]) - 97)) + str(9 - int(move[i + 1]))
                   for i in (0, 2))


@pytest.mark.parametrize('name,fen,counts', PERFT_CASES, ids=[case[0] for case in PERFT_CASES])
@pytest.mark.parametrize('depth', (1, 2, 3))
@pytest.mark.parametrize('reverse', (False, True))
def test_pikafish_fixed_perft(name, fen, counts, depth, reverse):
    position = Position.from_fen(reverse_fen(fen) if reverse else fen)
    assert perft(position, depth) == counts[depth - 1]


def test_initial_fen_coordinates_and_immutable_moves():
    position = Position.from_fen(INITIAL_FEN)
    assert position.fen() == INITIAL_FEN
    assert position.side == 'w'
    assert position.piece_at('e0') == 'K'
    assert position.piece_at('e9') == 'k'
    assert position.piece_at('b2') == 'C'
    assert position.piece_at('h7') == 'c'
    assert position.piece_at('e4') is None
    assert not position.in_check()
    moves = position.legal_moves()
    assert len(moves) == len(set(moves)) == 44
    for move in moves:
        child = position.make(move)
        assert child is not position
        assert child.side == 'b'
        assert child.piece_at(move[:2]) is None
        assert child.piece_at(move[2:]) == position.piece_at(move[:2])
        assert position.fen() == INITIAL_FEN
        assert Position.from_fen(child.fen()).legal_moves() == child.legal_moves()
    moves.clear()
    assert len(position.legal_moves()) == 44
    with pytest.raises(AttributeError):
        position.side = 'b'


def test_six_field_fen_canonicalization_ignores_counters():
    assert Position.from_fen(INITIAL_FEN + ' - - 119 42').fen() == INITIAL_FEN
    assert outcome(INITIAL_FEN + ' - - 120 61', []) is None


@pytest.mark.parametrize('fen', [
    None, '', '9/9 w', INITIAL_FEN.replace(' w', ' r'), INITIAL_FEN + ' extra',
    INITIAL_FEN.replace('RNBAKABNR', 'RNBAZABNR'), INITIAL_FEN.replace('RNBAKABNR', '9'),
    INITIAL_FEN.replace('RNBAKABNR', 'RNBAKKBNR'), INITIAL_FEN.replace('/9/', '/8/', 1),
    INITIAL_FEN.replace('RNBAKABNR', 'KNBARABNR'), INITIAL_FEN + ' - - -1 1',
    INITIAL_FEN + ' - - 0 0', INITIAL_FEN + ' K - 0 1', INITIAL_FEN + ' - - x 1',
])
def test_invalid_fen(fen):
    with pytest.raises(ValueError):
        Position.from_fen(fen)


@pytest.mark.parametrize('move', [None, '', 'a0', 'a0a1x', 'a0j1', 'a0a0', 'a1a2',
                                  'a9a8', 'a0b0', 'a0a4', 'h2h9x', 'e0e2'])
def test_illegal_moves_do_not_change_position(move):
    position = Position.from_fen(INITIAL_FEN)
    for method in (position.make, position.is_capture):
        with pytest.raises(ValueError):
            method(move)
    assert position.fen() == INITIAL_FEN


@pytest.mark.parametrize('square', [None, '', 'a10', 'j0', 'A0', 'e-1'])
def test_invalid_square(square):
    with pytest.raises(ValueError):
        Position.from_fen(INITIAL_FEN).piece_at(square)


def test_flying_generals_and_pinned_screen():
    position = Position.from_fen(FENS['flying_general'])
    assert not position.in_check()
    assert 'e4e5' in position.legal_moves()
    assert 'e4d4' not in position.legal_moves()
    with pytest.raises(ValueError):
        position.make('e4d4')
    facing = Position.from_fen('4k4/9/9/9/9/9/9/9/9/4K4 w')
    assert facing.in_check()
    assert set(facing.legal_moves()) == {'e0d0', 'e0f0'}
    with pytest.raises(ValueError):
        facing.make('e0e9')


def test_horse_legs_and_elephant_eyes_and_river():
    horse = Position.from_fen(FENS['horse_legs'])
    assert {m for m in horse.legal_moves() if m.startswith('e3')} == {
        'e3d1', 'e3f1', 'e3g2', 'e3g4'}
    elephant = Position.from_fen(FENS['elephant_eyes_river'])
    assert not any(move.startswith('c4') for move in elephant.legal_moves())
    assert {'c0a2', 'c0e2'} <= set(elephant.legal_moves())
    clear = Position.from_fen('4k4/9/9/4p4/9/2B6/9/9/9/4K4 w')
    assert {m for m in clear.legal_moves() if m.startswith('c4')} == {'c4a2', 'c4e2'}


def test_palace_king_and_advisor_moves():
    position = Position.from_fen(FENS['palace_edges'])
    assert set(position.legal_moves()) == {'e0f0', 'e1d2', 'e1f2', 'e1f0', 'e3e4'}
    edge = Position.from_fen('5k3/9/9/9/9/9/9/3K5/9/9 w')
    assert set(edge.legal_moves()) == {'d2e2', 'd2d1'}


def test_cannon_exactly_one_screen_and_rook_blocking():
    position = Position.from_fen(FENS['cannon_screens'])
    assert position.is_capture('b2b7')
    assert position.make('b2b7').piece_at('b7') == 'C'
    assert not position.is_capture('b2b3')
    for move in ('b2b4', 'b2b5', 'b2d2'):
        with pytest.raises(ValueError):
            position.make(move)
    initial = Position.from_fen(INITIAL_FEN)
    assert initial.is_capture('b2b9')
    assert not initial.is_capture('a0a2')
    with pytest.raises(ValueError):
        initial.make('a0a3')


def test_check_evasions_and_no_self_check():
    rook = Position.from_fen(FENS['rook_check_evasion'])
    assert rook.in_check()
    assert set(rook.legal_moves()) == {'e0d0', 'f0e1'}
    cannon = Position.from_fen(FENS['cannon_check_evasion'])
    assert cannon.in_check()
    assert set(cannon.legal_moves()) == {'e0d0', 'e0f0', 'd1e1'}
    with pytest.raises(ValueError):
        cannon.make('e3e4')


def test_pawns_before_and_after_crossing_for_both_colors():
    black = Position.from_fen(FENS['crossed_pawns'])
    assert {m for m in black.legal_moves() if m.startswith('g5')} == {'g5g4'}
    red = black.make('g5g4')
    assert {m for m in red.legal_moves() if m.startswith('c5')} == {'c5b5', 'c5d5', 'c5c6'}
    assert {m for m in red.legal_moves() if m.startswith('g3')} == {'g3g4'}
    black = red.make('c5c6')
    assert {m for m in black.legal_moves() if m.startswith('g4')} == {'g4f4', 'g4h4', 'g4g3'}


@pytest.mark.parametrize('reverse', (False, True))
def test_checkmate_and_stalemate_are_losses(reverse):
    mate = '3Rk4/3R5/9/9/9/9/9/9/9/5K3 b'
    stale = '4k4/3R1R3/9/9/9/9/9/9/9/5K3 b'
    for fen, reason in ((mate, 'checkmate'), (stale, 'stalemate')):
        if reverse:
            fen = reverse_fen(fen)
        position = Position.from_fen(fen)
        assert not position.legal_moves()
        assert position.in_check() == (reason == 'checkmate')
        assert outcome(fen, []) == {'winner': 'b' if reverse else 'w', 'reason': reason}


def test_checkmate_after_move_takes_precedence_and_stops_replay():
    fen = '4k4/3R1R3/9/9/9/9/9/9/9/3K5 w'
    assert outcome(fen, []) is None
    assert outcome(fen, ['d8e8', 'not-a-move']) == {'winner': 'w', 'reason': 'checkmate'}


@pytest.mark.parametrize('reverse', (False, True))
def test_single_sided_perpetual_check_loses(reverse):
    fen, cycle = LONG_CHECK, CHECK_CYCLE
    if reverse:
        fen, cycle = reverse_fen(fen), [reverse_move(move) for move in cycle]
    assert outcome(fen, cycle) is None
    assert outcome(fen, cycle * 2) == {
        'winner': 'w' if reverse else 'b', 'reason': 'perpetual_check'}


def test_mutual_perpetual_check_is_draw_with_real_legal_moves():
    position = Position.from_fen(MUTUAL_CHECK)
    for move in MUTUAL_CYCLE * 2:
        assert position.in_check()
        position = position.make(move)
        assert position.in_check()
    assert position.fen() == MUTUAL_CHECK
    assert outcome(MUTUAL_CHECK, MUTUAL_CYCLE) is None
    assert outcome(MUTUAL_CHECK, MUTUAL_CYCLE * 2) == {
        'winner': None, 'reason': 'perpetual_check'}


def test_ordinary_threefold_and_initial_occurrence():
    cycle = ['b0c2', 'b9c7', 'c2b0', 'c7b9']
    assert outcome(INITIAL_FEN, cycle) is None
    assert outcome(INITIAL_FEN, cycle * 2) == {'winner': None, 'reason': 'repetition'}


def test_perpetual_check_requires_every_move_across_both_cycles():
    first = ['d8d7', 'e9e8', 'd7c7', 'e8e9', 'c7c8', 'e9d9', 'c8d8', 'd9e9']
    assert outcome(LONG_CHECK, first) is None
    assert outcome(LONG_CHECK, first + CHECK_CYCLE) == {'winner': None, 'reason': 'repetition'}


def quiet_history(count, *, capture_at=None):
    rng = random.Random(41)
    position = Position.from_fen(INITIAL_FEN)
    seen, moves = {position.fen()}, []
    for ply in range(count):
        candidates = position.legal_moves()
        rng.shuffle(candidates)
        for move in candidates:
            capture = position.piece_at(move[2:]) is not None
            if capture != (ply == capture_at):
                continue
            child = position.make(move)
            if child.fen() in seen or child.in_check() or not child.legal_moves():
                continue
            moves.append(move)
            position = child
            seen.add(position.fen())
            break
        else:
            pytest.fail('seeded quiet walk has no candidate')
    return moves


def test_120_plies_without_capture_is_draw_not_60_plies():
    moves = quiet_history(120)
    assert outcome(INITIAL_FEN, moves[:60]) is None
    assert outcome(INITIAL_FEN, moves[:119]) is None
    assert outcome(INITIAL_FEN, moves) == {'winner': None, 'reason': 'move_limit'}


def test_capture_resets_move_limit():
    moves = quiet_history(171, capture_at=50)
    assert outcome(INITIAL_FEN, moves[:120]) is None
    assert outcome(INITIAL_FEN, moves[:170]) is None
    assert outcome(INITIAL_FEN, moves) == {'winner': None, 'reason': 'move_limit'}


@pytest.mark.parametrize('fen', [
    '4k4/9/9/9/9/9/9/9/9/3K5 w',
    '2bak4/9/9/9/9/9/9/9/4A4/2B1K4 w',
])
def test_insufficient_material(fen):
    assert outcome(fen, []) == {'winner': None, 'reason': 'insufficient'}


@pytest.mark.parametrize('piece', 'RNCP')
def test_any_attacking_material_prevents_insufficient_draw(piece):
    fen = f'5k3/9/9/9/9/9/{piece}8/9/9/3K5 w'
    assert outcome(fen, []) is None


def test_outcome_ongoing_and_illegal_history():
    assert outcome(INITIAL_FEN, []) is None
    assert outcome(INITIAL_FEN, ['h2e2']) is None
    with pytest.raises(ValueError):
        outcome(INITIAL_FEN, ['a0a9'])
