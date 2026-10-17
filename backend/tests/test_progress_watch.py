"""Source-faithful accepted-progress replay; no live client reports or sends."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from test_verified_watch import BASE, feed, session
from test_verified_watch import env as env  # noqa: PLC0414 - shared fixture

from app.main import app
from app.modules.usage import UsageSampler


def payload(position, **changes):
    return {'SessionId':'s', 'PlaySessionId':'run', 'ItemId':'film',
            'PositionTicks':position*10_000_000, 'IsPaused':False, 'PlaybackRate':1, **changes}


@pytest.fixture
def progress(env, monkeypatch):
    db, members, sampler, stats = env
    clock = [BASE]
    monkeypatch.setattr('app.modules.usage.time.time', lambda: clock[0])
    feed(sampler, BASE, 0)
    return db, members, sampler, stats, clock


def send(sampler, clock, offset, position, **kw):
    clock[0] = BASE+offset
    sampler._progress('u', 'device', payload(position, **kw), clock[0])


def seconds(db):
    return float((db.one("SELECT seconds FROM watch_verified_totals WHERE emby_user_id='u'") or {}).get('seconds') or 0)


def diagnosis(db):
    return json.loads(db.one("SELECT summary_json FROM watch_diagnostics WHERE emby_user_id='u'")['summary_json'])


def test_progress_recovers_frozen_sessions_and_report_poll_replays_never_double_count(progress):
    db, _, sampler, stats, clock = progress
    feed(sampler, BASE+15, 0, heartbeat=BASE)  # cached position/heartbeat: no credit
    assert seconds(db) == 0
    before = db.query('SELECT emby_user_id,traffic_used_bytes FROM members')
    send(sampler, clock, 15, 15)  # switch source safely: no bridge to old poll
    send(sampler, clock, 30, 30)
    assert seconds(db) == 15
    assert db.query('SELECT emby_user_id,traffic_used_bytes FROM members') == before
    assert stats.watch_summary('u')['recorded_seconds'] == 15
    feed(sampler, BASE+45, 45)  # same report period must not be billed twice
    assert seconds(db) == 15
    send(sampler, clock, 45, 45)
    send(sampler, clock, 46, 45)  # duplicate payload does not move the baseline
    send(sampler, clock, 60, 60)
    assert seconds(db) == 45
    assert db.one('SELECT seconds FROM watch_sample_totals')['seconds'] == 45
    assert not db.query('SELECT * FROM watch_totals')
    assert diagnosis(db)['counts']['duplicate_progress'] == 1


@pytest.mark.parametrize('rate', [0.5, 1, 1.5, 2])
def test_report_speed_counts_elapsed_not_content_distance(progress, rate):
    db, _, sampler, _, clock = progress
    send(sampler, clock, 5, 5*rate, PlaybackRate=rate)
    send(sampler, clock, 35, 35*rate, PlaybackRate=rate)
    assert seconds(db) == 30


@pytest.mark.parametrize('variant,reason', [
    ('seek','seek_or_buffering'), ('paused','paused'), ('unknown_pause','pause_unknown'),
    ('missing_position','progress_missing'), ('nan','progress_missing'),
    ('rate_change','rate_changed'), ('invalid_rate','rate_unknown'), ('unknown_rate','rate_unknown'),
    ('runtime','progress_invalid'), ('stale','heartbeat_stale')])
def test_report_keeps_original_strict_rejection_gate(progress, variant, reason):
    db, _, sampler, _, clock = progress
    send(sampler, clock, 5, 5)
    event = payload(35)
    if variant == 'seek': event['PositionTicks'] = 500*10_000_000
    if variant == 'paused': event['IsPaused'] = True
    if variant == 'unknown_pause': event.pop('IsPaused')
    if variant == 'missing_position': event.pop('PositionTicks')
    if variant == 'nan': event['PositionTicks'] = float('nan')
    if variant == 'rate_change': event['PlaybackRate'] = 2
    if variant == 'invalid_rate': event['PlaybackRate'] = 0
    if variant == 'unknown_rate': event['PlaybackRate'] = None
    if variant == 'runtime': event['PositionTicks'] = 8000*10_000_000
    clock[0] = BASE+105 if variant == 'stale' else BASE+35
    sampler._progress('u', 'device', event, BASE+35)
    assert seconds(db) == 0
    assert diagnosis(db)['reason'] == reason


def test_missing_rate_uses_accepted_progress_without_inventing_session_rate(progress):
    db, _, sampler, _, clock = progress
    event = payload(5);event.pop('PlaybackRate')
    clock[0] = BASE+5;sampler._progress('u','device',event,clock[0])
    event['PositionTicks'] = 35*10_000_000
    clock[0] = BASE+35;sampler._progress('u','device',event,clock[0])
    assert seconds(db) == 30
    event['PositionTicks'] = 70*10_000_000
    clock[0] = BASE+70;sampler._progress('u','device',event,clock[0])
    assert seconds(db) == 65 and diagnosis(db)['reason'] == 'verified'
    assert diagnosis(db)['rate'] is None
    assert diagnosis(db)['timing_basis'] == 'accepted_progress'
    assert all(row['playback_rate'] is None for row in db.query('SELECT playback_rate FROM watch_verified_samples'))


@pytest.mark.parametrize('field,value', [('SessionId','other'), ('PlaySessionId','other'), ('ItemId','other')])
def test_wrong_binding_never_creates_or_credits_session(progress, field, value):
    db, _, sampler, _, clock = progress
    send(sampler, clock, 5, 5, **{field:value})
    send(sampler, clock, 35, 35, **{field:value})
    assert seconds(db) == 0 and list(sampler._live) == ['s']
    assert diagnosis(db)['reason'] == 'session_unresolved'
    assert not db.query("SELECT name FROM sqlite_master WHERE name='stream_leases'")


@pytest.mark.parametrize('shape', ['top','nested','without_session_id'])
def test_native_progress_shapes_use_the_same_bound_proof(progress, shape):
    db, _, sampler, _, clock = progress
    for offset in (5,35):
        event = payload(offset)
        if shape == 'nested':
            event['PlayState'] = {key:event.pop(key) for key in ('PositionTicks','IsPaused','PlaybackRate')}
        if shape == 'without_session_id':
            event.pop('SessionId')
        clock[0] = BASE+offset
        sampler._progress('u','device',event,clock[0])
    assert seconds(db) == 30


def test_concurrent_tick_and_duplicate_reports_credit_once(progress, monkeypatch):
    db, _, sampler, _, clock = progress
    send(sampler,clock,5,5)
    monkeypatch.setattr(sampler._emby,'active_sessions_raw',AsyncMock(return_value=[]))
    clock[0] = BASE+35
    async def check():
        await asyncio.gather(sampler.playback_progress('u','device',payload(35),received_at=clock[0]),
                             sampler.tick(),
                             sampler.playback_progress('u','device',payload(35),received_at=clock[0]))
    asyncio.run(check())
    assert seconds(db) == 30
    assert db.one('SELECT seconds FROM watch_sample_totals')['seconds'] == 30
    assert len(db.query('SELECT * FROM watch_verified_samples')) == 1
    assert db.one("SELECT traffic_used_bytes FROM members WHERE emby_user_id='u'")['traffic_used_bytes'] == 0


def test_wrong_device_and_user_cannot_credit_another_play(progress):
    db, _, sampler, _, clock = progress
    for uid, device in [('u','other'),('other','device'),('u','')]:
        for off in (5,35):
            clock[0] = BASE+off
            sampler._progress(uid,device,payload(off),clock[0])
    assert seconds(db) == 0


def test_late_stop_pause_restart_and_gap_do_not_backfill(progress):
    db, members, sampler, stats, clock = progress
    send(sampler, clock, 5, 5)
    send(sampler, clock, 35, 35)
    clock[0] = BASE+36
    sampler._playback_report('u','device',payload(35),True)
    send(sampler, clock, 50, 50)
    feed(sampler, BASE+60, 60)  # cached play cannot resurrect a Stop
    assert seconds(db) == 30
    clock[0] = BASE+70
    sampler._playback_report('u','device',payload(70),False)
    send(sampler, clock, 70, 70)
    send(sampler, clock, 100, 100)
    assert seconds(db) == 60
    restarted = UsageSampler(db,members,sampler._emby)
    send(restarted, clock, 1000, 1000)
    send(restarted, clock, 1030, 1030)
    assert seconds(db) == 90
    send(restarted, clock, 2000, 2000)
    assert seconds(db) == 90 and diagnosis(db)['reason'] == 'observation_gap'
    assert stats.watch_summary('u')['recorded_seconds'] == 90


def test_short_session_snapshot_disappearance_retains_only_fresh_report_evidence(progress):
    db, _, sampler, _, clock = progress
    send(sampler, clock, 5, 5)
    send(sampler, clock, 35, 35)
    before = db.query('SELECT traffic_used_bytes FROM members')
    sampler._sample([],BASE+40,None)
    assert 's' in sampler._live and seconds(db) == 30
    send(sampler, clock, 50, 50)
    assert seconds(db) == 45
    assert db.query('SELECT traffic_used_bytes FROM members') == before
    sampler._sample([],BASE+200,None)
    assert not sampler._live
    assert db.one('SELECT seconds FROM play_events')['seconds'] == 45
    assert seconds(db) == 45


def test_out_of_order_and_poll_report_source_clock_changes_are_conservative(progress):
    db, _, sampler, _, clock = progress
    feed(sampler,BASE+30,30)
    assert seconds(db) == 30
    send(sampler,clock,35,35)
    send(sampler,clock,65,65)
    assert seconds(db) == 60
    sampler._progress('u','device',payload(40),BASE+40)  # delayed completion of earlier request
    assert seconds(db) == 60 and diagnosis(db)['reason'] == 'out_of_order_progress'
    send(sampler,clock,95,95)
    assert seconds(db) == 90
    feed(sampler,BASE+160,160)  # report expired; poll needs a new baseline
    assert seconds(db) == 90
    feed(sampler,BASE+190,190)
    assert seconds(db) == 120


def test_diagnostics_are_bounded_and_never_retain_raw_report_credentials(progress):
    db, _, sampler, _, clock = progress
    for i in range(100):
        send(sampler,clock,5+i,5,Token='must-not-be-retained',Arbitrary='must-not-be-retained')
    stored = db.query('SELECT * FROM watch_diagnostics')
    assert len(stored) == 1 and len(stored[0]['summary_json']) < 1500
    assert 'must-not-be-retained' not in json.dumps(stored+db.query('SELECT * FROM watch_checkpoints'))
    assert seconds(db) == 0
    with db.write() as conn:
        conn.execute("INSERT INTO watch_diagnostics VALUES('expired',?, '{}')", (BASE-15*86400,))
        conn.executemany('INSERT INTO watch_diagnostics VALUES(?,?,?)',
                         [(f'viewer-{i}',BASE-100,'{}') for i in range(5001)])
    send(sampler,clock,120,5)
    assert db.one('SELECT COUNT(*) AS n FROM watch_diagnostics')['n'] == 5000
    assert not db.one("SELECT 1 FROM watch_diagnostics WHERE emby_user_id='expired'")


def test_real_http_route_authenticates_and_requires_upstream_acceptance(monkeypatch):
    clock = [BASE]
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.usage,'tick',AsyncMock(return_value={'ok':True}))
        monkeypatch.setattr('app.main.time.time',lambda:clock[0])
        app.state.members.upsert('u','viewer',{'group_id':'standard'})
        feed(app.state.usage,BASE,0)
        monkeypatch.setattr(app.state.emby,'user_for_token',AsyncMock(return_value='u'))
        accepted = AsyncMock(return_value=204)
        monkeypatch.setattr(app.state.emby,'report_playback',accepted)
        headers = {'X-Emby-Token':'caller-placeholder', 'X-Emby-Authorization':'MediaBrowser DeviceId="device"'}
        assert client.post('/api/playback/progress',json=payload(5)).status_code == 401
        for off in (5,35):
            clock[0] = BASE+off
            assert client.post('/api/playback/progress',headers=headers,json=payload(off)).status_code == 204
        assert seconds(app.state.db) == 30
        monkeypatch.setattr(app.state.emby,'report_playback',AsyncMock(return_value=503))
        clock[0] = BASE+65
        assert client.post('/api/playback/progress',headers=headers,json=payload(65)).status_code == 503
        assert seconds(app.state.db) == 30
        assert accepted.await_count == 2


@pytest.fixture
def native_progress(progress):
    db, _, sampler, _, _ = progress
    sampler._live.clear()
    raw = session(BASE, 0)
    raw.pop('PlaySessionId')  # actual native Sessions structure observed on site
    sampler._sample([raw], BASE, None)
    db.execute('CREATE TABLE stream_leases(user_id TEXT,session_id TEXT,play_id TEXT,observed INTEGER,'
               'snapshot_guard INTEGER,PRIMARY KEY(user_id,session_id))')
    db.execute("INSERT INTO stream_leases VALUES('u','s','run',1,0)")
    return progress


def test_native_missing_play_id_uses_only_existing_observed_lease(native_progress):
    db, _, sampler, _, clock = native_progress
    before = db.query('SELECT * FROM stream_leases')
    send(sampler, clock, 5, 5)
    send(sampler, clock, 35, 35)
    assert seconds(db) == 30
    assert diagnosis(db)['binding'] == 'lease'
    assert diagnosis(db)['counts']['progress_verified'] == 1
    feed(sampler, BASE+45, 45, play='')
    assert seconds(db) == 30  # report source retains priority, never poll double count
    send(sampler, clock, 65, 65)
    assert seconds(db) == 60
    assert db.query('SELECT * FROM stream_leases') == before
    db.execute('DELETE FROM stream_leases')  # real route removes lease before Stop callback
    sampler._playback_report('u','device',payload(65),True)
    feed(sampler, BASE+80, 80, play='')
    send(sampler, clock, 95, 95)
    assert seconds(db) == 60
    assert sampler._live['s']['stopped_by_report']


@pytest.mark.parametrize('variant', ['missing','wrong_play','wrong_user','wrong_sid','not_observed',
                                     'guarded','wrong_device','wrong_item','raw_conflict','ambiguous'])
def test_native_binding_never_trusts_self_report_or_wrong_identity(native_progress, variant):
    db, _, sampler, _, clock = native_progress
    changes = {}
    if variant == 'missing': db.execute('DELETE FROM stream_leases')
    if variant == 'wrong_play': db.execute("UPDATE stream_leases SET play_id='other'")
    if variant == 'wrong_user': db.execute("UPDATE stream_leases SET user_id='other'")
    if variant == 'wrong_sid': db.execute("UPDATE stream_leases SET session_id='other'")
    if variant == 'not_observed': db.execute('UPDATE stream_leases SET observed=0')
    if variant == 'guarded': db.execute('UPDATE stream_leases SET snapshot_guard=1')
    if variant == 'wrong_device': sampler._live['s']['device_id'] = 'other'
    if variant == 'wrong_item': changes['ItemId'] = 'other'
    if variant == 'raw_conflict':
        sampler._live['s']['play_id'] = 'explicit-other'
        sampler._live['s']['play_id_source'] = 'sessions'
    if variant == 'ambiguous':
        sampler._live['other'] = dict(sampler._live['s'])
        db.execute("INSERT INTO stream_leases VALUES('u','other','run',1,0)")
        changes['SessionId'] = ''
    before = db.query('SELECT * FROM stream_leases')
    send(sampler, clock, 5, 5, **changes)
    send(sampler, clock, 35, 35, **changes)
    assert seconds(db) == 0 and diagnosis(db)['reason'] == 'session_unresolved'
    assert db.query('SELECT * FROM stream_leases') == before


def test_native_replaced_lease_old_stop_report_and_deleted_binding_do_not_credit(native_progress):
    db, _, sampler, _, clock = native_progress
    send(sampler, clock, 5, 5)
    send(sampler, clock, 35, 35)
    db.execute("UPDATE stream_leases SET play_id='new-run'")
    sampler._playback_report('u','device',payload(35),True)
    assert not sampler._live['s'].get('stopped_by_report')
    send(sampler, clock, 65, 65)
    assert seconds(db) == 30 and diagnosis(db)['reason'] == 'session_unresolved'
    send(sampler, clock, 66, 66, PlaySessionId='new-run')
    assert seconds(db) == 30 and diagnosis(db)['reason'] == 'identity_changed'
    send(sampler, clock, 96, 96, PlaySessionId='new-run')
    assert seconds(db) == 60
    db.execute('DELETE FROM stream_leases')
    send(sampler, clock, 126, 126, PlaySessionId='new-run')
    assert seconds(db) == 60 and diagnosis(db)['reason'] == 'session_unresolved'


def test_native_lease_replacement_between_match_and_commit_is_atomic(native_progress, monkeypatch):
    db, _, sampler, _, clock = native_progress
    send(sampler, clock, 5, 5)
    original = sampler._record_watch
    def race(*args, **kwargs):
        db.execute("UPDATE stream_leases SET play_id='replacement'")
        return original(*args, **kwargs)
    monkeypatch.setattr(sampler, '_record_watch', race)
    send(sampler, clock, 35, 35)
    assert seconds(db) == 0
    assert sampler._live['s']['report_received_at'] == BASE+5
    assert diagnosis(db)['reason'] == 'session_unresolved'


def test_native_restart_and_missing_rate_keep_original_proof_gate(native_progress):
    db, members, sampler, _, clock = native_progress
    send(sampler, clock, 5, 5)
    send(sampler, clock, 35, 35)
    restored = UsageSampler(db, members, sampler._emby)
    send(restored, clock, 65, 65)
    assert seconds(db) == 30  # first post-restart report is a new baseline
    send(restored, clock, 95, 95)
    assert seconds(db) == 60
    send(restored, clock, 125, 125, PlaybackRate=None)
    assert seconds(db) == 60 and diagnosis(db)['reason'] == 'rate_unknown'


def test_real_native_http_route_uses_issuance_lease_without_creating_one(monkeypatch):
    clock = [BASE]
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.usage,'tick',AsyncMock(return_value={'ok':True}))
        monkeypatch.setattr('app.main.time.time',lambda:clock[0])
        app.state.members.upsert('u','viewer',{'group_id':'standard'})
        raw = session(BASE,0);raw.pop('PlaySessionId')
        app.state.usage._sample([raw],BASE,None)
        app.state.db.execute("INSERT INTO stream_leases(user_id,session_id,play_id,observed,pending_at) VALUES('u','s','run',0,?)",(BASE,))
        monkeypatch.setattr(app.state.emby,'user_for_token',AsyncMock(return_value='u'))
        monkeypatch.setattr(app.state.emby,'report_playback',AsyncMock(return_value=204))
        headers = {'X-Emby-Token':'caller-placeholder','X-Emby-Device-Id':'device'}
        for off in (5,35):
            clock[0] = BASE+off
            assert client.post('/api/playback/progress',headers=headers,json=payload(off)).status_code == 204
        assert seconds(app.state.db) == 30
        assert app.state.db.one('SELECT COUNT(*) n FROM stream_leases')['n'] == 1
        assert diagnosis(app.state.db)['binding'] == 'lease'
