"""Regression for migrated status sources; no production requests or notifications."""
import importlib.util
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.modules.intake_plugin import IntakePipelinePlugin, IntakeSnapshotReader
from app.modules.mounts import MountsReader
from app.modules.tasks import TasksReader


def payload(stamp=None):
    return {'generated_at': time.time() if stamp is None else stamp,
            **{k: {} for k in ('emby', 'refresh', 'notify', 'upload', 'cloud', 'downloader', 'health')}}


def test_external_generation_not_copy_time(tmp_path):
    p = tmp_path / 'intake.json'
    p.write_text(json.dumps(payload(time.time() - 400)))
    assert IntakeSnapshotReader(str(p)).get()['stale']
    p.write_text(json.dumps(payload()))
    assert not IntakeSnapshotReader(str(p)).get()['stale']


@pytest.mark.parametrize('content', ['[]', '{}', '{', json.dumps(payload(float('nan'))),
                                    json.dumps(payload(time.time() + 3600))])
def test_invalid_external_snapshot_is_unavailable(tmp_path, content):
    p = tmp_path / 'intake.json'
    p.write_text(content)
    assert IntakeSnapshotReader(str(p)).get()['available'] is False


def test_missing_snapshot(tmp_path):
    p = str(tmp_path / 'missing')
    for reader in (IntakeSnapshotReader(p), MountsReader(p), TasksReader(p)):
        get = getattr(reader, 'get', None) or reader.snapshot
        assert get()['available'] is False


@pytest.mark.asyncio
async def test_external_manual_run_never_collects_wrong_local_paths(tmp_path):
    reader = IntakeSnapshotReader(str(tmp_path / 'missing'))
    plugin = IntakePipelinePlugin(SimpleNamespace(intake_store=reader))
    result = await plugin.run({})
    assert not result['ok'] and '外部' in result['error']


def test_snapshot_path_not_a_local_intake_directory():
    cfg = Settings(intake_snapshot_path='/configured/snapshot.json')
    assert 'snapshot_path' not in cfg.intake_paths()


@pytest.mark.parametrize('reader,key', [(MountsReader, 'mounts'), (TasksReader, 'tasks')])
def test_failure_and_staleness_are_preserved(tmp_path, reader, key):
    p = tmp_path / 'snapshot.json'
    p.write_text(json.dumps({key: [{'alive': False, 'last_status': 'failed'}],
                            'alerts': [{'level': 'bad', 'message': 'real failure'}]}))
    os.utime(p, (time.time()-700, time.time()-700))
    data = reader(str(p)).snapshot()
    assert data['stale'] and data['data'][key][0]['last_status'] == 'failed'


def collector():
    path = Path(__file__).parents[1] / 'tools/collect-status.py'
    spec = importlib.util.spec_from_file_location('test_status_collector', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_mount_not_healthy_directory():
    c = collector()
    row = c.mount_row('expected', '/tmp', 'dummy.service', {})
    assert row['alive'] is False and row['kind'] == 'none'
    assert row['fs_total_bytes'] is None


def test_effective_size_and_role():
    c = collector()
    assert c.size_value('200G') == 200 * 2**30
    assert c.size_value('50G') == 50 * 2**30
    assert c.size_value('') is None
    assert all('csf' not in path for path, unit in c.MOUNTS.values())


@pytest.mark.asyncio
async def test_stale_backend_not_restamped(tmp_path, monkeypatch):
    c = collector()
    monkeypatch.setattr(c, 'STATE', tmp_path)
    s = tmp_path / 'settings.json'
    s.write_text(json.dumps({'emby': {}}))
    monkeypatch.setattr(c, 'settings', lambda: SimpleNamespace(settings_file=s))
    data = payload(time.time()-500)
    data['source'] = {'role': 'download-backend'}
    (tmp_path / 'intake-backend-snapshot.json').write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match='backend snapshot unavailable'):
        await c.collect_intake()
    assert not (tmp_path / 'intake-snapshot.json').exists()


@pytest.mark.asyncio
async def test_roles_and_partial_failure_not_green(tmp_path, monkeypatch):
    c = collector()
    monkeypatch.setattr(c, 'STATE', tmp_path)
    s = tmp_path / 'settings.json'
    s.write_text(json.dumps({'emby': {}}))
    monkeypatch.setattr(c, 'settings', lambda: SimpleNamespace(settings_file=s))
    async def emby(_):
        return {'available': False, 'reason': 'test failure'}
    monkeypatch.setattr(c.IntakeCollector, 'collect_emby', emby)
    data = payload()
    data['source'] = {'role': 'download-backend', 'host': 'download-test'}
    (tmp_path / 'intake-backend-snapshot.json').write_text(json.dumps(data))
    out = await c.collect_intake()
    assert out['sources']['backend']['host'] == 'download-test'
    assert out['sources']['emby']['role'] == 'playback-host'
    assert out['health']['level'] == 'warn'
    assert out['generated_at'] == data['generated_at']
