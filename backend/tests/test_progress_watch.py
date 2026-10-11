"""Source-faithful accepted-progress replay; no live client reports or sends."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from test_verified_watch import BASE, feed
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


def test_missing_rate_only_uses_explicit_fresh_bound_session_metadata(progress):
    db, _, sampler, _, clock = progress
    event = payload(5);event.pop('PlaybackRate')
    clock[0] = BASE+5;sampler._progress('u','device',event,clock[0])
    event['PositionTicks'] = 35*10_000_000
    clock[0] = BASE+35;sampler._progress('u','device',event,clock[0])
    assert seconds(db) == 30  # initial raw Sessions explicitly reported rate=1
    event['PositionTicks'] = 70*10_000_000
    clock[0] = BASE+70;sampler._progress('u','device',event,clock[0])
    assert seconds(db) == 30 and diagnosis(db)['reason'] == 'rate_unknown'
    assert diagnosis(db)['rate'] is None


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
