"""Cross-game process counting, bounded queueing and legal deadline fallback."""
import os
import random
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.modules import board_engine_pool as limits
from app.modules import gomoku_engine, gomoku_robot, xiangqi_robot
from app.modules.game_center_board import BoardGame, BoardRobots
from app.modules.xiangqi_engine import INITIAL_FEN, Position


@pytest.fixture
def processes(monkeypatch):
    """Real child processes; only replace their chess protocol implementation."""
    popen = subprocess.Popen
    lock = threading.Lock()
    records, peak = [], [0]
    def spawn(args, **kwargs):
        kind = 'gomoku' if '-I' in args else 'xiangqi'
        script = '''import sys,time
kind=sys.argv[1]
for raw in sys.stdin:
    line=raw.strip()
    if line=='uci':
        print('option name Threads type spin default 1 min 1 max 4')
        print('option name Hash type spin default 16 min 1 max 64')
        print('uciok',flush=True)
    elif line=='isready': print('readyok',flush=True)
    elif line.startswith('go ') or line=='DONE':
        time.sleep(float(sys.argv[2]))
        print('7,7' if kind=='gomoku' else 'bestmove a0a1',flush=True)
    elif line=='quit': break
'''
        with lock:
            process = popen([os.path.realpath(sys.executable), '-I', '-c', script,
                             kind, str(spawn.delay)], **kwargs)
            records.append((kind, process, time.monotonic()))
            peak[0] = max(peak[0], sum(p.poll() is None for _, p, _ in records))
        return process
    spawn.delay = .12
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    yield records, peak, spawn
    for _, process, _ in records:
        if process.poll() is None:
            process.kill()
        process.wait()


def move(kind):
    if kind == 'gomoku':
        return gomoku_robot.robot_move([], 'hard', binary=os.path.realpath(sys.executable), directory='/')
    return xiangqi_robot.robot_move(INITIAL_FEN, 'hard', binary='/test-engine', nnue=None,
                                   rng=random.Random(2))


def legal(kind, chosen):
    return chosen in (gomoku_engine.legal_moves([]) if kind == 'gomoku' else
                      Position.from_fen(INITIAL_FEN).legal_moves())


def test_mixed_engines_never_exceed_two_live_processes_and_waiters_run(processes, monkeypatch):
    records, peak, _ = processes
    assert limits.MAX_ENGINE_PROCESSES == 2
    barrier = threading.Barrier(6)
    kinds = ['gomoku', 'xiangqi'] * 3
    def no_fallback(*a, **kw):
        pytest.fail('queue should drain to a real process before its deadline')
    monkeypatch.setattr(gomoku_robot, 'fallback_move', no_fallback)
    monkeypatch.setattr(xiangqi_robot, '_fallback', no_fallback)
    def call(kind):
        barrier.wait()
        return move(kind)
    with ThreadPoolExecutor(max_workers=6) as executor:
        choices = list(executor.map(call, kinds))
    assert all(legal(kind, chosen) for kind, chosen in zip(kinds, choices))
    assert len(records) == 6 and {k for k, _, _ in records} == {'xiangqi', 'gomoku'}
    assert peak[0] == 2
    assert all(p.poll() is not None for _, p, _ in records)
    # The third engine really queued, rather than being silently dropped.
    assert records[2][2] - records[0][2] >= .10


def test_queue_deadline_returns_legal_fallback_without_spawning(monkeypatch):
    def no_process(*a, **kw):
        pytest.fail('expired waiter must not start an engine')
    monkeypatch.setattr(subprocess, 'Popen', no_process)
    def call(kind):
        started = time.monotonic()
        with limits.job_budget(started + .10):
            chosen = move(kind)
        return chosen, time.monotonic()-started
    with limits.engine_slot(2), limits.engine_slot(2), ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(call, ('gomoku', 'xiangqi')))
    for kind, (chosen, elapsed) in zip(('gomoku', 'xiangqi'), results):
        assert legal(kind, chosen)
        assert .08 <= elapsed < .6


@pytest.mark.parametrize('kind', ['gomoku', 'xiangqi'])
def test_queue_and_thinking_share_one_deadline_and_slot_is_released(processes, kind):
    records, peak, spawn = processes
    spawn.delay = .5
    started = time.monotonic()
    def call():
        with limits.job_budget(started + .24):
            return move(kind)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with limits.engine_slot(2), limits.engine_slot(2):
            future = executor.submit(call)
            time.sleep(.10)
            assert not records  # Both process slots are still held.
        chosen = future.result(timeout=1)
    elapsed = time.monotonic()-started
    assert legal(kind, chosen) and .20 <= elapsed < .65
    assert peak[0] == 1 and len(records) == 1
    assert records[0][1].returncode == -signal.SIGKILL
    # Both permits are available after timeout + reap, not leaked by exceptions.
    with limits.engine_slot(.05), limits.engine_slot(.05):
        pass


@pytest.mark.parametrize('kind', ['gomoku', 'xiangqi'])
def test_executor_queue_expiry_skips_engine_and_tick_returns_legal_fallback(monkeypatch, kind):
    robots, game = BoardRobots(), BoardGame(kind)
    entered, release, lock = threading.Event(), threading.Event(), threading.Lock()
    calls = []
    def blocked(hand, level):
        with lock:
            calls.append(hand['number'])
            if len(calls) == 2:
                entered.set()
        release.wait(2)
        return game.fallback_move(hand, level)
    monkeypatch.setattr(game, 'robot_move', blocked)
    monkeypatch.setattr(robots, 'MAX_WAIT', .10)
    tables = []
    for n in range(3):
        tables.append({'id': str(n), 'rules': {'robot_level': 'hard'}, 'hand': {
            'number': n, 'position_no': 0, 'current_player': 'bot_1',
            'users': {'bot_1': {'bot': True}}, 'turn_started_at': 1000,
            'moves': [], 'fen': INITIAL_FEN}})
    try:
        for table in tables:
            robots.schedule(game, table)
        assert entered.wait(1)
        time.sleep(.12)
        # Tick itself never waits on the worker or on a process permit.
        started = time.monotonic()
        chosen = robots.move(game, tables[2], 1001.5)
        assert time.monotonic()-started < .2 and legal(kind, chosen)
        assert robots.jobs[robots.key(tables[2])][0].cancelled()
        release.set()
        robots.pool.shutdown(wait=True)
        assert calls == [0, 1]
    finally:
        release.set()
        robots.pool.shutdown(wait=True)
