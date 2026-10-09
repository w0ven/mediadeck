"""Activity is an observation of verified playback, not access or entitlement."""
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.core.db import Database
from app.modules.activity import DAY, member_activity
from app.modules.member_ops import sort_rows
from app.modules.stats import StatsService

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()


@pytest.fixture
def db(tmp_path):
    store = Database(tmp_path / 'activity.db')
    store.execute("UPDATE meta SET value=? WHERE key='watch_verified_since'", (str(NOW - 60 * DAY),))
    yield store
    store.close()


def member(uid='u', **extra):
    return {'emby_user_id': uid, 'state': 'active', 'created_at': NOW - 60 * DAY, **extra}


def sample(db, start, end, uid='u', key='run', *, total=True):
    db.execute('INSERT INTO watch_verified_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (key, uid, 'film', 'Film', 'Movie', '', 'Test', 'DirectPlay',
                start, end, end - start, 0, end - start, 1))
    if total:
        db.execute('INSERT INTO watch_verified_totals VALUES(?,?,?,?) '
                   'ON CONFLICT(emby_user_id) DO UPDATE SET seconds=seconds+excluded.seconds,'
                   'first_at=MIN(first_at,excluded.first_at),last_at=MAX(last_at,excluded.last_at)',
                   (uid, end - start, start, end))


def activity(db, m=None, **kw):
    return member_activity(db, [m or member()], now=NOW, **kw)['u']


def test_old_playback_new_access_and_pruned_samples_keep_last_playback(db):
    sample(db, NOW - 40 * DAY - 300, NOW - 40 * DAY)
    db.execute('DELETE FROM watch_verified_samples')
    m = member(last_seen_at=NOW, last_activity='2099-01-01T00:00:00Z', last_activity_ts=NOW)
    got = activity(db, m)
    assert got['last_played_at'] == NOW - 40 * DAY
    assert got['playback_activity']['days_since_played'] == 40
    assert got['playback_activity']['status'] == 'inactive'
    rows = [dict(m, **got), dict(member('recent'), last_played_at=NOW - DAY, last_seen_at=1)]
    for key in ('last_seen', 'last_played'):
        assert sort_rows(rows, key, 'desc')[0]['emby_user_id'] == 'recent'
        assert sort_rows([m], key, 'desc') == [m]  # access is not a fallback key


def test_complete_no_record_can_be_candidate_but_missing_history_is_not_filled(db):
    got = activity(db)['playback_activity']
    assert got['status'] == 'inactive'
    assert got['days_since_played'] is None
    assert got['score'] == got['watch_days_30d'] == got['watch_hours_30d'] == 0
    # Legacy visits, estimates and completed wall-clock events are excluded.
    db.execute("INSERT INTO watch_sample_totals VALUES('u',36000,?,?)", (NOW - 36000, NOW))
    db.execute("INSERT INTO play_events(emby_user_id,seconds,started_at,ended_at) VALUES('u',36000,?,?)", (NOW - 36000, NOW))
    assert activity(db)['playback_activity'] == got


@pytest.mark.parametrize('coverage', [0, 4, 29.999])
def test_insufficient_coverage_never_makes_a_candidate_or_unknown_zero(db, coverage):
    db.execute("UPDATE meta SET value=? WHERE key='watch_verified_since'", (str(NOW - coverage * DAY),))
    got = activity(db)['playback_activity']
    assert got['status'] == 'observing' and got['score'] is None
    assert got['observed_days'] == pytest.approx(coverage)
    sample(db, NOW - 40 * DAY - 300, NOW - 40 * DAY)
    got = activity(db)['playback_activity']
    assert got['status'] == 'observing' and got['watch_days_30d'] == 0


