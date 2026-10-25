"""Real UCI subprocesses exercise deadlines, SIGKILL and effective credentials.

Pikafish-only tests skip when the env-configured (or development) files are
absent. Fake engines are Python -c children, not root-readable script files.
"""
from __future__ import annotations

import os
import random
import signal
import sys
import time
from pathlib import Path

import pytest

from app.modules import xiangqi_robot as robot
from app.modules.xiangqi_engine import INITIAL_FEN, Position

_OPTIONS = [
    'option name Threads type spin default 1 min 1 max 1024',
    'option name Hash type spin default 16 min 1 max 33554432',
    'option name EvalFile type string default pikafish.nnue',
]


@pytest.fixture
def engine_paths():
    binary = os.environ.get('MEDIADECK_PIKAFISH_BIN', '')
    nnue = os.environ.get('MEDIADECK_PIKAFISH_NNUE', '')
    if not Path(binary).is_file() or not Path(nnue).is_file():
        pytest.skip('Pikafish executable/network not available')
    return binary, nnue


def fake_engine(monkeypatch, *, mode='ok', bestmove='a0a1', options=None):
    options = _OPTIONS if options is None else options
    script = f'''
import sys, time
mode = {mode!r}
for raw in sys.stdin:
    command = raw.strip()
    if mode == 'crash':
        sys.exit(7)
    if command == 'uci':
        if mode == 'no_uci':
            print('unterminated', end='', flush=True)
            time.sleep(60)
        if mode == 'flood':
            print('x' * 1100000, flush=True)
            time.sleep(60)
        for option in {options!r}:
            print(option, flush=True)
        if mode == 'slow':
            time.sleep(0.04)
        print('uciok', flush=True)
    elif command == 'isready':
        if mode == 'no_ready':
            time.sleep(60)
        if mode == 'slow':
            time.sleep(0.04)
        print('readyok', flush=True)
    elif command.startswith('go '):
        if mode == 'no_best':
            time.sleep(60)
        if mode == 'slow':
            time.sleep(0.04)
        print('bestmove {bestmove}', flush=True)
    elif command == 'quit':
        if mode == 'no_quit':
            time.sleep(60)
        sys.exit(0)
'''
    original_popen, original_send = robot.subprocess.Popen, robot._UCI.send
    processes, identities, commands, arguments = [], [], [], []

    def spawn(args, **kwargs):
        arguments.append(kwargs)
        process = original_popen([os.path.realpath(sys.executable), '-c', script], **kwargs)
        processes.append(process)
        if Path(f'/proc/{process.pid}/status').is_file():
            identities.append(Path(f'/proc/{process.pid}/status').read_text())
        return process

    def send(self, command):
        commands.append(command)
        return original_send(self, command)

    monkeypatch.setattr(robot.subprocess, 'Popen', spawn)
    monkeypatch.setattr(robot._UCI, 'send', send)
    return processes, identities, commands, arguments


def forbidden_fallback(*args):
    pytest.fail('expected a real engine response, not fallback')


@pytest.mark.parametrize('level', ('easy', 'normal', 'hard'))
def test_real_pikafish_all_levels_return_legal_move(engine_paths, monkeypatch, level):
    binary, nnue = engine_paths
    monkeypatch.setattr(robot, '_fallback', forbidden_fallback)
    move = robot.robot_move(INITIAL_FEN, level, binary=binary, nnue=nnue, rng=random.Random(2))
    assert move in Position.from_fen(INITIAL_FEN).legal_moves()


@pytest.mark.parametrize('level', ('easy', 'normal', 'hard'))
def test_uci_handshake_resource_limits_and_difficulty(monkeypatch, level):
    processes, _, commands, _ = fake_engine(monkeypatch)
    monkeypatch.setattr(robot, '_fallback', forbidden_fallback)
    move = robot.robot_move(INITIAL_FEN, level, binary='/fake', nnue='/network with spaces.nnue',
                            rng=random.Random(2))
    assert move == 'a0a1'
    assert commands[0] == 'uci'
    assert 'setoption name Threads value 1' in commands
    assert 'setoption name Hash value 16' in commands
    assert 'setoption name EvalFile value /network with spaces.nnue' in commands
    assert commands.index('isready') < commands.index('position fen ' + INITIAL_FEN + ' - - 0 1')
    assert commands[-1] == 'quit'
    assert commands[-2] == {
        'easy': 'go movetime 20 depth 2', 'normal': 'go movetime 150 depth 6',
        'hard': 'go movetime 700'}[level]
    assert all(process.poll() == 0 for process in processes)
    assert all(process.stdin.closed and process.stdout.closed for process in processes)


