"""Current node health must not be inferred from retained accounting history."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_metering import _env, _sample, _svc

from app.main import app
from app.modules.bot_views import quota_lines

NOW = 1_700_000_000.0


def test_inventory_changes_apply_immediately_without_touching_history(tmp_path):
    inventory = ['active', 'retiring']
    svc = _svc(tmp_path, tags={'tag': 'user'}, expected=inventory)
    svc.ingest(_env('active', 'boot', 1, [_sample('a', 'tag', 100)], observed_at=NOW))
    svc.ingest(_env('retiring', 'old', 1,
                    [_sample('b', 'tag', 40, observed_at=NOW - 300)], observed_at=NOW - 300))
    before = svc.snapshot('user', now=NOW)
    assert before['coverage']['degraded'] and before['measured_used_bytes'] == 140
    watermarks = svc._db.query('SELECT * FROM measured_watermarks')
    ledger = svc._db.query('SELECT * FROM measured_usage_monthly')
    reporters = svc._db.query('SELECT * FROM measured_node_seq')

    inventory.remove('retiring')
    after = svc.snapshot('user', now=NOW)
    assert not after['coverage']['degraded']
    assert [n['name'] for n in after['coverage']['nodes']] == ['active']
    assert after['measurement_status'] == 'measured'
    assert after['measured_used_bytes'] == before['measured_used_bytes']
    assert svc._db.query('SELECT * FROM measured_watermarks') == watermarks
    assert svc._db.query('SELECT * FROM measured_usage_monthly') == ledger
    assert svc._db.query('SELECT * FROM measured_node_seq') == reporters
    assert {n['node'] for n in svc.totals(now=NOW)['by_node']} == {'active', 'retiring'}
    assert [n['name'] for n in svc.totals(now=NOW)['coverage']['nodes']] == ['active']

    inventory.append('retiring')
    restored = svc.snapshot('user', now=NOW)
    assert restored['coverage']['degraded']
    assert restored['coverage']['nodes'][1]['reason'] == 'stale'
    svc.ingest(_env('retiring', 'new', 1, [], observed_at=NOW))
    assert not svc.snapshot('user', now=NOW)['coverage']['degraded']


def test_new_enabled_node_is_missing_until_it_reports(tmp_path):
    inventory = ['active']
    svc = _svc(tmp_path, expected=inventory)
    svc.ingest(_env('active', 'boot', 1, [], observed_at=NOW))
    assert not svc.snapshot('user', now=NOW)['coverage']['degraded']
    inventory.append('new-node')
    sample = svc.snapshot('user', now=NOW)
    assert sample['coverage']['degraded']
    assert sample['coverage']['nodes'][1]['reason'] == 'never_reported'
    assert sample['measured_used_bytes'] is None
    assert sample['measurement_status'] == 'unavailable'


def test_explicit_empty_inventory_does_not_resurrect_reporters(tmp_path):
    inventory = ['active']
    svc = _svc(tmp_path, expected=inventory)
    svc.ingest(_env('active', 'boot', 1, [], observed_at=NOW))
    inventory.clear()
    snapshot = svc.snapshot('user', now=NOW)
    assert snapshot['coverage']['nodes'] == []
    assert snapshot['coverage']['degraded']
    assert snapshot['measured_used_bytes'] is None
    assert snapshot['measurement_status'] == 'unavailable'


def test_standalone_no_inventory_keeps_reporter_discovery(tmp_path):
    svc = _svc(tmp_path)
    svc.ingest(_env('observed', 'boot', 1, [], observed_at=NOW))
    assert [n['name'] for n in svc.snapshot('user', now=NOW)['coverage']['nodes']] == ['observed']


def test_live_api_follows_enable_disable_remove_and_reenable_without_restart(monkeypatch):
    import time

    nodes = [SimpleNamespace(name='edge-a', enabled=True),
             SimpleNamespace(name='edge-b', enabled=True)]
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.settings_service, 'nodes', lambda: nodes)
        for name, age in [('edge-a', 0), ('edge-b', 300), ('deleted-edge', 600)]:
            app.state.metering.ingest(_env(name, 'boot', 1, [], observed_at=time.time()-age))

        def coverage():
            r = client.get('/api/metering', auth=('admin', 'change-me'))
            assert r.status_code == 200
            return r.json()['totals']['coverage']['nodes']

        assert [n['name'] for n in coverage()] == ['edge-a', 'edge-b']
        nodes[1].enabled = False
        assert [n['name'] for n in coverage()] == ['edge-a']
        nodes[1].enabled = True
        assert coverage()[1]['reason'] == 'stale'
        nodes.pop()
        assert [n['name'] for n in coverage()] == ['edge-a']
        nodes.clear()
        assert coverage() == []


@pytest.mark.parametrize('used', [None, 0, 512])
@pytest.mark.parametrize('public', [False, True])
def test_bot_omits_incomplete_warning_without_faking_unknown_usage(used, public):
    member = {'quota_source': 'measured', 'measured_used_bytes': used,
              'traffic_used_bytes': 999999, 'traffic_quota_bytes': 1024,
              'metering': {'coverage': {'degraded': True}, 'measurement_status': 'partial'}}
    text = '\n'.join(quota_lines(member, public=public))
    assert '数据不完整' not in text and '采集不完整' not in text
    assert '999999' not in text and '估算' not in text
    if used is None:
        assert '计量暂不可用' in text and '暂无法确认' in text
        assert '0 B' not in text
    else:
        assert f'已用：<b>{used} B</b>' in text
