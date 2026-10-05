"""Normal progress is the only evidence admitted to every watch-time view."""
import json
from datetime import UTC, datetime

import pytest

from app.adapters.mock import MockEmby
from app.core.db import Database
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.stats import StatsService, ranking_bounds
from app.modules.usage import UsageSampler

BASE = 1_800_000_000


def session(at, pos, *, paused=False, rate=1, item='film', play='run', heartbeat=None):
    return {'Id': 's', 'UserId': 'u', 'UserName': 'viewer', 'DeviceId': 'device',
            'Client': 'TestClient', 'PlaySessionId': play,
            'LastActivityDate': datetime.fromtimestamp(at if heartbeat is None else heartbeat, UTC).isoformat(),
            'NowPlayingItem': {'Id': item, 'Name': 'Demo', 'Type': 'Movie', 'RunTimeTicks': 3600*10_000_000},
            'PlayState': {'IsPaused': paused, 'PositionTicks': pos*10_000_000, 'PlaybackRate': rate}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr('app.modules.stats.time.time', lambda: BASE+120)
    db = Database(tmp_path/'watch.db')
    db.execute("UPDATE meta SET value=? WHERE key='watch_verified_since'", (str(BASE),))
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    members.upsert('u', 'viewer', {'group_id': 'standard'})
    sampler = UsageSampler(db, members, MockEmby())
    stats = StatsService(db)
    stats.bind_live_watch(sampler.live_watch)
    yield db, members, sampler, stats
    db.close()


def feed(sampler, at, pos, **kw):
    sampler._sample([session(at, pos, **kw)], at, None)


def test_normal_pause_resume_seek_and_every_view_agree(env):
    db, _, sampler, stats = env
    feed(sampler, BASE, 0)
    feed(sampler, BASE+30, 30)
    feed(sampler, BASE+60, 30, paused=True)
    feed(sampler, BASE+90, 30)  # resume: establish baseline, don't backfill pause
    feed(sampler, BASE+120, 60)
    assert stats.watch_summary('u')['recorded_seconds'] == 60
    assert stats.top_watchers()[0]['seconds'] == 60
    assert stats.top_titles()[0]['seconds'] == 60  # still playing: no stop needed
    assert stats.top_titles_split()[0][0]['seconds'] == 60
    assert stats.recent_watches('u')[0]['seconds'] == 60
    assert stats.member_detail('u')['recent_plays'][0]['seconds'] == 60
    feed(sampler, BASE+150, 900)  # forward seek is not 840 seconds watched
    feed(sampler, BASE+180, 930)
    feed(sampler, BASE+210, 100)  # rewind is not negative or duplicated time
    feed(sampler, BASE+240, 130)
    assert db.one('SELECT seconds FROM watch_verified_totals')['seconds'] == 120


@pytest.mark.parametrize('variant', ['zero', 'frozen', 'missing_pos', 'paused', 'unknown_pause',
                                     'old_heartbeat', 'missing_heartbeat', 'invalid_pos', 'nan', 'no_item'])
def test_invalid_evidence_never_accrues(env, variant):
    db, _, sampler, _ = env
    for offset in (0, 30, 60, 3600, 12*3600):
        s = session(BASE+offset, 0 if variant in ('zero', 'frozen') else offset)
        if variant == 'missing_pos': s['PlayState'].pop('PositionTicks')
        if variant == 'paused': s['PlayState']['IsPaused'] = True
        if variant == 'unknown_pause': s['PlayState'].pop('IsPaused')
        if variant == 'old_heartbeat': s['LastActivityDate'] = session(BASE-600, 0)['LastActivityDate']
        if variant == 'missing_heartbeat': s.pop('LastActivityDate')
        if variant == 'invalid_pos': s['PlayState']['PositionTicks'] = 7200*10_000_000
        if variant == 'nan': s['PlayState']['PositionTicks'] = float('nan')
        if variant == 'no_item': s.pop('NowPlayingItem')
        sampler._sample([s], BASE+offset, None)
    assert not db.query('SELECT * FROM watch_verified_samples')
    assert not db.query('SELECT * FROM watch_verified_totals')


@pytest.mark.parametrize('rate', [0.5, 1, 1.5, 2])
def test_playback_speed_counts_wall_time_not_content_distance(env, rate):
    db, _, sampler, _ = env
    feed(sampler, BASE, 0, rate=rate)
    feed(sampler, BASE+30, 30*rate, rate=rate)
    assert db.one('SELECT seconds FROM watch_verified_totals')['seconds'] == 30


def test_cached_heartbeat_polling_gap_and_replacement_do_not_backfill(env):
    db, _, sampler, _ = env
    feed(sampler, BASE, 0)
    feed(sampler, BASE+30, 0, heartbeat=BASE)
    feed(sampler, BASE+60, 60)  # delayed evidence: not 60s credited from original baseline
    assert db.one('SELECT seconds FROM watch_verified_totals')['seconds'] == 30
    feed(sampler, BASE+660, 660)
    assert db.one('SELECT seconds FROM watch_verified_totals')['seconds'] == 30
    feed(sampler, BASE+690, 690)
    feed(sampler, BASE+720, 720, play='replacement')
    feed(sampler, BASE+750, 750, play='replacement')
    assert db.one('SELECT seconds FROM watch_verified_totals')['seconds'] == 90


def test_short_pause_between_polls_is_not_wall_time(env):
    db, _, sampler, _ = env
    feed(sampler, BASE, 0)
    feed(sampler, BASE+30, 20)  # 10s paused or buffering with unpaused endpoints
    assert not db.query('SELECT * FROM watch_verified_samples')
    feed(sampler, BASE+60, 50)
    assert db.one('SELECT seconds FROM watch_verified_totals')['seconds'] == 30


def test_restart_finish_prune_and_duplicate_are_exactly_once(env, monkeypatch):
    db, members, sampler, stats = env
    feed(sampler, BASE, 0)
    feed(sampler, BASE+30, 30)
    state = sampler._live['s']
    evidence = (dict(state['watch_observation'], position=0, activity=BASE), state['watch_observation'])
    sampler._record_watch('s', state, BASE, BASE+30, evidence=evidence)
    assert stats.watch_summary('u')['recorded_seconds'] == 30
    sampler = UsageSampler(db, members, MockEmby())
    feed(sampler, BASE+3600, 1000)
    feed(sampler, BASE+3630, 1030)
    sampler._sample([], BASE+3660, None)
    assert stats.watch_summary('u')['recorded_seconds'] == 60
    assert db.one('SELECT seconds FROM play_events')['seconds'] == 60
    assert not db.query('SELECT * FROM watch_checkpoints')
    db._migrate()
    assert stats.watch_summary('u')['recorded_seconds'] == 60
    monkeypatch.setattr('app.modules.stats.time.time', lambda: BASE+500*86400)
    stats.prune(30)
    assert not db.query('SELECT * FROM watch_verified_samples')
    assert stats.watch_summary('u')['recorded_seconds'] == 60


def test_old_checkpoint_and_history_are_preserved_but_excluded(env):
    db, members, _, stats = env
    db.execute("INSERT INTO play_events(emby_user_id,seconds,started_at,ended_at) VALUES('u',43200,?,?)",
               (BASE-43200, BASE))
    db.execute("INSERT INTO watch_sample_totals VALUES('u',1000,?,?)", (BASE-1000, BASE))
    old = {'user_id':'u','username':'viewer','item_id':'film','seconds':1000,'bytes':0,
           'started_at':BASE-1000,'sampled':True,'watch_key':'old'}
    db.execute('INSERT INTO watch_checkpoints VALUES(?,?)', ('s', json.dumps(old)))
    sampler = UsageSampler(db, members, MockEmby())
    feed(sampler, BASE, 0)
    feed(sampler, BASE+30, 30)
    summary = stats.watch_summary('u')
    assert summary['recorded_seconds'] == summary['seconds_24h'] == 30
    assert summary['historical_unverified_seconds'] == 44200
    assert db.one('SELECT seconds FROM play_events')['seconds'] == 43200
    assert stats.top_watchers()[0]['seconds'] == stats.top_titles()[0]['seconds'] == 30


def test_cross_day_intervals_clip_user_title_and_personal_identically(env):
    _, _, sampler, stats = env
    _, end = ranking_bounds(1, now=BASE+86400)
    feed(sampler, end-20, 0)
    feed(sampler, end+10, 30)
    assert stats.watch_window(end-86400, end, 'u')['seconds'] == 20
    assert stats.top_watchers(since=end-86400, until=end)[0]['seconds'] == 20
    assert stats.top_titles(1, calendar=True, now=end+60)[0]['seconds'] == 20
    assert stats.watch_window(end, end+60, 'u')['seconds'] == 10
