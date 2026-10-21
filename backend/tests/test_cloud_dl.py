"""Cloud status contract: no business script imports, writes, credentials or sends."""
import importlib.util
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

TOOLS = Path(__file__).parents[1] / 'tools'
HOST = 'cloud-dl'  # neutral default SSH alias / source host


def load(name):
    sys.path.insert(0, str(TOOLS))
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def status(now=1000):
    return {'source': {'host': HOST, 'hostname': 'test-dl', 'role': 'cloud-download-owner'},
            'observed_at': now, 'progress_generated_at': now-30, 'backlog_generated_at': now-60,
            'jobs': 0, 'progress_jobs': 0, 'active_files': 0, 'downloaded_bytes': 0,
            'total_bytes': 0, 'live_mibs': 0.0, 'backlog': 0, 'gd_pending': 0, 'gd_done': 3718}


@pytest.fixture(autouse=True)
def neutral_env(monkeypatch):
    for name in ('MEDIADECK_CLOUD_DL_PATHS', 'MEDIADECK_INTAKE_PATHS', 'MEDIADECK_STATUS_MOUNTS'):
        monkeypatch.delenv(name, raising=False)


def test_idle_is_available_with_original_generations():
    c = load('mediadeck_cloud_dl')
    result = c.validate(status(), now=1000)
    assert result['available'] and result['jobs'] == 0
    assert result['generated_at'] == 940 and result['progress_generated_at'] == 970
    assert result['gd_upload']['pending'] == 0 and result['gd_upload']['done'] == 3718
    assert result['legacy_owner']['status'] == 'disabled'
    assert result['legacy_owner']['applicable'] is False
    assert 'available' not in result['legacy_owner']


@pytest.mark.parametrize('field,value', [('progress_generated_at', 1), ('backlog_generated_at', 1),
    ('observed_at', 1100), ('jobs', -1), ('jobs', True), ('gd_pending', None),
    ('live_mibs', float('nan')), ('source', {'host':'old-owner','role':'cloud-download-owner'})])
def test_invalid_or_stale_cannot_become_fresh_zero(field, value):
    c = load('mediadeck_cloud_dl')
    raw = status(); raw[field] = value
    with pytest.raises(ValueError):
        c.validate(raw, now=1000)


@pytest.mark.parametrize('exc', [subprocess.TimeoutExpired('ssh', 7),
                               subprocess.CalledProcessError(255, 'ssh'), OSError('unavailable')])
def test_bounded_ssh_failure_is_cloud_unavailable(monkeypatch, exc):
    c = load('mediadeck_cloud_dl')
    def fail(args, **kwargs):
        assert kwargs['timeout'] == 7
        assert 'BatchMode=yes' in args and 'StrictHostKeyChecking=yes' in args
        assert 'timeout --kill-after=1s 5s' in args[-1]
        raise exc
    monkeypatch.setattr(c.subprocess, 'run', fail)
    result = c.collect_cloud()
    assert not result['available'] and result['source']['host'] == HOST
    assert result['legacy_owner']['applicable'] is False
    assert 'jobs' not in result  # Failure is not a fabricated empty queue.


def test_env_selects_ssh_alias_and_only_known_keys(monkeypatch):
    monkeypatch.setenv('MEDIADECK_CLOUD_DL_PATHS', json.dumps({'host': 'dl-test'}))
    c = load('mediadeck_cloud_dl')
    seen = []
    def run(args, **kwargs):
        seen.append(args)
        raise OSError('unavailable')
    monkeypatch.setattr(c.subprocess, 'run', run)
    assert c.collect_cloud()['source']['host'] == 'dl-test'
    assert seen[0][-2] == 'dl-test'
    raw = status(); raw['source'] = dict(raw['source'], host='dl-test')
    assert c.validate(raw, now=1000)['available']
    for bad in ('{"unknown": "x"}', '{"host": "-oProxyCommand=x"}', '{"jobs_dir": 1}', '[]', '{'):
        monkeypatch.setenv('MEDIADECK_CLOUD_DL_PATHS', bad)
        c = load('mediadeck_cloud_dl')
        monkeypatch.setattr(c.subprocess, 'run', lambda *a, **k: pytest.fail('ssh with bad config'))
        result = c.collect_cloud()
        assert not result['available'] and 'jobs' not in result