@pytest.mark.parametrize('field', ['created_at', 'register_at', 'activated_at'])
def test_known_account_start_limits_observation_without_inferring_activation(db, field):
    m = member(**{field: NOW - 4 * DAY}, updated_at=NOW, applied_at=NOW)
    got = activity(db, m)['playback_activity']
    assert got['status'] == 'observing' and got['observed_days'] == 4
    # Updates, applies and expiry do not invent an activation/start date.
    got = activity(db, member(updated_at=NOW, applied_at=NOW, expires_at=NOW + DAY))['playback_activity']
    assert got['observation_complete'] and got['observed_days'] == 60


def test_pending_is_not_dormant_even_with_complete_coverage(db):
    got = activity(db, member(state='pending'))['playback_activity']
    assert got['status'] == 'pending' and got['reason'] == '未开通' and got['score'] is None


@pytest.mark.parametrize('distance,status', [(13.999, 'ready'), (14, 'inactive'), (21, 'inactive'), (0, 'ready')])
def test_exact_recency_formula_and_candidate_age_gate(db, distance, status):
    end = NOW - distance * DAY
    sample(db, end - 30, end)
    got = activity(db)['playback_activity']
    assert got['score'] == round(60 * 2 ** (-distance / 7) + (15 * 30 / 3600 / 10))
    assert got['status'] == status


def test_future_last_timestamp_has_nonnegative_distance(db):
    db.execute("INSERT INTO watch_verified_totals VALUES('u',30,?,?)", (NOW, NOW + 1))
    assert activity(db)['playback_activity']['days_since_played'] == 0


def test_beijing_midnight_threshold_and_parallel_session_union(db):
    midnight = NOW - 12 * 3600
    sample(db, midnight - 300, midnight + 299, key='a')
    sample(db, midnight - 300, midnight + 299, key='parallel')
    got = activity(db)['playback_activity']
    assert got['watch_days_30d'] == 1  # yesterday 300s, today only 299s
    assert got['watch_hours_30d'] == pytest.approx(599 / 3600)
    sample(db, midnight + 299, midnight + 300, key='adjacent')
    got = activity(db)['playback_activity']
    assert got['watch_days_30d'] == 2 and got['watch_hours_30d'] == pytest.approx(600 / 3600)


def test_rolling_30_day_edges_clip_before_natural_day_threshold(db):
    since = NOW - 30 * DAY
    sample(db, since - 600, since + 299, key='left')
    sample(db, NOW - 299, NOW + 600, key='right')
    got = activity(db)['playback_activity']
    assert got['watch_days_30d'] == 0
    assert got['watch_hours_30d'] == pytest.approx(598 / 3600)
    assert got['as_of'] == got['window_until'] == NOW and got['window_since'] == since


def test_short_clicks_do_not_inflate_watching_days(db):
    for i in range(1, 11):
        sample(db, NOW - i * DAY, NOW - i * DAY + 30, key=str(i))
    got = activity(db)['playback_activity']
    assert got['watch_days_30d'] == 0
    assert got['watch_hours_30d'] == pytest.approx(300 / 3600)


def test_frequency_time_and_score_caps(db):
    for i in range(1, 21):
        sample(db, NOW - i * DAY - 3600, NOW - i * DAY, key=str(i))
    sample(db, NOW - 3600, NOW, key='current')
    got = activity(db)['playback_activity']
    assert got['watch_days_30d'] == 21 and got['watch_hours_30d'] == 21
    assert got['score'] == 100


def test_low_score_threshold_is_strict_and_duration_can_prevent_candidate(db):
    # D=14 gives 15 recency points; 10h adds 15, one active day adds 3.125.
    sample(db, NOW - 14 * DAY - 10 * 3600, NOW - 14 * DAY)
    got = activity(db)['playback_activity']
    assert got['score'] == 33 and got['status'] == 'ready'
    # Exactly 30 after rounding is not <30.
    db.execute('DELETE FROM watch_verified_samples')
    sample(db, NOW - 14 * DAY - 28500, NOW - 14 * DAY, key='threshold')
    got = activity(db)['playback_activity']
    assert got['score'] == 30 and got['status'] == 'ready'


