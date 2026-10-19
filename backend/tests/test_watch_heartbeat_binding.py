import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from test_progress_watch import diagnosis, payload, seconds
from test_verified_watch import BASE, feed, session
from test_verified_watch import env as env  # noqa: PLC0414

from app.core.db import Database
from app.main import app
from app.modules.economy_rules import watch_progress
from app.modules.streams import StreamAdmission
from app.modules.usage import UsageSampler


def bind(db, play='run', issued=BASE, item='film'):
    db.execute('INSERT INTO watch_playbacks(user_id,play_id,session_id,device_id,item_id,issued_at,metadata_json) '
               'VALUES(?,?,?,?,?,?,?)', ('u',play,'s','device',item,issued,
                                        json.dumps({'client':'RodelPlayer','item_name':'Demo','item_type':'Movie'})))


def report(sampler, clock, offset, position, **changes):
    clock[0] = BASE+offset
    event = payload(position, **changes)
    event.pop('PlaybackRate')
    sampler._progress('u', 'device', event, clock[0])


@pytest.fixture
def bound(env, monkeypatch):
    db, members, sampler, stats = env
    clock = [BASE]
    monkeypatch.setattr('app.modules.usage.time.time', lambda: clock[0])
    bind(db)
    return db, members, sampler, stats, clock


@pytest.mark.parametrize('speed', [0.5, 1, 1.25, 1.5, 2])
def test_missing_rate_counts_elapsed_and_all_views_agree(bound, speed):
    db, _, sampler, stats, clock = bound
    report(sampler,clock,5,5*speed)
    report(sampler,clock,15,15*speed)
    report(sampler,clock,25,25*speed)
    assert seconds(db) == 20
    assert stats.watch_summary('u')['seconds_24h'] == 20
    assert stats.top_watchers()[0]['seconds'] == 20
    assert stats.top_titles()[0]['seconds'] == 20
    assert stats.member_detail('u')['recent_plays'][0]['seconds'] == 20
    assert all(r['playback_rate'] is None for r in db.query('SELECT playback_rate FROM watch_verified_samples'))
    assert diagnosis(db)['binding'] == 'issuance'
    assert watch_progress(db,'u',clock[0])[1] == 20
    assert not db.query('SELECT * FROM usage_daily')


def test_real_rodel_missing_rate_reports_replay_without_default_one(bound):
    db, _, sampler, _, clock = bound
    samples = [(0,1168.125),(10.027871,1180.638),(20.058451,1193.1499999),(30.100906,1205.663)]
    feed(sampler,BASE,1168.125,rate=1)
    for offset, position in samples:
        report(sampler,clock,offset,position)
    assert seconds(db) == pytest.approx(30.100906,abs=0.001)
    assert diagnosis(db)['rate'] is None


def test_unknown_rate_stall_pause_seek_gap_and_rewind_do_not_backfill(bound):
    db, _, sampler, _, clock = bound
    report(sampler,clock,5,5)
    report(sampler,clock,15,15)
    report(sampler,clock,25,15)
    report(sampler,clock,35,15,IsPaused=True)
    report(sampler,clock,45,15,IsPaused=False)
    report(sampler,clock,55,25)
    assert seconds(db) == 20
    assert watch_progress(db,'u',clock[0])[1] == 20
    report(sampler,clock,65,500)
    report(sampler,clock,75,510)
    report(sampler,clock,85,520,EventName='Seek')
    report(sampler,clock,95,530)
    report(sampler,clock,105,540)
    assert seconds(db) == 40
    assert watch_progress(db,'u',clock[0])[1] == 40
    report(sampler,clock,205,640)
    assert seconds(db) == 40
    report(sampler,clock,215,650)
    report(sampler,clock,225,100)
    report(sampler,clock,235,110)
    assert seconds(db) == 60
    assert watch_progress(db,'u',clock[0])[1] == 60


def test_issuance_binding_survives_absent_concurrency_lease_and_cached_other_title(bound):
    db, _, sampler, _, clock = bound
    report(sampler,clock,5,5)
    report(sampler,clock,15,15)
    sampler._sample([session(BASE+20,0,item='old-film',play='old-run')],BASE+20,None)
    report(sampler,clock,25,25)
    assert seconds(db) == 20
    assert sampler._live['s']['item_id'] == 'film'
    assert db.one('SELECT item_id FROM watch_verified_samples')['item_id'] == 'film'
    assert not db.one("SELECT name FROM sqlite_master WHERE name='stream_leases'")


