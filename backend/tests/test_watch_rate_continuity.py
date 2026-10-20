import json

import pytest
from test_progress_watch import diagnosis, seconds, send
from test_progress_watch import progress as progress  # noqa: PLC0414
from test_verified_watch import BASE, feed
from test_verified_watch import env as env  # noqa: PLC0414

from app.modules.usage import UsageSampler


@pytest.mark.parametrize('speed', [1.25, 1.5, 2, 3, 4])
def test_repeated_faster_progress_with_default_rate_counts_elapsed(progress, speed):
    db, _, sampler, stats, clock = progress
    for offset in (5, 15, 25):
        send(sampler, clock, offset, offset*speed)
    assert seconds(db) == 0
    for offset in (35, 45, 55):
        send(sampler, clock, offset, offset*speed)
    assert seconds(db) == 30
    assert stats.watch_summary('u')['recorded_seconds'] == 30
    assert stats.top_watchers()[0]['seconds'] == 30
    assert stats.top_titles()[0]['seconds'] == 30
    assert diagnosis(db)['timing_basis'] == 'continuous_progress'
    assert diagnosis(db)['rate'] == 1
    assert all(row['playback_rate'] is None for row in db.query('SELECT playback_rate FROM watch_verified_samples'))
    assert not db.query('SELECT * FROM usage_daily')
    assert db.one("SELECT traffic_used_bytes FROM members WHERE emby_user_id='u'")['traffic_used_bytes'] == 0


def test_report_arrival_jitter_does_not_discard_whole_intervals(progress):
    db, _, sampler, _, clock = progress
    send(sampler, clock, 5, 10, PlaybackRate=2)
    send(sampler, clock, 37, 70, PlaybackRate=2)
    assert seconds(db) == 30
    send(sampler, clock, 65, 130, PlaybackRate=2)
    assert seconds(db) == 58
    send(sampler, clock, 95, 190, PlaybackRate=2)
    assert seconds(db) == 88


@pytest.mark.parametrize('changes', [{'IsSeeking': True}, {'EventName': 'Seek'}, {'IsPaused': True}])
def test_explicit_seek_and_pause_reset_continuity(progress, changes):
    db, _, sampler, _, clock = progress
    for offset in (5, 15, 25, 35):
        send(sampler, clock, offset, offset*2)
    assert seconds(db) == 10
    send(sampler, clock, 45, 90, **changes)
    for offset in (55, 65, 75):
        send(sampler, clock, offset, offset*2)
    assert seconds(db) == 10
    send(sampler, clock, 85, 170)
    assert seconds(db) == 20


def test_single_small_seek_is_not_misreported_speed(progress):
    db, _, sampler, _, clock = progress
    send(sampler, clock, 5, 5)
    send(sampler, clock, 15, 25)
    assert seconds(db) == 0
    send(sampler, clock, 25, 35)
    assert seconds(db) == 10
    send(sampler, clock, 35, 65)
    send(sampler, clock, 45, 80)
    send(sampler, clock, 55, 110)
    assert seconds(db) == 10


def test_stall_rewind_large_jump_gap_and_rate_change_reset_continuity(progress):
    db, _, sampler, _, clock = progress
    for offset in (5, 15, 25, 35):
        send(sampler, clock, offset, offset*2)
    assert seconds(db) == 10
    send(sampler, clock, 45, 70)
    send(sampler, clock, 55, 50)
    send(sampler, clock, 65, 500)
    send(sampler, clock, 135, 640)
    send(sampler, clock, 145, 660, PlaybackRate=2)
    assert seconds(db) == 10
    send(sampler, clock, 155, 680, PlaybackRate=2)
    assert seconds(db) == 20


def test_polling_never_learns_speed_from_cached_progress(progress):
    db, _, sampler, _, _ = progress
    for offset in (10, 20, 30, 40, 50):
        feed(sampler, BASE+offset, offset*2)
    assert seconds(db) == 0


def test_slow_progress_does_not_credit_hidden_buffering(progress):
    db, _, sampler, _, clock = progress
    for offset in (5, 35, 65, 95):
        send(sampler, clock, offset, offset*0.5)
    assert seconds(db) == 0


def test_misreported_speed_with_bounded_arrival_jitter(progress):
    db, _, sampler, _, clock = progress
    for offset, position in ((5, 10), (17, 30), (25, 50), (35, 70), (47, 90), (55, 110)):
        send(sampler, clock, offset, position)
    assert seconds(db) == 30


def test_restart_cannot_restore_continuity_candidates_or_backfill(progress):
    db, members, sampler, _, clock = progress
    for offset in (5, 15, 25, 35):
        send(sampler, clock, offset, offset*2)
    assert seconds(db) == 10
    restarted = UsageSampler(db, members, sampler._emby)
    for offset in (45, 55, 65):
        send(restarted, clock, offset, offset*2)
    assert seconds(db) == 10
    send(restarted, clock, 75, 150)
    assert seconds(db) == 20


def test_duplicate_and_poll_reports_cannot_credit_same_interval_twice(progress):
    db, _, sampler, _, clock = progress
    for offset in (5, 15, 25, 35, 45):
        send(sampler, clock, offset, offset*2)
        send(sampler, clock, offset+1, offset*2)
        feed(sampler, BASE+offset+2, offset*2)
    assert seconds(db) == 20
    assert diagnosis(db)['counts']['duplicate_progress'] == 5


def test_continuity_diagnostics_preserve_reported_rate(progress):
    db, _, sampler, _, clock = progress
    for offset in (5, 15, 25, 35):
        send(sampler, clock, offset, offset*2)
    state = json.loads(db.one('SELECT state_json FROM watch_checkpoints')['state_json'])
    assert state['watch_observation']['rate'] == 1
    assert diagnosis(db)['credited_seconds'] == 10
