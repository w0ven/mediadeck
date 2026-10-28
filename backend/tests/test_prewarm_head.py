"""Prewarm tests use synthetic logs, a local SQLite library and temporary mounts."""
from __future__ import annotations

import builtins
import importlib.util
import json
import multiprocessing
import os
import sqlite3
import time
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest

TOOL_PATH = Path(__file__).resolve().parents[1] / 'tools' / 'prewarm_head.py'
SIZE_ENV = {
    'MEDIADECK_PREWARM_DISK_LOW_WATER_GB': ('DISK_LOW_WATER_BYTES', 320, 1024 ** 3),
    'MEDIADECK_PREWARM_FILE_BUDGET_MB': ('PER_FILE_EXPANSION_BUDGET', 320, 1024 ** 2),
    'MEDIADECK_PREWARM_WINDOW_MB': ('WINDOW_BYTES', 1920, 1024 ** 2),
}
LOG_PREFIX = '2026-01-01 00:00:00.000 Info MediaInfoService-TEST:00000001: '


@pytest.fixture
def load_prewarm(tmp_path, monkeypatch):
    def load(overrides=None):
        # The tool reads configuration at import time. Never inherit host paths
        # or tuning, even when multiple module instances are loaded in one test.
        for name in SIZE_ENV:
            monkeypatch.delenv(name, raising=False)
        env = {
            'MEDIADECK_PREWARM_MOUNTS': json.dumps({
                str(tmp_path / 'mount'): str(tmp_path / 'cache'),
            }),
            'MEDIADECK_EMBY_PATH_PREFIXES': '/media/',
            'MEDIADECK_EMBY_LOG': str(tmp_path / 'emby.log'),
            'MEDIADECK_DISPATCH_LOG': str(tmp_path / 'dispatch.log'),
            'MEDIADECK_EMBY_LIBRARY_DB': str(tmp_path / 'library.db'),
            **(overrides or {}),
        }
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        spec = importlib.util.spec_from_file_location('prewarm_head_test', TOOL_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return load


@pytest.fixture
def prewarm(load_prewarm):
    return load_prewarm()


@pytest.fixture
def watcher(prewarm, tmp_path):
    settings = tmp_path / 'settings.json'
    settings.write_text(json.dumps({'emby': {
        'url': 'http://emby.invalid', 'api_key': 'test-token',
    }}))
    return prewarm.PrewarmWatcher(settings)


def request_line(path='/emby/Items/42/PlaybackInfo?AutoOpenLiveStream=false'):
    return LOG_PREFIX + f'http/1.1 POST http://\u200c\u200demby.invalid\u200c:8096{path}\n'


def test_size_defaults(prewarm):
    for attr, default, unit in SIZE_ENV.values():
        assert getattr(prewarm, attr) == default * unit
    assert prewarm.MAX_CONCURRENT == 2
    assert prewarm.TOTAL_RESERVATION_LIMIT == 640 * 1024 ** 2
    assert prewarm.HEAD_BYTES == 32 * 1024 ** 2
    assert prewarm.TAIL_BYTES == 2 * 1024 ** 2


def test_size_overrides(load_prewarm):
    module = load_prewarm({
        'MEDIADECK_PREWARM_DISK_LOW_WATER_GB': '240.5',
        'MEDIADECK_PREWARM_FILE_BUDGET_MB': '96',
        'MEDIADECK_PREWARM_WINDOW_MB': '576',
    })
    assert module.DISK_LOW_WATER_BYTES == int(240.5 * 1024 ** 3)
    assert module.PER_FILE_EXPANSION_BUDGET == 96 * 1024 ** 2
    assert module.TOTAL_RESERVATION_LIMIT == module.MAX_CONCURRENT * 96 * 1024 ** 2
    assert module.WINDOW_BYTES == 576 * 1024 ** 2


@pytest.mark.parametrize('name', SIZE_ENV)
@pytest.mark.parametrize('value', ['', '0', '-1', 'bad', 'nan', 'inf', '-inf', '1e999', '1e-99'])
def test_invalid_sizes_fail_at_import(load_prewarm, name, value):
    with pytest.raises(ValueError, match=name + ' must be a positive finite number'):
        load_prewarm({name: value})


@pytest.mark.parametrize('path', [
    '/emby/Items/42/PlaybackInfo?AutoOpenLiveStream=false',
    '/Items/42/PlaybackInfo?UserId=test',
    '/Items/42/PlaybackInfo',
    '/emby/Items/42/PlaybackInfo. Source Ip: \u200cclient.invalid\u200d, UserAgent: test',
])
def test_request_triggers_immediately(watcher, path):
    watcher.on_log_line(request_line(path))
    assert watcher.stats['playback_seen'] == 1
    assert watcher._resolver_in == [('42', 'emby_log')]


@pytest.mark.parametrize('line', [
    LOG_PREFIX + 'http/1.1 Response 200 to \u200cclient.invalid\u200d. Time: 2ms. '
    'POST http://\u200cemby.invalid\u200d:8096/emby/Items/42/PlaybackInfo?UserId=test',
    LOG_PREFIX + 'GetPostedPlaybackInfo request. IsPlayback: true',
    LOG_PREFIX + 'GetPostedPlaybackInfo request. IsPlayback: false',
    LOG_PREFIX + 'http/1.1 GET http://emby.invalid/Items/42/PlaybackInfo',
    LOG_PREFIX + 'http/1.1 POST http://emby.invalid/Items/42/PlaybackInfoOther',
    LOG_PREFIX + 'http/1.1 POST http://emby.invalid/Items/42/Refresh',
    'unrelated log line',
])
def test_responses_and_other_lines_do_not_trigger(watcher, line):
    watcher.on_log_line(line)
    assert watcher.stats['playback_seen'] == 0
    assert watcher._resolver_in == []


def test_request_resolves_local_db_and_dispatch_deduplicates(prewarm, watcher, monkeypatch):
    path = '/media/example/sample.bin'
    with sqlite3.connect(watcher.library_db) as db:
        db.execute('CREATE TABLE MediaItems (Id INTEGER PRIMARY KEY, Path TEXT)')
        db.execute('INSERT INTO MediaItems VALUES (?, ?)', (42, path))

    def no_api(item):
        pytest.fail('local DB hit must not call API')

    monkeypatch.setattr(watcher, '_resolve_api', no_api)
    resolve_local = watcher._resolve_local
    calls = []

    def resolve(item):
        calls.append(item)
        if len(calls) == 2:
            watcher._stop.set()
        return resolve_local(item)

    monkeypatch.setattr(watcher, '_resolve_local', resolve)
    # Any mount probing in the watcher would violate its safety boundary.
    def no_mount_probe(*args, **kwargs):
        pytest.fail('watcher must only map mount paths lexically')

    monkeypatch.setattr(prewarm.os.path, 'realpath', no_mount_probe)
    monkeypatch.setattr(prewarm.os.path, 'isfile', no_mount_probe)
    watcher.on_log_line(request_line())
    watcher.on_dispatch_line(json.dumps({
        'status': 302, 'method': 'GET', 'path': '/emby/videos/42/original.mkv',
    }))
    watcher._resolver_loop()
    watcher._drain_resolver()
    assert calls == ['42', '42']
    assert watcher.stats['issuance_seen'] == 1
    assert watcher.stats['resolved'] == 2
    assert watcher.stats['triggered'] == 1
    assert watcher.stats['skipped_dedup'] == 1
    assert watcher.queue[0][1] == [str(Path(prewarm.MEDIA_MOUNTS[0]) / 'example/sample.bin')]
    # Local resolution leaves the fixture's library row unchanged.
    with sqlite3.connect('file:' + watcher.library_db + '?mode=ro', uri=True) as db:
        assert db.execute('SELECT Path FROM MediaItems WHERE Id=42').fetchone() == (path,)


def test_request_keeps_api_fallback_and_backoff(prewarm, watcher, monkeypatch):
    clock = [100.0]
    attempts = []
    monkeypatch.setattr(prewarm.time, 'monotonic', lambda: clock[0])

    def unavailable(req, timeout):
        attempts.append(req.full_url)
        raise OSError('synthetic API outage')

    monkeypatch.setattr(prewarm.urllib.request, 'urlopen', unavailable)
    for expected_backoff in (2, 4, 8, 16, 32, 60, 60):
        assert watcher._resolve_api('42') is None
        assert watcher._api_backoff_until == clock[0] + expected_backoff
        assert watcher._resolve_api('42') is None  # no HTTP during backoff
        clock[0] += expected_backoff
    assert len(attempts) == 7
    assert watcher.stats['skipped_backoff'] == 7

    def fallback(item):
        watcher._stop.set()
        return '/media/sample.bin'

    monkeypatch.setattr(watcher, '_resolve_api', fallback)
    watcher.on_log_line(request_line())
    watcher._resolver_loop()  # missing local DB falls back for request triggers
    watcher._drain_resolver()
    assert watcher.stats['triggered'] == 1


def test_resolver_input_stays_bounded(prewarm, watcher):
    for item in range(prewarm.RESOLVER_QUEUE_MAX + 10):
        watcher.on_log_line(request_line(f'/Items/{item}/PlaybackInfo'))
    assert len(watcher._resolver_in) == prewarm.RESOLVER_QUEUE_MAX
    assert watcher._resolver_in[0] == ('10', 'emby_log')


def test_disk_blocking_logs_are_bounded(prewarm, watcher, tmp_path, monkeypatch, capsys):
    cache = tmp_path / 'cache'
    cache.mkdir()
    clock = [0.0]
    free = [prewarm.DISK_LOW_WATER_BYTES - 1]
    monkeypatch.setattr(prewarm.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(prewarm.shutil, 'disk_usage', lambda path: SimpleNamespace(free=free[0]))
    for _ in range(10_000):
        assert not watcher._disk_ok()
    clock[0] = prewarm.BLOCKED_LOG_INTERVAL - 0.01
    assert not watcher._disk_ok()
    clock[0] = prewarm.BLOCKED_LOG_INTERVAL
    for _ in range(10_000):
        assert not watcher._disk_ok()
    clock[0] *= 2
    assert not watcher._disk_ok()
    free[0] = prewarm.DISK_LOW_WATER_BYTES
    assert watcher._disk_ok()
    assert watcher._disk_ok()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row['event'] for row in events] == [
        'disk_blocked', 'disk_blocked_summary', 'disk_blocked_summary', 'disk_unblocked',
    ]
    assert events[-1]['blocked_for_s'] == 120
    assert watcher.stats['skipped_disk'] == 20_002  # checks, not dropped queue entries


def test_disk_checks_minimum_cache_filesystem_and_existing_parent(
    prewarm, watcher, tmp_path, monkeypatch, capsys,
):
    cache_a, cache_b = tmp_path / 'cache-a', tmp_path / 'cache-b'
    cache_a.mkdir()
    cache_b.mkdir()
    monkeypatch.setattr(prewarm, 'CACHE_ROOTS', {
        str(tmp_path / 'mount-a'): cache_a / 'missing' / 'nested',
        str(tmp_path / 'mount-b'): cache_b,
    })
    limit = prewarm.DISK_LOW_WATER_BYTES
    free = {cache_a: limit + 1, cache_b: limit - 1}
    checked = []

    def usage(path):
        checked.append(path)
        return SimpleNamespace(free=free[path])

    monkeypatch.setattr(prewarm.shutil, 'disk_usage', usage)
    assert not watcher._disk_ok()
    assert checked == [cache_a, cache_b]
    free.update({cache_a: limit - 2, cache_b: limit + 1})
    assert not watcher._disk_ok()  # switching the blocking filesystem logs once
    assert not watcher._disk_ok()
    free[cache_a] = limit
    assert watcher._disk_ok()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row['event'] for row in events] == ['disk_blocked', 'disk_blocked', 'disk_unblocked']
    assert [row['cache_dir'] for row in events] == [str(cache_b), str(cache_a), str(cache_a)]