@pytest.mark.parametrize('field,value', [('ItemId','wrong'),('SessionId','wrong'),('PlaySessionId','wrong')])
def test_issued_binding_rejects_wrong_identity_without_changing_state(bound,field,value):
    db, _, sampler, _, clock = bound
    report(sampler,clock,5,5)
    original = json.dumps(sampler._live,sort_keys=True)
    report(sampler,clock,15,15,**{field:value})
    assert seconds(db) == 0
    assert json.dumps(sampler._live,sort_keys=True) == original
    clock[0] = BASE+25
    sampler._progress('u','wrong-device',payload(25),clock[0])
    assert seconds(db) == 0


def test_stop_and_new_play_do_not_resurrect_old_reports_or_erase_new_checkpoint(bound):
    db, _, sampler, _, clock = bound
    report(sampler,clock,5,5)
    report(sampler,clock,35,35)
    bind(db,'new-run',BASE+40,'new-film')
    report(sampler,clock,45,0,PlaySessionId='new-run',ItemId='new-film')
    report(sampler,clock,55,10,PlaySessionId='new-run',ItemId='new-film')
    assert seconds(db) == 40
    assert json.loads(db.one('SELECT state_json FROM watch_checkpoints')['state_json'])['play_id'] == 'new-run'
    report(sampler,clock,65,65)
    sampler._playback_report('u','device',payload(65),True)
    report(sampler,clock,75,30,PlaySessionId='new-run',ItemId='new-film')
    assert seconds(db) == 60
    sampler._playback_report('u','device',payload(30,PlaySessionId='new-run',ItemId='new-film'),True)
    report(sampler,clock,85,40,PlaySessionId='new-run',ItemId='new-film')
    sampler._sample([session(BASE+150,100,item='new-film',play='new-run')],BASE+150,None)
    sampler._sample([session(BASE+180,130,item='new-film',play='new-run')],BASE+180,None)
    assert seconds(db) == 60


def test_binding_closed_between_match_and_commit_does_not_credit(bound,monkeypatch):
    db, _, sampler, _, clock = bound
    report(sampler,clock,5,5)
    original = sampler._record_watch
    def race(*args, **kwargs):
        db.execute('UPDATE watch_playbacks SET closed_at=?',(BASE+15,))
        return original(*args,**kwargs)
    monkeypatch.setattr(sampler,'_record_watch',race)
    report(sampler,clock,15,15)
    assert seconds(db) == 0
    assert sampler._live['s']['report_received_at'] == BASE+5


def test_restart_imports_only_fresh_preexisting_binding_without_history_credit(env,monkeypatch):
    db, members, sampler, _, = env
    clock=[BASE]
    monkeypatch.setattr('app.modules.usage.time.time',lambda:clock[0])
    feed(sampler,BASE,0)
    feed(sampler,BASE+15,15)
    clock[0]=BASE+20
    restored=UsageSampler(db,members,sampler._emby)
    assert db.one('SELECT play_id FROM watch_playbacks')['play_id'] == 'run'
    report(restored,clock,25,25)
    assert seconds(db) == 15
    report(restored,clock,35,35)
    assert seconds(db) == 25


def test_playback_info_records_binding_for_unlimited_member_without_a_seat(env,monkeypatch):
    db, members, sampler, _ = env
    members.set_overrides('u', {'max_streams': 0})
    streams=StreamAdmission(members,sampler._emby)
    raw=session(BASE,0);raw.pop('PlaySessionId')
    monkeypatch.setattr(sampler._emby,'active_sessions_raw',AsyncMock(return_value=[raw]))
    async def issue():return 200,{'PlaySessionId':'issued','MediaSources':[{'RunTimeTicks':36000000000}]}
    admission,code,_=asyncio.run(streams.issue_info('u','device','',issue,item_id='film'))
    assert admission.allowed and code==200
    assert db.one('SELECT play_id FROM watch_playbacks')['play_id']=='issued'
    assert not db.query('SELECT * FROM stream_leases')


def test_http_issue_progress_and_stop_use_same_binding_without_poll_metadata(monkeypatch):
    clock=[BASE]
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.usage,'tick',AsyncMock(return_value={'ok':True}))
        monkeypatch.setattr('app.main.time.time',lambda:clock[0])
        app.state.members.upsert('u','viewer',{'group_id':'standard'})
        raw=session(BASE,0);raw.pop('NowPlayingItem');raw.pop('PlaySessionId')
        monkeypatch.setattr(app.state.emby,'active_sessions_raw',AsyncMock(return_value=[raw]))
        monkeypatch.setattr(app.state.emby,'user_for_token',AsyncMock(return_value='u'))
        monkeypatch.setattr(app.state.emby,'verify_item_access',AsyncMock(return_value=True))
        monkeypatch.setattr(app.state.emby,'playback_info',AsyncMock(return_value=(200,{'PlaySessionId':'run','MediaSources':[]})))
        monkeypatch.setattr(app.state.emby,'report_playback',AsyncMock(return_value=204))
        monkeypatch.setattr(app.state.emby,'report_stopped',AsyncMock(return_value=204))
        headers={'X-Emby-Token':'fixture-only','X-Emby-Device-Id':'device'}
        assert client.post('/api/playback/info/film',headers=headers,json={}).status_code==200
        for offset in (5,15):
            clock[0]=BASE+offset
            event=payload(offset);event.pop('PlaybackRate')
            assert client.post('/api/playback/progress',headers=headers,json=event).status_code==204
        assert seconds(app.state.db)==10
        clock[0]=BASE+20
        assert client.post('/api/playback/stopped',headers=headers,json=payload(15)).status_code==204
        clock[0]=BASE+25
        assert client.post('/api/playback/progress',headers=headers,json=payload(25)).status_code==204
        assert seconds(app.state.db)==10