def test_sample_can_supply_newer_last_timestamp_without_visit_fallback(db):
    sample(db, NOW - 600, NOW - 300, total=False)
    got = activity(db, member(last_seen_at=NOW))
    assert got['last_played_at'] == NOW - 300


@pytest.mark.parametrize('epoch', [None, '', 'garbage', 'nan', '0'])
def test_missing_or_invalid_verification_origin_is_unavailable(db, epoch):
    if epoch is None:
        db.execute("DELETE FROM meta WHERE key='watch_verified_since'")
    else:
        db.execute("UPDATE meta SET value=? WHERE key='watch_verified_since'", (epoch,))
    got = activity(db)['playback_activity']
    assert got['status'] == 'unavailable' and got['score'] is None


@pytest.mark.parametrize('failed_table', ['watch_verified_samples', 'watch_verified_totals', 'meta'])
def test_failed_source_is_unknown_not_a_zero_score(db, monkeypatch, failed_table):
    sample(db, NOW - 300, NOW)
    original_query, original_one = db.query, db.one
    def fail_query(sql, *args):
        if failed_table in sql:
            raise RuntimeError('isolated read failure')
        return original_query(sql, *args)
    def fail_one(sql, *args):
        if failed_table in sql:
            raise RuntimeError('isolated read failure')
        return original_one(sql, *args)
    monkeypatch.setattr(db, 'query', fail_query)
    monkeypatch.setattr(db, 'one', fail_one)
    got = activity(db)
    assert got['playback_activity']['status'] == 'unavailable'
    assert got['playback_activity']['score'] is None
    assert got['last_played_available'] is (failed_table != 'watch_verified_totals')


@pytest.mark.parametrize('reason', ['progress_missing', 'heartbeat_stale', 'pause_unknown', 'baseline', 'observation_gap'])
def test_explicit_current_unverified_playback_cannot_be_candidate(db, reason):
    got = activity(db, live_watch=[{'user_id': 'u', 'watch_reason': reason}])['playback_activity']
    assert got['status'] == 'unavailable' and got['score'] is None
    assert got['reason'] == '当前播放待核验'


def test_sampling_error_and_provider_failure_are_conservative(db):
    got = activity(db, sampling_status={'last_error': 'failure'})['playback_activity']
    assert got['status'] == 'unavailable' and got['score'] is None
    stats = StatsService(db)
    def failed():
        raise RuntimeError('failure')
    stats.bind_live_watch(failed)
    assert stats.member_activity([member()], now=NOW)['u']['playback_activity']['status'] == 'unavailable'
    # Normal empty/paused current view need not erase known historical coverage.
    assert activity(db, live_watch=[{'user_id': 'u', 'watch_reason': 'paused'}])['playback_activity']['status'] == 'inactive'


def test_batch_90k_samples_182_members_is_bounded_and_read_only(db, monkeypatch):
    rows = [member(str(i)) for i in range(182)]
    with db.write() as conn:
        conn.executemany('INSERT INTO watch_verified_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         ((str(i), str(i % 130), 'film', 'Film', 'Movie', '', '', '',
                           NOW - 30 * (i // 130 + 1), NOW - 30 * (i // 130), 30, 0, 30, 1)
                          for i in range(90000)))
    original = db.query
    queries = []
    def track(sql, *args):
        queries.append(sql)
        return original(sql, *args)
    monkeypatch.setattr(db, 'query', track)
    before = db.one('SELECT COUNT(*) AS n FROM watch_verified_samples')['n']
    begin = time.monotonic()
    result = member_activity(db, rows, now=NOW)
    elapsed = time.monotonic() - begin
    assert len(result) == 182 and len(queries) <= 4  # one() may call query()
    assert elapsed < 5, elapsed
    assert db.one('SELECT COUNT(*) AS n FROM watch_verified_samples')['n'] == before
    assert all(r['playback_activity']['as_of'] == NOW for r in result.values())
    assert result['0']['playback_activity']['watch_hours_30d'] > 0
