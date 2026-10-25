"""Real Rapfi is optional; legality/fallback/resource safety always run."""
import os
import random
import sys
import time
from pathlib import Path

import pytest

from app.modules import gomoku_engine as engine
from app.modules import gomoku_robot as robot


@pytest.mark.parametrize('level', robot.LEVELS)
def test_fallback_legality_and_tactics_without_binary(level, monkeypatch):
    monkeypatch.delenv('MEDIADECK_RAPFI_BIN', raising=False)
    monkeypatch.delenv('MEDIADECK_RAPFI_DIR', raising=False)
    assert robot.robot_move([], level) == [7, 7]
    # Black can win immediately, and white must block it when it is their turn.
    moves = [[0, 0], [0, 7], [1, 0], [2, 7], [2, 0], [4, 7], [3, 0], [6, 7]]
    assert robot.robot_move(moves, level, binary='/absent', directory='/absent') == [4, 0]
    assert robot.robot_move(moves[:-1], level) == [4, 0]
    for seed in range(10):
        move = robot.robot_move([[7, 7], [8, 8], [8, 7]], level, rng=random.Random(seed))
        assert move in engine.legal_moves([[7, 7], [8, 8], [8, 7]])


def test_difficulties_and_open_three_heuristic():
    moves = [[5, 7], [0, 0], [6, 7], [2, 0]]
    choices = [robot.fallback_move(moves, 'easy', random.Random(i)) for i in range(30)]
    hard = robot.fallback_move(moves, 'hard', random.Random(1))
    assert hard in ([4, 7], [7, 7])
    assert len({tuple(m) for m in choices}) > 10
    assert any(m not in ([4, 7], [7, 7]) for m in choices)
    with pytest.raises(ValueError):
        robot.robot_move([], 'expert')


def executable(tmp_path, content):
    # Pytest parents are private by default; the root service must run engines
    # as nobody and still be able to traverse this *test-only* directory.
    for parent in [tmp_path, *tmp_path.parents]:
        if parent == Path('/tmp') or parent == Path('/'):
            break
        parent.chmod(0o755)
    binary = tmp_path/'pbrain-test'
    binary.write_text('#!' + os.path.realpath(sys.executable) + '\n' + content)
    binary.chmod(0o755)
    return str(binary)


def test_engine_launcher_drops_root_and_enforces_limits(tmp_path):
    expected = 65534 if os.geteuid() == 0 else os.geteuid()
    binary = executable(tmp_path, f'''import os,resource,sys
assert os.geteuid() == {expected}
assert resource.getrlimit(resource.RLIMIT_AS)[0] == 512*1024*1024
assert resource.getrlimit(resource.RLIMIT_CPU)[0] == 2
assert resource.getrlimit(resource.RLIMIT_CORE)[0] == 0
assert resource.getrlimit(resource.RLIMIT_FSIZE)[0] == 0
assert os.environ['OMP_NUM_THREADS'] == '1'
if {os.geteuid()} == 0:
    assert os.getegid() == 65534 and not os.getgroups()
lines=[]
for line in sys.stdin:
    lines.append(line.strip())
    if line.strip() == 'DONE': break
assert 'INFO thread_num 1' in lines and 'INFO rule 0' in lines
assert 'INFO max_memory 33554432' in lines
assert 'INFO pondering 0' in lines and 'INFO usedatabase 0' in lines
print('7,7', flush=True)
''')
    assert robot.engine_move([], 'normal', binary=binary, directory=str(tmp_path)) == [7, 7]


@pytest.mark.parametrize('script', ["import time; time.sleep(60)",
    "print('x'*100000, flush=True)", "print('99,99', flush=True)", "raise SystemExit(1)"])
def test_timeout_bad_output_and_failure_kill_and_fall_back(tmp_path, script, monkeypatch):
    binary = executable(tmp_path, script)
    spawned = []
    popen = robot.subprocess.Popen
    def capture(*a, **kw):
        proc = popen(*a, **kw)
        spawned.append(proc)
        return proc
    monkeypatch.setattr(robot.subprocess, 'Popen', capture)
    started = time.monotonic()
    assert robot.robot_move([], 'normal', binary=binary, directory=str(tmp_path)) == [7, 7]
    assert time.monotonic()-started < 1.8
    assert spawned and all(p.poll() is not None for p in spawned)


@pytest.mark.parametrize('level', robot.LEVELS)
@pytest.mark.parametrize('moves', [[], [[7, 7], [8, 7], [7, 8]]])
def test_real_rapfi_all_levels_legal(level, moves):
    binary, directory = os.getenv('MEDIADECK_RAPFI_BIN'), os.getenv('MEDIADECK_RAPFI_DIR')
    if not binary or not directory or not Path(binary).is_file() or not Path(directory).is_dir():
        pytest.skip('Rapfi not configured')
    # Direct engine call: a local fallback cannot accidentally make this test pass.
    # The 250615 binary can abort while constructing its search thread before
    # its mutex is initialized. Production falls back; this smoke test retries
    # startup only, so success still proves a *real* engine move at each level.
    for attempt in range(3):
        started = time.monotonic()
        try:
            move = robot.engine_move(moves, level, binary=binary, directory=directory)
            break
        except OSError:
            if attempt == 2:
                raise
    assert move in engine.legal_moves(moves)
    assert time.monotonic()-started < 1.6
