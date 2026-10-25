"""Freestyle rule boundaries and pure-input invariants."""
import pytest

from app.modules import gomoku_engine as engine


@pytest.mark.parametrize('line', [
    [(x, 0) for x in range(5)], [(14, y) for y in range(10, 15)],
    [(n, n) for n in range(5)], [(n, 14-n) for n in range(5)],
    [(9+n, 9+n) for n in range(6)],
])
def test_five_all_directions_edges_and_overline(line):
    # For six-in-a-row, fill the middle last so there is no earlier five.
    if len(line) == 6:
        line = line[:3] + line[4:] + line[3:4]
    moves = []
    for i, point in enumerate(line):
        moves.append(list(point))
        if i < len(line)-1:
            moves.append([i*2, 7])
    assert engine.outcome(moves) == {'winner': 'black', 'reason': 'five'}
    assert not engine.legal_moves(moves)
    with pytest.raises(ValueError):
        engine.make(moves, [7, 9])


def full_draw():
    points = ([], [])
    for y in range(15):
        for x in range(15):
            points[int((x+2*y) % 4 >= 2)].append([x, y])
    assert list(map(len, points)) == [113, 112]
    return [points[i % 2][i//2] for i in range(225)]


def test_full_board_draw_and_white_wins():
    moves = full_draw()
    assert engine.outcome(moves[:-1]) is None
    assert engine.outcome(moves) == {'winner': None, 'reason': 'board_full'}
    moves = [[14, 14]]
    for x in range(5):
        moves.append([x, 0])
        if x < 4:
            moves.append([x*2, 8])
    assert engine.outcome(moves) == {'winner': 'white', 'reason': 'five'}


@pytest.mark.parametrize('bad', [[-1, 0], [15, 0], [0, 15], [0, True], [0, 1.0], [0], '7,7', None])
def test_invalid_coordinates(bad):
    with pytest.raises(ValueError):
        engine.make([], bad)


def test_occupied_purity_and_no_wraparound():
    moves = [[7, 7]]
    after = engine.make(moves, [8, 7])
    after[0][0] = 0
    assert moves == [[7, 7]]
    with pytest.raises(ValueError):
        engine.make(moves, [7, 7])
    with pytest.raises(ValueError):
        engine.board([[1, 1], [1, 1]])
    cells = {(x, 0): 0 for x in range(11, 15)}
    cells[0, 1] = 0
    assert not engine.five(cells, (0, 1), 0)
    assert len(engine.legal_moves(moves)) == 224