def test_configured_budgets_control_admission(load_prewarm, tmp_path):
    module = load_prewarm({
        'MEDIADECK_PREWARM_FILE_BUDGET_MB': '96',
        'MEDIADECK_PREWARM_WINDOW_MB': '192',
    })
    settings = tmp_path / 'settings.json'
    settings.write_text(json.dumps({'emby': {'url': 'http://emby.invalid', 'api_key': 'test'}}))
    watcher = module.PrewarmWatcher(settings)
    assert watcher._budget_ok()
    watcher._reserved_bytes = module.TOTAL_RESERVATION_LIMIT
    assert not watcher._budget_ok()
    assert watcher.stats['skipped_budget'] == 1
    watcher._reserved_bytes = 0
    watcher._window_charges = [(time.monotonic(), module.WINDOW_BYTES)]
    assert not watcher._budget_ok()
    assert watcher.stats['skipped_window'] == 1
    watcher._window_charges = [(time.monotonic() - module.WINDOW_SECONDS - 1, module.WINDOW_BYTES)]
    assert watcher._budget_ok()


@pytest.mark.parametrize('size_delta', [
    None, 1, -1, 0, 2 * 1024 ** 2 - 1, 2 * 1024 ** 2, 8 * 1024 ** 2,
], ids=['empty', 'one-byte', 'short-head', 'head-only', 'overlap', 'adjacent', 'gap'])
def test_worker_reads_head_and_tail_without_overlap(prewarm, tmp_path, size_delta):
    head = prewarm.HEAD_BYTES
    size = 0 if size_delta is None else (1 if size_delta == 1 else head + size_delta)
    mount = Path(prewarm.MEDIA_MOUNTS[0])
    cache = next(iter(prewarm.CACHE_ROOTS.values()))
    mount.mkdir()
    cache.mkdir()
    media = mount / 'sample.bin'
    with media.open('wb') as file:
        file.truncate(size)
        if size:
            file.write(b'H')
            file.seek(size - 1)
            file.write(b'Z')
    (cache / media.name).write_bytes(b'cache metadata fixture')
    result_r, result_w = os.pipe()
    trace_r, trace_w = os.pipe()
    parent_pid = os.getpid()

    def run_worker():
        reads = []

        class TrackedFile:
            def __init__(self, file):
                self.file = file

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.file.close()

            def __getattr__(self, name):
                return getattr(self.file, name)

            def read(self, count):
                start = self.file.tell()
                data = self.file.read(count)
                if data:
                    reads.append([start, len(data), data[:1].hex(), data[-1:].hex()])
                return data

        def tracked_open(*args, **kwargs):
            assert os.getpid() != parent_pid
            return TrackedFile(builtins.open(*args, **kwargs))

        prewarm.open = tracked_open
        prewarm.worker_main([str(media)], head, result_w)
        os.write(trace_w, json.dumps(reads).encode())

    process = multiprocessing.get_context('fork').Process(target=run_worker)
    try:
        process.start()
        os.close(result_w)
        os.close(trace_w)
        process.join(timeout=5)
        assert not process.is_alive(), 'worker did not finish'
        assert process.exitcode == 0
        result = json.loads(os.read(result_r, 65536))
        reads = json.loads(os.read(trace_r, 65536))
    finally:
        if process.is_alive():
            process.kill()
            process.join()
        os.close(result_r)
        os.close(trace_r)
        process.close()
    assert result.get('error') is None
    assert result['used'] == str(media)
    assert 0 <= result['tail_read_s'] <= result['total_s']
    assert result['allocated_before'] >= prewarm.ALLOCATED_SLOP
    assert sum(row[1] for row in reads) == min(size, head + prewarm.TAIL_BYTES)
    if not size:
        assert reads == []
        return
    assert reads[0][0] == 0
    assert reads[0][2] == (b'Z' if size == 1 else b'H').hex()
    assert reads[-1][0] + reads[-1][1] == size
    assert reads[-1][3] == b'Z'.hex()
    for previous, current in pairwise(reads):
        assert previous[0] + previous[1] <= current[0]  # no repeated bytes
    if size > head + prewarm.TAIL_BYTES:
        assert any(row[0] == size - prewarm.TAIL_BYTES for row in reads)
        assert all(start < head or start >= size - prewarm.TAIL_BYTES for start, *_ in reads)


def test_pump_reports_tail_timing_and_releases_reservation(prewarm, watcher, capsys):
    mount = Path(prewarm.MEDIA_MOUNTS[0])
    mount.mkdir()
    media = mount / 'sample.bin'
    media.write_bytes(b'synthetic media')
    watcher._spawn([str(media)])
    deadline = time.monotonic() + 5
    try:
        while watcher._procs and time.monotonic() < deadline:
            watcher.pump()
            time.sleep(0.01)
        assert not watcher._procs
        assert watcher.stats['done'] == 1
        assert watcher._reserved_bytes == 0
    finally:
        for _, pid, fd, _ in watcher._procs:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            os.close(fd)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    done = next(row for row in events if row['event'] == 'worker_done')
    assert done['tail_read_s'] is not None
