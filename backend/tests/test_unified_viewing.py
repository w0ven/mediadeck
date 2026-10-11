"""One recorded read model; original verified/financial ledgers stay separate."""
import sqlite3
from datetime import datetime

import pytest

from app.core.db import Database
from app.modules import viewing
from app.modules.member_ops import sort_rows
from app.modules.plugins_builtin import ViewingReportPlugin, _summarise, _top_titles
from app.modules.stats import StatsService

NOW = datetime(2026, 10, 12, 8, tzinfo=viewing.BEIJING).timestamp()


@pytest.fixture
def model(tmp_path, monkeypatch):
    monkeypatch.setattr('app.modules.stats.time.time', lambda: NOW)
    db = Database(tmp_path/'viewing.db')
    db.execute("UPDATE meta SET value=? WHERE key='watch_verified_since'", (str(NOW-30*86400),))
    yield db, StatsService(db)
    db.close()


def sample(db, key, start, seconds, *, verified=True):
    end = start+seconds
    db.execute('INSERT INTO watch_samples VALUES(?,?,?,?,?)', (key, 'u', start, end, seconds))
    db.execute('INSERT INTO watch_sample_totals VALUES(?,?,?,?) ON CONFLICT(emby_user_id) '
               'DO UPDATE SET seconds=seconds+excluded.seconds,last_at=excluded.last_at',
               ('u', seconds, start, end))
    if verified:
        db.execute('INSERT INTO watch_verified_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (key, 'u', 'movie', 'Example Film', 'Movie', '', 'Player', 'DirectPlay',
                    start, end, seconds, 0, seconds, 1))
        db.execute('INSERT INTO watch_verified_totals VALUES(?,?,?,?) ON CONFLICT(emby_user_id) '
                   'DO UPDATE SET seconds=seconds+excluded.seconds,last_at=excluded.last_at',
                   ('u', seconds, start, end))


def test_lifetime_keeps_scalars_deduplicates_mirrors_and_does_not_allocate(model):
    db, stats = model
    db.execute("INSERT INTO watch_legacy_baselines VALUES('u',3600,?,?, 'source')", (NOW-90*86400, NOW-60*86400))
    db.execute("INSERT INTO play_events(emby_user_id,seconds,started_at,ended_at) VALUES('u',300,?,?)",
               (NOW-40*86400, NOW-40*86400+300))
    # Durable total whose detail was pruned: it must not reset or gain a date.
    db.execute("INSERT INTO watch_sample_totals VALUES('u',1000,?,?)", (NOW-35*86400, NOW-34*86400))
    sample(db, 'live', NOW-3600, 120)
    db.execute("INSERT INTO play_events(emby_user_id,seconds,started_at,ended_at,sampled,watch_key) "
               "VALUES('u',120,?,?,1,'live')", (NOW-3600, NOW-3480))
    summary = stats.watch_summary('u')
    assert summary['recorded_seconds'] == 5020
    assert summary['seconds_24h'] == summary['seconds_30d'] == 120
    assert stats.watch_window(NOW-86400, NOW, 'u')['seconds'] == 120
    assert db.one("SELECT seconds FROM watch_totals WHERE emby_user_id='u'")['seconds'] == 300
    detail = stats.member_detail('u')
    assert detail['viewing']['seconds'] == 120 and detail['viewing']['plays'] == 1
    assert len(detail['recent_plays']) == 1
    db.execute('DELETE FROM watch_samples')
    db.execute('DELETE FROM watch_verified_samples')
    assert stats.watch_summary('u')['recorded_seconds'] == 5020
    assert stats.member_detail('u')['viewing']['seconds'] == 0


def test_previous_week_is_beijing_monday_to_monday():
    bounds = viewing.previous_week(NOW)
    assert datetime.fromtimestamp(bounds['since'], viewing.BEIJING).isoformat() == '2026-10-05T00:00:00+08:00'
    assert datetime.fromtimestamp(bounds['until'], viewing.BEIJING).isoformat() == '2026-10-12T00:00:00+08:00'
    assert viewing.previous_week(bounds['until']-1)['until'] == bounds['since']
    assert viewing.previous_week(bounds['until']) == bounds