@pytest.mark.parametrize('level,skill,elo', [('easy', 0, 1000), ('normal', 8, 1800),
                                           ('hard', 20, None)])
def test_optional_strength_options_are_discovered_and_clamped(monkeypatch, level, skill, elo):
    options = _OPTIONS + [
        'option name Skill Level type spin default 20 min 0 max 20',
        'option name UCI_LimitStrength type check default false',
        'option name UCI_Elo type spin default 3000 min 1000 max 3000',
    ]
    _, _, commands, _ = fake_engine(monkeypatch, options=options)
    monkeypatch.setattr(robot, '_fallback', forbidden_fallback)
    robot.robot_move(INITIAL_FEN, level, binary='/fake', nnue=None, rng=random.Random(2))
    assert f'setoption name Skill Level value {skill}' in commands
    assert 'setoption name UCI_LimitStrength value ' + ('true' if elo else 'false') in commands
    if elo:
        assert f'setoption name UCI_Elo value {elo}' in commands
    else:
        assert not any('name UCI_Elo' in command for command in commands)


def test_easy_random_branch_is_uniform_over_all_legal_moves_without_starting_engine(monkeypatch):
    offered = []

    class RNG:
        def random(self):
            return 0.84

        def choice(self, options):
            offered.extend(options)
            return options[-1]

    def forbidden_engine(*args):
        pytest.fail('easy random move should not start an engine')

    monkeypatch.setattr(robot, '_engine_move', forbidden_engine)
    move = robot.robot_move(INITIAL_FEN, 'easy', binary='/fake', nnue=None, rng=RNG())
    assert offered == Position.from_fen(INITIAL_FEN).legal_moves()
    assert move == offered[-1]


@pytest.mark.parametrize('binary', (None, '/no-such-pikafish'))
@pytest.mark.parametrize('level', ('easy', 'normal', 'hard'))
def test_missing_engine_fallback_is_legal(binary, level):
    position = Position.from_fen(INITIAL_FEN)
    move = robot.robot_move(INITIAL_FEN, level, binary=binary, nnue=None, rng=random.Random(2))
    assert move in position.legal_moves()
    assert move in ('b2b9', 'h2h9')


@pytest.mark.parametrize('fen,expected', [
    ('4k4/9/9/9/9/r8/9/9/9/R2K5 w', 'a0a4'),
    ('4k4/9/9/9/9/9/9/9/9/R2K5 w', 'a0a9'),
])
def test_fallback_prefers_capture_then_check(fen, expected):
    assert robot.robot_move(fen, 'normal', binary=None, nnue=None, rng=random.Random(4)) == expected


def test_start_failure_does_not_escape(monkeypatch):
    def fail(*args, **kwargs):
        raise PermissionError('cannot exec')

    monkeypatch.setattr(robot.subprocess, 'Popen', fail)
    move = robot.robot_move(INITIAL_FEN, 'hard', binary='/fake', nnue=None)
    assert move in Position.from_fen(INITIAL_FEN).legal_moves()


@pytest.mark.parametrize('mode', ('no_uci', 'no_ready', 'no_best', 'no_quit', 'slow'))
def test_total_deadline_kills_and_reaps_real_process(monkeypatch, mode):
    processes, _, _, _ = fake_engine(monkeypatch, mode=mode)
    monkeypatch.setattr(robot, '_TOTAL_TIMEOUT', 0.10)
    start = time.monotonic()
    move = robot.robot_move(INITIAL_FEN, 'hard', binary='/fake', nnue=None)
    elapsed = time.monotonic() - start
    assert move in Position.from_fen(INITIAL_FEN).legal_moves()
    assert elapsed < 0.8
    assert processes[0].returncode == -signal.SIGKILL
    assert processes[0].stdin.closed and processes[0].stdout.closed