def test_frozen_heartbeats_reset_unknown_rate_proof_without_backfilling_stall(bound):
    db, _, sampler, _, clock = bound
    for offset, position in [(5,5),(15,15),(25,15),(35,15),(45,25),(55,35)]:
        report(sampler,clock,offset,position)
    assert seconds(db) == 20


def test_unknown_rate_still_rejects_stale_received_heartbeat(bound):
    db, _, sampler, _, clock = bound
    report(sampler,clock,5,5)
    clock[0] = BASE+85
    event = payload(15);event.pop('PlaybackRate')
    sampler._progress('u','device',event,BASE+15)
    assert seconds(db) == 0
    assert diagnosis(db)['reason'] == 'heartbeat_stale'


@pytest.mark.parametrize('with_binding', [True, False])
@pytest.mark.parametrize('with_play_id', [True, False])
def test_successful_stop_persists_tombstone_after_session_disappears(bound,with_binding,with_play_id):
    db, _, sampler, _, clock = bound
    if not with_binding:
        db.execute('DELETE FROM watch_playbacks')
        feed(sampler,BASE,0)
    report(sampler,clock,5,5)
    report(sampler,clock,35,35)
    event = payload(35)
    if not with_play_id:
        event.pop('PlaySessionId')
    clock[0] = BASE+36
    sampler._playback_report('u','device',event,True)
    sampler._sample([],BASE+40,None)
    assert 's' not in sampler._live
    feed(sampler,BASE+45,45)
    report(sampler,clock,50,50)
    report(sampler,clock,60,60)
    feed(sampler,BASE+75,75)
    assert seconds(db) == 30
    assert db.one('SELECT closed_at FROM watch_playbacks')['closed_at'] == BASE+36


def test_cached_explicit_old_play_does_not_replace_fresh_new_report_identity(bound):
    db, _, sampler, _, clock = bound
    db.execute('DELETE FROM watch_playbacks')
    feed(sampler,BASE,0)
    report(sampler,clock,5,5)
    report(sampler,clock,35,35)
    bind(db,'new-run',BASE+40)
    report(sampler,clock,45,0,PlaySessionId='new-run')
    report(sampler,clock,55,10,PlaySessionId='new-run')
    feed(sampler,BASE+60,35)
    assert sampler._live['s']['play_id'] == 'new-run'
    report(sampler,clock,65,65)
    report(sampler,clock,75,75)
    assert seconds(db) == 40
    report(sampler,clock,85,40,PlaySessionId='new-run')
    assert seconds(db) == 70


def test_nullable_rate_migration_preserves_existing_rows_and_totals(tmp_path):
    path=tmp_path/'legacy.db'
    db=Database(path)
    row=('key','u','film','Demo','Movie','','client','DirectPlay',BASE,BASE+10,10,0,10,1)
    db.close()
    conn=sqlite3.connect(path)
    schema=conn.execute("SELECT sql FROM sqlite_master WHERE name='watch_verified_samples'").fetchone()[0]
    conn.execute('DROP TABLE watch_verified_samples')
    conn.execute(schema.replace('playback_rate REAL,','playback_rate REAL NOT NULL,'))
    conn.execute('INSERT INTO watch_verified_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',row)
    conn.execute("INSERT INTO watch_verified_totals VALUES('u',10,?,?)",(BASE,BASE+10))
    conn.commit();conn.close()
    migrated=Database(path)
    assert tuple(migrated.query('SELECT * FROM watch_verified_samples')[0].values())==row
    assert migrated.one('SELECT seconds FROM watch_verified_totals')['seconds']==10
    migrated._migrate()
    assert len(migrated.query('SELECT * FROM watch_verified_samples'))==1
    migrated.execute('INSERT INTO watch_verified_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('new',*row[1:-1],None))
    migrated.close()
