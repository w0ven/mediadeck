"""Byte accounting retirement: real isolated DB/API/agent checks, no production data."""
import importlib.util
import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs

import pytest
from fastapi.testclient import TestClient
from test_verified_watch import (
    BASE,
    env,  # noqa: F401
    feed,
)

from app.core.config import NodePool, StreamNode
from app.core.errors import ConfigError
from app.main import app
from app.modules.bot_views import quota_lines
from app.modules.edgelog import TrafficLedger
from app.modules.enforcement import desired_policy
from app.modules.member_rewards import RewardError, grant_reward
from app.modules.metering import MeasuredMeteringService
from app.modules.provisioning import install_script, meterd_unit, nginx_site

ROOT = Path(__file__).resolve().parents[2]


def agent(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'agent' / (name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive(db):
    names = [r['name'] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")
             if r['name'].startswith(('measured_', 'meter_', 'edge_'))]
    return {name: db.query('SELECT * FROM '+name) for name in names}


def seed_archive(db):
    db.execute("INSERT INTO measured_usage_monthly VALUES ('2020-01','mock-a','tag','u',123456,7)")
    db.execute("INSERT INTO measured_credits VALUES ('2020-01','u',17,1)")
    db.execute("INSERT INTO edge_usage_daily VALUES ('2020-01-01','mock-a','tag','u',4567,2,3)")
    db.execute("INSERT INTO edge_cursors VALUES ('mock-a','/old.log',4,999,1)")


def test_retired_ingest_repeated_and_invalid_reports_never_write_and_require_auth():
    with TestClient(app) as client:
        db = app.state.db
        seed_archive(db)
        token = client.get('/api/nodes/mock-a/report-token', auth=('admin','change-me')).json()['report_token']
        headers = {'Authorization':'Bearer '+token}
        before = archive(db)
        payload = {'node':'mock-a','boot_id':'old-boot','seq':99,'nft_ok':True,
                   'unit':'kernel_outbound_ip_bytes','samples':[{'counter_bytes':999999999}]}
        assert client.post('/api/edge/mock-a/measured', json=payload).status_code == 401
        assert client.post('/api/edge/mock-a/report', json={}).status_code == 401
        for _ in range(2):
            response = client.post('/api/edge/mock-a/measured', json=payload, headers=headers)
            assert response.status_code == 200
            body = response.json()
            assert body['retired'] and body['accepted'] is False
            assert body['ack'] == {'boot_id':'old-boot','seq':99}
            assert body['policy']['snapshot'] is True
            response = client.post('/api/edge/mock-a/report', content=b'not-a-log-or-json', headers=headers)
            assert response.status_code == 200 and response.json()['retired']
        assert client.post('/api/edge/mock-a/measured', json={**payload,'node':'other'}, headers=headers).status_code == 422
        assert client.get('/api/edge/mock-a/policy', headers=headers).json()['traffic_tracking'] is False
        assert client.get('/api/edge/mock-a/policy').status_code == 401
        assert client.get('/api/edge/mock-a/cursors', headers=headers).json()['cursors'] == []
        assert archive(db) == before
        assert client.post('/api/metering/cutover', json={'cutover':True}, auth=('admin','change-me')).status_code == 410
        assert client.post('/api/edge/relink', json={}, auth=('admin','change-me')).status_code == 410
        assert client.get('/api/metering', auth=('admin','change-me')).json()['totals'] is None
        assert client.get('/agent/flowmeter.py').status_code == 410
        assert not hasattr(app.state,'metering') and not hasattr(app.state,'ledger')


def test_legacy_quota_exhaustion_cannot_block_but_expiry_and_suspension_still_do(env):  # noqa: F811 - pytest fixture injection
    db, members, _, _ = env
    db.execute("UPDATE groups SET billing_mode='both',traffic_quota_bytes=1 WHERE id='standard'")
    db.execute("UPDATE members SET traffic_used_bytes=100000,status='exhausted',expires_at=? WHERE emby_user_id='u'", (BASE+10000,))
    member = members.get('u')
    assert member['state'] == 'active' and member['traffic_quota_bytes'] == 0
    assert member['traffic_used_bytes'] is None
    assert desired_policy(member)['IsDisabled'] is False
    db.execute("UPDATE members SET status='suspended' WHERE emby_user_id='u'")
    assert desired_policy(members.get('u'))['IsDisabled'] is True
    db.execute("UPDATE members SET status='active',expires_at=1 WHERE emby_user_id='u'")
    assert members.get('u')['state'] == 'expired'
    assert desired_policy(members.get('u'))['IsDisabled'] is True


def test_watch_records_and_rewards_input_continue_without_bytes_or_history_reset(env):  # noqa: F811 - pytest fixture injection
    db, members, sampler, stats = env
    seed_archive(db)
    db.execute("UPDATE members SET traffic_used_bytes=77,traffic_period_start=1,overrides_json=? WHERE emby_user_id='u'",
               (json.dumps({'extra_traffic_bytes': 888}),))
    before = archive(db)
    feed(sampler, BASE, 0)
    feed(sampler, BASE+30, 30)
    feed(sampler, BASE+60, 60)
    sampler._sample([], BASE+75, None)
    assert db.one('SELECT seconds FROM watch_verified_totals')['seconds'] == 60
    assert db.one('SELECT bytes FROM play_events')['bytes'] == 0
    assert db.one('SELECT bytes FROM usage_daily')['bytes'] == 0
    assert stats.member_detail('u')['recent_plays'][0]['seconds'] == 60
    assert 'bytes' not in stats.member_detail('u')['recent_plays'][0]
    assert 'bytes' not in stats.top_users()[0]
    assert 'traffic' not in stats.overview()
    members.add_traffic('u',9999)
    assert members.roll_periods(BASE+50*86400) == 0
    with pytest.raises(ConfigError): members.reset_traffic('u')
    with pytest.raises(ConfigError): members.add_extra_traffic('u',10)
    with pytest.raises(RewardError): grant_reward(db,members,'u','traffic',10)
    members.set_overrides('u',{'max_streams':4,'extra_traffic_bytes':1000})
    members.upsert('u','viewer',{'traffic_used_bytes':0})
    assert db.one("SELECT traffic_used_bytes FROM members WHERE emby_user_id='u'")['traffic_used_bytes'] == 77
    assert members.get('u')['overrides']['extra_traffic_bytes'] == 888
    assert members.get('u')['max_streams'] == 4
    db.execute("UPDATE play_events SET bytes=456,ended_at=1")
    db.execute("UPDATE usage_daily SET bytes=123,day='2000-01-01'")
    stats.prune(30)
    assert db.one('SELECT bytes FROM play_events')['bytes'] == 456
    assert db.one('SELECT bytes FROM usage_daily')['bytes'] == 123
    assert archive(db) == before
    assert quota_lines(members.get('u')) == []


def test_legacy_service_objects_cannot_write_or_enable_collectors(env,tmp_path):  # noqa: F811 - pytest fixture injection
    db, _, _, _ = env
    seed_archive(db)
    before = archive(db)
    meter = MeasuredMeteringService(db)
    assert meter.ingest({'samples':[{'counter_bytes':55}]})['retired']
    assert meter.used_bytes('u') is None
    with pytest.raises(ValueError): meter.reset_credit('u')
    ledger = TrafficLedger(db)
    assert ledger.ingest_batch('node',{})['retired']
    ledger.record('node',{},{}); ledger.set_cursor('node','log',1,100); ledger.relink({})
    assert archive(db) == before
    flow = agent('flowmeter')
    with pytest.raises(RuntimeError): flow.FlowMeter(str(tmp_path/'must-not-exist.db'),enabled=True)
    assert not (tmp_path/'must-not-exist.db').exists()
    assert agent('edgereport').run_once('https://never-contact.invalid','node','unused','/no-log') == 0


def test_guard_identity_deny_restart_and_unblock_without_meter_database(tmp_path,monkeypatch):
    mod = agent('meterd')
    state, deny = tmp_path/'access.json',tmp_path/'deny.map'
    deny.write_text('"oldquota" 1;\n"manual" 1;\n')
    guard = mod.AccessPolicy(str(state),str(deny),mod.DenyPublisher(str(deny)))
    killed = []
    monkeypatch.setattr(guard,'terminate',lambda key:killed.append(key))
    qs = parse_qs('lip=127.0.0.1&lp=443&a=10.0.0.1&p=1234&cid=1.1&u=member')
    assert guard.register(qs) == 204
    assert guard.register({**qs,'u':['other']}) == 403
    assert killed
    assert guard.register({**qs,'u':['manual']}) == 403
    result = guard.apply({'ok':True,'policy':{'snapshot':True,'blocked_tags':['manual','member']}})
    assert result['ok'] and 'oldquota' not in deny.read_text()
    assert guard.register(qs) == 403
    restored = mod.AccessPolicy(str(state),str(deny))
    assert restored.register(qs) == 403
    with pytest.raises(ValueError): guard.apply({'ok':True,'policy':{'blocked_tags':[]}})
    assert guard.blocked == {'manual','member'}
    guard.apply({'ok':True,'policy':{'snapshot':True,'blocked_tags':[]}})
    assert guard.register(qs) == 204
    assert not list(tmp_path.glob('*.db'))
    assert not any(x in state.read_text() for x in ('bytes','packets','counter'))


def test_guard_reads_only_legacy_identities_and_never_modifies_old_database(tmp_path):
    mod = agent('meterd')
    old = tmp_path/'flowmeter.db'
    db = sqlite3.connect(old)
    db.execute('CREATE TABLE conns(local_ip,local_port,remote_ip,remote_port,utag,nginx_cid,closed_at,counter_bytes)')
    db.execute("INSERT INTO conns VALUES('127.0.0.1',443,'10.0.0.1',1234,'member','1.1',NULL,999)")
    db.commit(); db.close()
    before = old.read_bytes()
    guard = mod.AccessPolicy(str(tmp_path/'access.json'),str(tmp_path/'deny'),legacy_path=str(old))
    assert list(guard.connections.values()) == ['member']
    assert old.read_bytes() == before


def test_probe_no_byte_collectors_or_traffic_logging_generated(tmp_path,monkeypatch):
    mod = agent('loadprobe')
    monkeypatch.setattr(threading.Thread,'start',lambda _: None)
    sampler = mod.Sampler('ignored',{443},Mock())
    assert sampler.snapshot() == {'ok':True,'active_streams':0,'traffic_tracking':False}
    assert not hasattr(mod,'SpeedLog') and not hasattr(mod,'tx_bytes') and not hasattr(mod,'conn_bytes')
    node = StreamNode(name='edge-a',base_url='https://edge.example',probe_url='http://127.0.0.1:9800/load',sign_secret='test-secret',
                      pools=[NodePool(name='p',emby_prefix='/media',node_path='/srv/media',url_prefix='/s')])
    site = nginx_site(node)
    for old in ('bytes_sent','mediadeck-speed.log','mediadeck_speed','mirror /_mediadeck/announce'):
        assert old not in site
    for required in ('auth_request /_mediadeck/verify','deny.map','limit_conn','X-Mediadeck-Rate','proxy_force_ranges on'):
        assert required in site
    unit = meterd_unit(node,'https://panel.example')
    assert '--enable-nft' not in unit and '--state-file' in unit
    script = install_script(node,'https://panel.example')
    assert '/agent/flowmeter.py' not in script
    assert 'mediadeck-stream-rate.service' in script
    assert 'mediadeck-meterd.service' in script


def test_static_admin_controls_removed_not_just_bot_copy():
    for filename in ('app.js','members.js','ops.js','nodepool.js','activity_admin.js'):
        source = (ROOT/'backend/app/static'/filename).read_text()
        for marker in ('本月实测流量','data-act="metering"','data-member-action="reset-traffic"','本月流量','id="ov-extra"','月流量bytes'):
            assert marker not in source


def test_old_traffic_table_layout_and_rows_survive_database_startup(tmp_path):
    from app.core.db import Database
    path = tmp_path/'old.db'
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE measured_watermarks(node TEXT,boot_id TEXT,conn_id TEXT,generation INTEGER,
          utag TEXT,emby_user_id TEXT,counter_bytes INTEGER,updated_at REAL,
          PRIMARY KEY(node,boot_id,conn_id,generation));
        INSERT INTO measured_watermarks VALUES('old','boot','conn',1,'tag','u',123,4);
        CREATE TABLE meter_policy_ack(node TEXT PRIMARY KEY,rev INTEGER,acked_at REAL);
        INSERT INTO meter_policy_ack VALUES('old',19,20);
    """)
    conn.commit(); conn.close()
    db = Database(path)
    try:
        assert db.one('SELECT * FROM measured_watermarks')['counter_bytes'] == 123
        assert db.one('SELECT * FROM measured_watermarks')['boot_id'] == 'boot'
        assert db.one('SELECT * FROM meter_policy_ack') == {'node':'old','rev':19,'acked_at':20.0}
    finally:
        db.close()


def test_scheduler_ignores_old_agent_byte_telemetry_but_retains_capacity():
    import asyncio
    from unittest.mock import AsyncMock

    from app.modules.scheduler import Scheduler
    node = StreamNode(name='old',base_url='https://example.invalid',probe_url='http://127.0.0.1/load',capacity=10,bandwidth_mbps=1)
    probe = Mock(load=AsyncMock(return_value={'ok':True,'active_streams':3,'egress_mbps':999,
                                            'user_speeds':{'tag':999999},'user_speeds_ok':True}))
    scheduler = Scheduler([node],probe)
    asyncio.run(scheduler.refresh())
    row = scheduler.snapshot()[0]
    assert row['active_streams'] == 3 and row['utilisation'] == .3
    assert 'egress_mbps' not in row and scheduler.user_speed_view() == {}
    assert 'egress_mbps' not in scheduler.history('old')[0]


def test_new_level_configuration_has_no_quota_dependency_but_accepts_old_archives():
    from app.modules.levels import defaults, validate
    config = defaults()
    assert all('traffic_quota_bytes' not in row for row in config['levels'])
    assert validate(config) == config
    for row in config['levels']:
        row['traffic_quota_bytes'] = 123
    assert validate(config)['levels'][0]['traffic_quota_bytes'] == 123