@pytest.mark.parametrize('mode', ('crash', 'flood'))
def test_broken_protocol_and_output_limit_fall_back(monkeypatch, mode):
    processes, _, _, _ = fake_engine(monkeypatch, mode=mode)
    move = robot.robot_move(INITIAL_FEN, 'normal', binary='/fake', nnue=None)
    assert move in Position.from_fen(INITIAL_FEN).legal_moves()
    assert processes[0].poll() is not None


@pytest.mark.parametrize('bestmove', ('(none)', 'a9a8', '0000', 'garbage'))
def test_engine_moves_are_validated_locally(monkeypatch, bestmove):
    fake_engine(monkeypatch, bestmove=bestmove)
    move = robot.robot_move(INITIAL_FEN, 'hard', binary='/fake', nnue=None)
    assert move in Position.from_fen(INITIAL_FEN).legal_moves()


@pytest.mark.parametrize('options', [
    [], _OPTIONS[1:],
    ['option name Threads type spin default 2 min 2 max 4', _OPTIONS[1]],
    [_OPTIONS[0], 'option name Hash type spin default 32 min 32 max 64'],
])
def test_unenforceable_resource_limits_refuse_search(monkeypatch, options):
    _, _, commands, _ = fake_engine(monkeypatch, options=options)
    move = robot.robot_move(INITIAL_FEN, 'hard', binary='/fake', nnue=None)
    assert move in Position.from_fen(INITIAL_FEN).legal_moves()
    assert not any(command.startswith('go ') for command in commands)


def test_nnue_path_cannot_inject_uci_commands(monkeypatch):
    _, _, commands, _ = fake_engine(monkeypatch)
    move = robot.robot_move(INITIAL_FEN, 'hard', binary='/fake', nnue='/net\ngo infinite')
    assert move in Position.from_fen(INITIAL_FEN).legal_moves()
    assert not any(command.startswith('go ') for command in commands)


@pytest.mark.skipif(os.geteuid() != 0, reason='requires root to verify actual privilege drop')
def test_root_child_really_runs_as_nobody_nogroup_without_supplementary_groups(monkeypatch):
    import grp
    import pwd

    processes, identities, _, arguments = fake_engine(monkeypatch)
    monkeypatch.setattr(robot, '_fallback', forbidden_fallback)
    assert robot.robot_move(INITIAL_FEN, 'hard', binary='/fake', nnue=None) == 'a0a1'
    uid, gid = pwd.getpwnam('nobody').pw_uid, grp.getgrnam('nogroup').gr_gid
    status = dict(line.split(':', 1) for line in identities[0].splitlines() if ':' in line)
    assert list(map(int, status['Uid'].split())) == [uid] * 4
    assert list(map(int, status['Gid'].split())) == [gid] * 4
    assert status['Groups'].strip() == ''
    assert arguments[0]['user'] == 'nobody'
    assert arguments[0]['group'] == 'nogroup'
    assert arguments[0]['extra_groups'] == ()
    assert processes[0].poll() is not None


def test_nonroot_does_not_try_to_change_credentials(monkeypatch):
    arguments = []
    monkeypatch.setattr(robot.os, 'geteuid', lambda: 1000)

    def refuse_start(args, **kwargs):
        arguments.append(kwargs)
        raise PermissionError('not starting a root child in this test')

    monkeypatch.setattr(robot.subprocess, 'Popen', refuse_start)
    robot.robot_move(INITIAL_FEN, 'normal', binary='/fake', nnue=None)
    assert not {'user', 'group', 'extra_groups'} & arguments[0].keys()


def test_bad_input_and_terminal_position_are_not_engine_failures():
    with pytest.raises(ValueError):
        robot.robot_move(INITIAL_FEN, 'unknown', binary=None, nnue=None)
    with pytest.raises(ValueError):
        robot.robot_move('invalid', 'hard', binary=None, nnue=None)
    with pytest.raises(ValueError, match='终局'):
        robot.robot_move('3Rk4/3R5/9/9/9/9/9/9/9/5K3 b', 'hard', binary=None, nnue=None)