def test_remote_paths_are_data_not_shell_or_python(tmp_path):
    c = load('mediadeck_cloud_dl')
    # Quotes, $(...) and backticks in a path must be read literally.
    root = tmp_path / "st'ate $(touch pwned) `touch pwned2`"
    for name in ('jobs', 'pending', 'done'):
        (root / name).mkdir(parents=True)
    (root / 'jobs' / 'a.json').write_text('{}')
    (root / 'done' / 'b.json').write_text('{}')
    now = time.time()
    (root / 'ownership.json').write_text(json.dumps({'mode': 'owner"x'}))
    (root / 'p.json').write_text(json.dumps({'at': now, 'ssh_ok': True, 'files': [],
                                             'jobs_pending': 0, 'live_mibs': 0.0}))
    (root / 'backlog.json').write_text(json.dumps({'at': now, 'rows': []}))
    paths = dict(c.DEFAULT_PATHS, state_root=str(root) + '/', owner_mode='owner"x',
                 progress_file='p.json', jobs_dir=str(root / 'jobs'),
                 gd_pending_dir=str(root / 'pending'), gd_done_dir=str(root / 'done'))
    command = shlex.quote(sys.executable) + ' -B -c ' + shlex.quote(c.remote_script(paths))
    # sshd hands the command string to the remote login shell.
    proc = subprocess.run(['sh', '-c', command], cwd=tmp_path, capture_output=True,
                          text=True, timeout=10, check=True)
    result = c.validate(json.loads(proc.stdout))
    assert result['jobs'] == 1 and result['gd_upload']['done'] == 1
    assert result['source']['host'] == HOST
    assert not list(tmp_path.glob('pwned*'))


def test_intake_paths_override_by_key_only(monkeypatch):
    monkeypatch.setenv('MEDIADECK_INTAKE_PATHS', json.dumps({'staging_dir': '/srv/example'}))
    c = load('mediadeck-intake-collector')
    assert c.PATHS['staging_dir'] == '/srv/example'
    assert c.PATHS['notify_log'] == c.DEFAULT_PATHS['notify_log']
    with pytest.raises(ValueError):
        c.intake_paths('{"not_a_key": "/x"}')


def test_status_mounts_come_from_env(monkeypatch):
    mounts = {'pool-a': ['/mnt/pool-a', 'rclone-pool-a.service']}
    monkeypatch.setenv('MEDIADECK_STATUS_MOUNTS', json.dumps(mounts))
    c = load('collect-status')
    assert {k: list(v) for k, v in c.MOUNTS.items()} == mounts
    for bad in ('{"x": ["/mnt/x"]}', '{"x": "/mnt/x"}', '[]'):
        monkeypatch.setenv('MEDIADECK_STATUS_MOUNTS', bad)
        with pytest.raises(ValueError):
            load('collect-status')


def test_collector_preserves_other_sources_when_cloud_fails(tmp_path, monkeypatch):
    c = load('mediadeck-intake-collector')
    c.OUT = tmp_path / 'intake.json'
    local = {'generated_at': 1000, 'refresh': {'available': True, 'total': 2},
             'notify': {'pending': 0}, 'upload': {'items': 4}, 'downloader': {'clients': []}}
    monkeypatch.setattr(c, 'local_snapshot', lambda: dict(local))
    monkeypatch.setattr(c, 'collect_cloud', lambda: {'available': False, 'reason': 'timeout'})
    c.main()
    result = json.loads(c.OUT.read_text())
    assert result['cloud']['available'] is False
    assert all(result[k] == v for k, v in local.items())


@pytest.mark.asyncio
@pytest.mark.parametrize('failed', [False, True])
async def test_only_live_failure_affects_aggregate(tmp_path, monkeypatch, failed):
    c = load('collect-status')
    dl = load('mediadeck_cloud_dl')
    monkeypatch.setattr(c, 'STATE', tmp_path)
    s = tmp_path/'settings.json'; s.write_text(json.dumps({'emby':{}}))
    monkeypatch.setattr(c, 'settings', lambda: SimpleNamespace(settings_file=s))
    async def emby(_):
        return {'available': True}
    monkeypatch.setattr(c.IntakeCollector, 'collect_emby', emby)
    cloud = dl.unavailable('timeout') if failed else dl.validate(status(time.time()))
    raw = {k:{'available':True} for k in ('refresh','notify','upload','downloader')}
    raw.update(generated_at=time.time(), source={'role':'download-backend'}, cloud=cloud)
    (tmp_path/'intake-backend-snapshot.json').write_text(json.dumps(raw))
    result = await c.collect_intake()
    assert result['health']['level'] == ('warn' if failed else 'ok')
    assert result['cloud']['legacy_owner']['status'] == 'disabled'
