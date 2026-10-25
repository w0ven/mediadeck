"""One bounded, unprivileged Pikafish process per practice move; no dependencies.

Easy plays uniformly random legal moves 85% of the time. Otherwise it searches
for 20 ms / depth 2; normal uses 150 ms / depth 6; hard uses 700 ms without a
depth cap. Advertised strength options are used when available. Engine failure
falls back to captures/checks, then randomness. No process or hash survives a
call. Call only with a valid, nonterminal position; bad inputs/no legal move
raise ValueError, but engine startup/protocol/timeout failures never escape.
"""
from __future__ import annotations

import os
import random
import re
import selectors
import subprocess
import time

from app.modules.board_engine_pool import engine_slot
from app.modules.xiangqi_engine import Position

_TOTAL_TIMEOUT = 3.0
_LEVELS = {'easy': (20, 2, 0), 'normal': (150, 6, 8), 'hard': (700, None, 20)}
_VALUES = {'K': 10000, 'R': 900, 'C': 450, 'N': 400, 'B': 200, 'A': 200, 'P': 100}


class _UCI:
    def __init__(self, process, selector, deadline: float):
        self.process = process
        self.selector = selector
        self.deadline = deadline
        self.buffer = b''
        self.received = 0
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)

    def send(self, command: str) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError('UCI deadline')
        self.process.stdin.write((command + '\n').encode('utf-8'))
        self.process.stdin.flush()

    def line(self) -> str:
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('UCI deadline')
            if b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                return line.decode('utf-8', errors='replace').strip()
            if not self.selector.select(remaining):
                raise TimeoutError('UCI deadline')
            data = os.read(self.process.stdout.fileno(), 65536)
            if not data:
                raise RuntimeError('UCI closed stdout')
            self.received += len(data)
            if self.received > 1024 * 1024:
                raise RuntimeError('UCI excessive output')
            self.buffer += data

    def until(self, expected: str) -> None:
        while self.line() != expected:
            pass


def _spin(option: str, desired: int) -> int:
    bounds = re.search(r'\bmin (-?\d+) max (-?\d+)\b', option)
    if bounds is None:
        raise ValueError('UCI spin bounds missing')
    minimum, maximum = map(int, bounds.groups())
    if minimum > maximum:
        raise ValueError('UCI invalid spin bounds')
    return max(minimum, min(maximum, desired))


def _configure(uci: _UCI, options: dict[str, str], level: str, nnue: str | None):
    def set_option(name, value):
        uci.send(f'setoption name {name} value {value}')

    if ('Threads' not in options or 'Hash' not in options
            or _spin(options['Threads'], 1) != 1
            or not 1 <= _spin(options['Hash'], 16) <= 16):
        raise ValueError('UCI cannot enforce resource limits')
    set_option('Threads', 1)
    set_option('Hash', _spin(options['Hash'], 16))
    if nnue:
        if 'EvalFile' not in options or '\n' in nnue or '\r' in nnue:
            raise ValueError('UCI invalid EvalFile')
        set_option('EvalFile', os.path.abspath(nnue))
    if 'Skill Level' in options:
        set_option('Skill Level', _spin(options['Skill Level'], _LEVELS[level][2]))
    if 'UCI_LimitStrength' in options:
        limited = level != 'hard' and 'UCI_Elo' in options
        if limited:
            elo = 800 if level == 'easy' else 1800
            set_option('UCI_Elo', _spin(options['UCI_Elo'], elo))
        set_option('UCI_LimitStrength', 'true' if limited else 'false')


def _engine_move(fen: str, level: str, binary: str, nnue: str | None) -> str:
    with engine_slot(_TOTAL_TIMEOUT) as deadline:
        return _run_engine(fen, level, binary, nnue, deadline)


def _run_engine(fen: str, level: str, binary: str, nnue: str | None, deadline: float) -> str:
    identity = {}
    if os.geteuid() == 0:
        identity = {'user': 'nobody', 'group': 'nogroup', 'extra_groups': ()}
    process = subprocess.Popen(
        [os.path.abspath(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, cwd='/', **identity,
    )
    try:
        with selectors.DefaultSelector() as selector:
            uci = _UCI(process, selector, deadline)
            uci.send('uci')
            options = {}
            while True:
                line = uci.line()
                if line == 'uciok':
                    break
                match = re.fullmatch(r'option name (.+?) type (.+)', line)
                if match:
                    options[match[1]] = match[2]
            _configure(uci, options, level, nnue)
            uci.send('isready')
            uci.until('readyok')
            uci.send('position fen ' + fen + ' - - 0 1')
            milliseconds, depth, _ = _LEVELS[level]
            command = f'go movetime {milliseconds}'
            if depth is not None:
                command += f' depth {depth}'
            uci.send(command)
            while True:
                line = uci.line()
                if line.startswith('bestmove '):
                    move = line.split()[1]
                    uci.send('quit')
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                    return move
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdin.close()
        process.stdout.close()


def _fallback(position: Position, moves: list[str], rng) -> str:
    choices, best = [], -1
    for move in moves:
        victim = position.piece_at(move[2:])
        score = _VALUES.get(victim.upper(), 0) if victim else 0
        if position.make(move).in_check():
            score += 60
        if score > best:
            choices, best = [move], score
        elif score == best:
            choices.append(move)
    return rng.choice(choices)


def robot_move(fen: str, level: str, *, binary: str | None, nnue: str | None, rng=None) -> str:
    """Return a legal move; binary/nnue are local paths, never shell commands."""
    if level not in _LEVELS:
        raise ValueError('难度无效')
    position = Position.from_fen(fen)
    moves = position.legal_moves()
    if not moves:
        raise ValueError('终局没有合法着法')
    rng = rng if rng is not None else random.SystemRandom()
    if level == 'easy' and rng.random() < 0.85:
        return rng.choice(moves)
    if binary:
        try:
            move = _engine_move(position.fen(), level, binary, nnue)
            if move in moves:
                return move
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            pass
    return _fallback(position, moves, rng)
