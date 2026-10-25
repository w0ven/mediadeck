"""Bounded, unprivileged Rapfi (piskvork) client with a pure local fallback."""
from __future__ import annotations

import os
import random
import re
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

from app.modules import gomoku_engine as engine
from app.modules.board_engine_pool import engine_slot

LEVELS = ('easy', 'normal', 'hard')
TIMEOUT = 1.2
MEMORY_MB = 512
# No preexec_fn: robots run from worker threads. Limits are applied by a tiny
# isolated Python launcher *after* subprocess has dropped all root credentials.
_LAUNCH = '''import os,resource,sys
resource.setrlimit(resource.RLIMIT_AS, (512*1024*1024,)*2)
resource.setrlimit(resource.RLIMIT_CPU, (2,2))
resource.setrlimit(resource.RLIMIT_CORE, (0,0))
resource.setrlimit(resource.RLIMIT_FSIZE, (0,0))
resource.setrlimit(resource.RLIMIT_NOFILE, (32,32))
os.execv(sys.argv[1], [sys.argv[1]])
'''


def candidates(cells):
    if not cells:
        return [(7, 7)]
    return sorted({(x + dx, y + dy) for x, y in cells for dx in range(-2, 3)
                   for dy in range(-2, 3) if 0 <= x + dx < 15 and 0 <= y + dy < 15
                   and (x + dx, y + dy) not in cells})


def _shape(cells, point, side):
    """Favor open threes, then open twos; also recognize broken threes."""
    score = 0
    x, y = point
    for dx, dy in engine.DIRECTIONS:
        line = ''.join('X' if n == 0 or cells.get((x + n*dx, y + n*dy)) == side
                       else '.' if 0 <= x+n*dx < 15 and 0 <= y+n*dy < 15
                       and (x+n*dx, y+n*dy) not in cells else '#'
                       for n in range(-4, 5))
        score += sum(value for shape, value in (('.XXXX.', 10000), ('.XXX.', 1000),
                     ('.XX.X.', 800), ('.X.XX.', 800), ('.XX.', 80), ('.X.X.', 50))
                     if shape in line)
    return score


def fallback_move(moves, level='normal', rng=None):
    if level not in LEVELS:
        raise ValueError('机器人难度无效')
    rng = rng or random.SystemRandom()
    cells = engine.board(moves)
    if engine.outcome(moves):
        raise ValueError('对局已结束')
    points = candidates(cells)
    side = len(moves) % 2
    # Immediate tactics are never missed, including at the board edge.
    for color in (side, 1-side):
        wins = [p for p in points if engine.five(cells, p, color)]
        if wins:
            return list(rng.choice(wins))
    # Entry level intentionally ignores developing threats most of the time.
    if level == 'easy' and rng.random() < .8:
        return list(rng.choice(points))
    scored = [(2*_shape(cells, p, side) + _shape(cells, p, 1-side)
               - .1*((p[0]-7)**2 + (p[1]-7)**2), p) for p in points]
    best = max(v for v, _ in scored)
    return list(rng.choice([p for v, p in scored if v == best]))


def engine_move(moves, level, *, binary, directory):
    """Return a legal Rapfi move or raise; never silently claim engine success."""
    with engine_slot(TIMEOUT) as deadline:
        return _run_engine(moves, level, binary=binary, directory=directory, deadline=deadline)


def _run_engine(moves, level, *, binary, directory, deadline):
    if level not in LEVELS:
        raise ValueError('机器人难度无效')
    binary = str(Path(binary).resolve(strict=True))
    directory = str(Path(directory).resolve(strict=True))
    if not os.access(binary, os.X_OK):
        raise OSError('engine is not executable')
    identity = {}
    if os.geteuid() == 0:
        import grp
        import pwd
        identity = {'user': pwd.getpwnam('nobody').pw_uid,
                    'group': grp.getgrnam('nogroup').gr_gid, 'extra_groups': []}
    env = {'PATH': '/usr/bin:/bin', 'HOME': directory, 'LANG': 'C',
           'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1'}
    proc = subprocess.Popen([os.path.realpath(sys.executable), '-I', '-c', _LAUNCH, binary],
                            cwd=directory, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True, **identity)
    try:
        milliseconds = {'easy': 60, 'normal': 200, 'hard': 650}[level]
        lines = ['START 15', 'INFO rule 0', 'INFO thread_num 1', 'INFO max_memory 33554432',
                 'INFO pondering 0', 'INFO usedatabase 0',
                 f'INFO timeout_turn {milliseconds}', f'INFO time_left {milliseconds}', 'BOARD']
        side = len(moves) % 2
        lines += [f'{x},{y},{1 if i % 2 == side else 2}' for i, (x, y) in enumerate(moves)]
        lines += ['DONE', '']
        proc.stdin.write('\n'.join(lines).encode('ascii'))
        proc.stdin.flush()
        buf, total = b'', 0
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                left = deadline - time.monotonic()
                if left <= 0 or not selector.select(left):
                    raise TimeoutError('Rapfi timed out')
                chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    raise OSError('Rapfi exited without a move')
                total += len(chunk)
                if total > 65536:
                    raise OSError('Rapfi output limit')
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    match = re.fullmatch(rb'(\d{1,2}),(\d{1,2})\s*', line)
                    if match:
                        move = [int(n) for n in match.groups()]
                        engine.make(moves, move)
                        return move
    finally:
        # Kill the whole group even after success; do not leave pondering children.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate(timeout=.3)


def robot_move(moves, level='normal', *, binary=None, directory=None, rng=None):
    rng = rng or random.SystemRandom()
    if level not in LEVELS:
        raise ValueError('机器人难度无效')
    if engine.outcome(moves):
        raise ValueError('对局已结束')
    binary = binary if binary is not None else os.environ.get('MEDIADECK_RAPFI_BIN', '')
    directory = directory if directory is not None else os.environ.get('MEDIADECK_RAPFI_DIR', '')
    if binary and directory:
        try:
            move = engine_move(moves, level, binary=binary, directory=directory)
            if level != 'easy' or rng.random() >= .8:
                return move
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
            pass
    return fallback_move(moves, level, rng)
