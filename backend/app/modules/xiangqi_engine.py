"""Pure Xiangqi rules, with Pikafish coordinates (a0 is Red's left corner).

FEN output has board and side only; standard six-field FEN is also accepted,
but counters are ignored. Adjudication uses the supplied history, counting
plies, not full moves. Stalemate loses. Repetition includes the side to move;
checks over BOTH cycles leading to the third occurrence decide long check.
Long chase (长捉), combined chase/check bans and tournament-specific exceptions
are intentionally not implemented. Kings are checked/mated, never captured.
"""
from __future__ import annotations

INITIAL_FEN = 'rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w'
_PIECES = ' KABNRCP'
_SQUARES = tuple(f'{chr(97 + x)}{y}' for y in range(10) for x in range(9))
_INDEX = {square: index for index, square in enumerate(_SQUARES)}


def _palace(x: int, y: int, side: int) -> bool:
    return 3 <= x <= 5 and (0 <= y <= 2 if side == 1 else 7 <= y <= 9)


def _geometry():
    rays, steps = [], {}
    for square in range(90):
        x, y = square % 9, square // 9
        lines = []
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            line = []
            nx, ny = x + dx, y + dy
            while 0 <= nx < 9 and 0 <= ny < 10:
                line.append(ny * 9 + nx)
                nx, ny = nx + dx, ny + dy
            lines.append(tuple(line))
        rays.append(tuple(lines))
        for side in (1, -1):
            for kind in (1, 2, 3, 4, 7):
                destinations = []
                if kind == 1:
                    offsets = ((1, 0), (-1, 0), (0, 1), (0, -1))
                elif kind == 2:
                    offsets = ((1, 1), (-1, 1), (1, -1), (-1, -1))
                elif kind == 3:
                    offsets = ((2, 2), (-2, 2), (2, -2), (-2, -2))
                elif kind == 4:
                    offsets = ((1, 2), (-1, 2), (1, -2), (-1, -2),
                               (2, 1), (2, -1), (-2, 1), (-2, -1))
                else:
                    crossed = y >= 5 if side == 1 else y <= 4
                    offsets = ((0, side), (1, 0), (-1, 0)) if crossed else ((0, side),)
                for dx, dy in offsets:
                    nx, ny = x + dx, y + dy
                    if not (0 <= nx < 9 and 0 <= ny < 10):
                        continue
                    if kind in (1, 2) and not _palace(nx, ny, side):
                        continue
                    if kind == 3 and (ny > 4 if side == 1 else ny < 5):
                        continue
                    blocker = -1
                    if kind == 3:
                        blocker = (y + dy // 2) * 9 + x + dx // 2
                    elif kind == 4:
                        blocker = (y + (dy // 2 if abs(dy) == 2 else 0)) * 9
                        blocker += x + (dx // 2 if abs(dx) == 2 else 0)
                    destinations.append((ny * 9 + nx, blocker))
                steps[side * kind, square] = tuple(destinations)
    attacks = {}
    for (piece, source), destinations in steps.items():
        for target, blocker in destinations:
            attacks.setdefault((1 if piece > 0 else -1, target), []).append(
                (source, piece, blocker))
    return tuple(rays), steps, attacks


_RAYS, _STEPS, _ATTACKS = _geometry()


def _attacked(board, target: int, enemy: int) -> bool:
    for direction, ray in enumerate(_RAYS[target]):
        screened = False
        for square in ray:
            piece = board[square]
            if not piece:
                continue
            if not screened:
                if piece == enemy * 5 or (direction >= 2 and piece == enemy):
                    return True
                screened = True
            else:
                if piece == enemy * 6:
                    return True
                break
    for source, piece, blocker in _ATTACKS.get((enemy, target), ()):
        if board[source] == piece and (blocker < 0 or not board[blocker]):
            return True
    return False


def _targets(board, source: int):
    piece = board[source]
    side, kind = (1 if piece > 0 else -1), abs(piece)
    if kind in (5, 6):
        for ray in _RAYS[source]:
            screened = False
            for target in ray:
                occupant = board[target]
                if not screened:
                    if not occupant:
                        yield target
                        continue
                    if kind == 5:
                        if occupant * side < 0 and abs(occupant) != 1:
                            yield target
                        break
                    screened = True
                elif occupant:
                    if occupant * side < 0 and abs(occupant) != 1:
                        yield target
                    break
    else:
        for target, blocker in _STEPS[piece, source]:
            if board[target] * side > 0 or abs(board[target]) == 1:
                continue
            if blocker < 0 or not board[blocker]:
                yield target


class Position:
    """Immutable-by-interface board. Invalid FEN, squares and moves raise ValueError."""

    __slots__ = ('_board', '_kings', '_side')

    def __init__(self, board: tuple[int, ...], side: str):
        self._board = board
        self._side = side
        self._kings = (board.index(1), board.index(-1))

    @classmethod
    def from_fen(cls, fen: str) -> Position:
        if not isinstance(fen, str) or not fen:
            raise ValueError('FEN 必须是非空字符串')
        fields = fen.split()
        if len(fields) not in (2, 6) or fields[1] not in ('w', 'b'):
            raise ValueError('FEN 字段或行棋方无效')
        if len(fields) == 6 and (
            fields[2:4] != ['-', '-'] or not fields[4].isascii()
            or not fields[4].isdigit() or not fields[5].isascii()
            or not fields[5].isdigit() or int(fields[5]) < 1
        ):
            raise ValueError('FEN 计数字段无效')
        ranks = fields[0].split('/')
        if len(ranks) != 10:
            raise ValueError('FEN 必须有十行')
        board = []
        for rank in reversed(ranks):
            row = []
            for token in rank:
                if token in '123456789':
                    row.extend([0] * int(token))
                elif token in 'KABNRCPkabnrcp':
                    row.append(_PIECES.index(token.upper()) * (1 if token.isupper() else -1))
                else:
                    raise ValueError('FEN 棋子无效')
            if len(row) != 9:
                raise ValueError('FEN 每行必须有九格')
            board.extend(row)
        for king in (1, -1):
            if board.count(king) != 1:
                raise ValueError('双方必须各有一个将帅')
            square = board.index(king)
            if not _palace(square % 9, square // 9, king):
                raise ValueError('将帅必须在九宫内')
        return cls(tuple(board), fields[1])

    @property
    def side(self) -> str:
        return self._side

    def fen(self) -> str:
        ranks = []
        for y in range(9, -1, -1):
            row, empty = '', 0
            for piece in self._board[y * 9:y * 9 + 9]:
                if not piece:
                    empty += 1
                    continue
                if empty:
                    row += str(empty)
                    empty = 0
                letter = _PIECES[abs(piece)]
                row += letter if piece > 0 else letter.lower()
            if empty:
                row += str(empty)
            ranks.append(row)
        return '/'.join(ranks) + ' ' + self.side

    def piece_at(self, square: str) -> str | None:
        if not isinstance(square, str) or square not in _INDEX:
            raise ValueError('坐标无效')
        piece = self._board[_INDEX[square]]
        if not piece:
            return None
        letter = _PIECES[abs(piece)]
        return letter if piece > 0 else letter.lower()

    def in_check(self) -> bool:
        side = 1 if self.side == 'w' else -1
        return _attacked(self._board, self._kings[side == -1], -side)

    def legal_moves(self) -> list[str]:
        side = 1 if self.side == 'w' else -1
        board = list(self._board)
        king = self._kings[side == -1]
        moves = []
        for source, piece in enumerate(self._board):
            if piece * side <= 0:
                continue
            for target in _targets(board, source):
                captured = board[target]
                board[source], board[target] = 0, piece
                if not _attacked(board, target if abs(piece) == 1 else king, -side):
                    moves.append(_SQUARES[source] + _SQUARES[target])
                board[source], board[target] = piece, captured
        return moves

    def _move(self, move: str) -> tuple[int, int]:
        if (not isinstance(move, str) or len(move) != 4
                or move[:2] not in _INDEX or move[2:] not in _INDEX):
            raise ValueError('着法无效')
        source, target = _INDEX[move[:2]], _INDEX[move[2:]]
        side = 1 if self.side == 'w' else -1
        if self._board[source] * side <= 0 or target not in _targets(self._board, source):
            raise ValueError('非法着法')
        return source, target

    def make(self, move: str) -> Position:
        source, target = self._move(move)
        board = list(self._board)
        piece = board[source]
        board[source], board[target] = 0, piece
        side = 1 if self.side == 'w' else -1
        king = target if abs(piece) == 1 else self._kings[side == -1]
        if _attacked(board, king, -side):
            raise ValueError('着法导致己方被将军')
        return Position(tuple(board), 'b' if self.side == 'w' else 'w')

    def is_capture(self, move: str) -> bool:
        self.make(move)
        return bool(self._board[_INDEX[move[2:]]])


def _terminal(position: Position) -> dict | None:
    if not position.legal_moves():
        return {'winner': 'b' if position.side == 'w' else 'w',
                'reason': 'checkmate' if position.in_check() else 'stalemate'}
    if not any(abs(piece) in (4, 5, 6, 7) for piece in position._board):
        return {'winner': None, 'reason': 'insufficient'}
    return None


def _repetition(checks: list[tuple[str, bool]], start: int) -> dict:
    cycle = checks[start:]
    checking = {side for side in ('w', 'b')
                if any(mover == side for mover, _ in cycle)
                and all(check for mover, check in cycle if mover == side)}
    if len(checking) == 1:
        loser = checking.pop()
        return {'winner': 'b' if loser == 'w' else 'w', 'reason': 'perpetual_check'}
    return {'winner': None, 'reason': 'perpetual_check' if checking else 'repetition'}


def outcome(start_fen: str, moves: list[str]) -> dict | None:
    """Return the first terminal result in a legal history (or None).

    The initial position is occurrence one. A capture resets the 120-ply
    counter; a pawn move does not. FEN counters do not substitute for history.
    Invalid moves before the terminal position raise ValueError.
    """
    position = Position.from_fen(start_fen)
    result = _terminal(position)
    if result:
        return result
    occurrences = {(position._board, position.side): [0]}
    checks = []
    quiet = 0
    for ply, move in enumerate(moves, 1):
        next_position = position.make(move)
        capture = position.piece_at(move[2:]) is not None
        quiet = 0 if capture else quiet + 1
        checks.append((position.side, next_position.in_check()))
        position = next_position
        result = _terminal(position)
        if result:
            return result
        key = (position._board, position.side)
        seen = occurrences.setdefault(key, [])
        seen.append(ply)
        if len(seen) >= 3:
            return _repetition(checks, seen[-3])
        if quiet >= 120:
            return {'winner': None, 'reason': 'move_limit'}
    return None