def test_week_seconds_and_all_session_counts_precede_rounding_and_detail_limits(model):
    db, stats = model
    bounds = viewing.previous_week(NOW)
    for i in range(123):
        sample(db, f'run{i}', bounds['since']+i*30, 1)
    # Wrong completion counts are not the viewing report's session definition.
    db.execute("INSERT INTO usage_daily(day,emby_user_id,seconds,plays) VALUES('2026-10-05','u',10000,7)")
    sample(db, 'this-week', bounds['until']+60, 500)
    report = stats.report_detail('u', now=NOW)
    data = report['viewing']
    assert data['seconds'] == 123 and data['plays'] == 123
    assert sum(p['seconds'] for p in report['series']) == 123
    assert sum(p['plays'] for p in report['series']) == 123
    assert sum(t['plays'] for t in data['titles']) == 123
    assert len(report['recent_plays']) == 50
    hours, plays, traffic = _summarise(report)
    assert hours == round(123/3600, 1) and plays == 123
    assert _top_titles(report)[0]['plays'] == 123
    text = ViewingReportPlugin._text('周报', 7, hours, plays, traffic, report)
    assert bounds['label'] in text and '123 次' in text and '近 7 天' not in text
    assert '累计观看' in text


def test_old_event_cross_boundary_is_not_fabricated_and_distinct_sessions_still_add(model):
    db, stats = model
    db.execute("INSERT INTO play_events(emby_user_id,seconds,started_at,ended_at) VALUES('u',100,?,?)",
               (NOW-1000, NOW-100))
    rows = viewing.records(db, 'u', NOW-500, NOW)
    clipped = viewing.window(rows, NOW-500, NOW)
    assert clipped['seconds'] == 0 and clipped['incomplete']
    full = viewing.window(rows, NOW-1100, NOW)
    assert full['seconds'] == 100 and not full['incomplete']
    sample(db, 'one', NOW-50, 30)
    sample(db, 'two', NOW-50, 30)
    assert stats.watch_summary('u')['seconds_24h'] == 160
    assert stats.watch_window(NOW-60, NOW, 'u')['seconds'] == 60


def test_known_window_without_precise_daily_split_has_explicit_remainder(model):
    db, stats = model
    bounds = viewing.previous_week(NOW)
    midnight = bounds['since']+86400
    db.execute("INSERT INTO play_events(emby_user_id,seconds,started_at,ended_at) VALUES('u',100,?,?)",
               (midnight-600, midnight+600))
    sample(db, 'new', midnight+1000, 30)
    report = stats.report_detail('u', now=NOW)
    assert report['viewing']['seconds'] == 130
    assert sum(day['seconds'] for day in report['series']) == 30
    assert report['viewing']['daily_unallocated_seconds'] == 100
    assert report['viewing']['daily_incomplete']
    assert sum(day['plays'] for day in report['series']) == report['viewing']['plays'] == 2


def test_import_details_without_title_are_not_invented(model):
    db, stats = model
    db.execute("INSERT INTO watch_legacy_events VALUES('old','u',60,?,?, 'source')", (NOW-120, NOW-60))
    report = stats.member_detail('u')
    assert report['viewing']['seconds'] == 60
    assert report['viewing']['plays'] == 1 and not report['viewing']['titles']
    assert report['viewing']['unlocated_title_plays'] == 1
    assert report['recent_plays'][0]['item_name'] == ''
    assert not db.query('SELECT * FROM watch_verified_samples')


def test_recent_reporting_is_readonly_existence_and_failure_unknown(model, tmp_path):
    db, _ = model
    path = tmp_path/'reporting.db'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE PlaybackActivity(UserId,DateCreated,PlayDuration)')
        c.execute('INSERT INTO PlaybackActivity VALUES(?,?,?)', ('u', '2026-10-12 07:50:00', 999999))
    stats = StatsService(db, history_path=str(path))
    member = {'emby_user_id': 'u', 'created_at': NOW-90*86400, 'status': 'active'}
    result = stats.member_activity([member], now=NOW)['u']
    assert result['recent_playback']['sources'] == ['Emby播放记录']
    assert result['last_played_at'] == NOW-600
    assert result['playback_activity']['watch_hours_30d'] == 0
    assert result['playback_activity']['status'] != 'inactive'
    assert stats.watch_summary('u')['recorded_seconds'] == 0
    assert not db.query('SELECT * FROM watch_verified_totals')
    stats._history_path = str(tmp_path/'missing.db')
    result = stats.member_activity([member], now=NOW)['u']
    assert result['last_played_at'] is None
    assert result['recent_playback']['history_status'] == 'unavailable'
    assert not (tmp_path/'missing.db').exists()


@pytest.mark.parametrize('order,expected', [('asc',['old','new','unknown']), ('desc',['new','old','unknown'])])
def test_unknown_playback_sorts_last_in_both_directions(order, expected):
    rows = [{'username':'unknown','last_played_at':None}, {'username':'new','last_played_at':NOW},
            {'username':'old','last_played_at':NOW-86400}]
    for key in ('last_seen','last_played'):
        assert [r['username'] for r in sort_rows(rows, key, order)] == expected
