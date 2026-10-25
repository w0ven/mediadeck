"""Pure 15x15 freestyle gomoku. Coordinates are zero-based (x, y)."""
from __future__ import annotations

SIZE = 15
DIRECTIONS = ((1, 0), (0, 1), (1, 1), (1, -1))


def coordinate(move):
    if (not isinstance(move, (list, tuple)) or len(move) != 2
            or any(type(n) is not int or not 0 <= n < SIZE for n in move)):
        raise ValueError('落子坐标须为 0～14 的两个整数')
    return tuple(move)


def board(moves):
    cells = {}
    for i, move in enumerate(moves):
        point = coordinate(move)
        if point in cells:
            raise ValueError('该位置已有棋子')
        cells[point] = i % 2
    return cells


def five(cells, point, side):
    x, y = point
    for dx, dy in DIRECTIONS:
        count = 1
        for sign in (-1, 1):
            nx, ny = x + dx * sign, y + dy * sign
            while cells.get((nx, ny)) == side:
                count += 1
                nx, ny = nx + dx * sign, ny + dy * sign
        if count >= 5:
            return True
    return False


def outcome(moves):
    """Winner is 'black'/'white'/None; six-or-more also wins. No forbidden moves."""
    cells = {}
    for i, move in enumerate(moves):
        point = coordinate(move)
        if point in cells:
            raise ValueError('该位置已有棋子')
        cells[point] = i % 2
        if five(cells, point, i % 2):
            return {'winner': ('black', 'white')[i % 2], 'reason': 'five'}
    if len(cells) == SIZE * SIZE:
        return {'winner': None, 'reason': 'board_full'}
    return None


def legal_moves(moves):
    cells = board(moves)
    if outcome(moves):
        return []
    return [[x, y] for y in range(SIZE) for x in range(SIZE) if (x, y) not in cells]


def make(moves, move):
    point = coordinate(move)
    if point in board(moves):
        raise ValueError('该位置已有棋子')
    if outcome(moves):
        raise ValueError('对局已结束')
    return [list(p) for p in moves] + [list(point)]
