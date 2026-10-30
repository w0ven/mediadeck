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
    for offset in (5, 15):
        send(sampler, clock, offset, offset*speed)
    assert seconds(db) == 0  # one faster interval alone could be a seek
    for offset in (25, 35, 45, 55):
        send(sampler, clock, offset, offset*speed)
    assert seconds(db) == 40
    assert stats.watch_summary('u')['recorded_seconds'] == 40
    assert stats.top_watchers()[0]['seconds'] == 40
    assert stats.top_titles()[0]['seconds'] == 40
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
    assert seconds(db) == 20
    send(sampler, clock, 45, 90, **changes)
    for offset in (55, 65):
        send(sampler, clock, offset, offset*2)
    assert seconds(db) == 20
    send(sampler, clock, 75, 150)
    assert seconds(db) == 30


def test_first_interval_and_jumps_beyond_4x_do_not_credit(progress):
    db, _, sampler, _, clock = progress
    send(sampler, clock, 5, 5)
    send(sampler, clock, 15, 25)  # a jump right after the baseline is not yet a run
    assert seconds(db) == 0
    send(sampler, clock, 25, 35)
    assert seconds(db) == 10
    send(sampler, clock, 35, 200)
    assert seconds(db) == 10 and diagnosis(db)['reason'] == 'seek_or_buffering'
    send(sampler, clock, 45, 210)
    send(sampler, clock, 55, 235)  # inside a run a faster interval counts elapsed only
    assert seconds(db) == 30


def test_stall_rewind_large_jump_gap_and_rate_change_reset_continuity(progress):
    db, _, sampler, _, clock = progress
    for offset in (5, 15, 25, 35):
        send(sampler, clock, offset, offset*2)
    assert seconds(db) == 20
    send(sampler, clock, 45, 70)
    send(sampler, clock, 55, 50)
    send(sampler, clock, 65, 500)
    send(sampler, clock, 135, 640)
    send(sampler, clock, 145, 660, PlaybackRate=2)
    assert seconds(db) == 20
    send(sampler, clock, 155, 680, PlaybackRate=2)
    assert seconds(db) == 30


def test_polling_never_learns_speed_from_cached_progress(progress):
    db, _, sampler, _, _ = progress
    for offset in (10, 20, 30, 40, 50):
        feed(sampler, BASE+offset, offset*2)
    assert seconds(db) == 0


def test_slow_progress_credits_only_played_content(progress):
    db, _, sampler, _, clock = progress
    for offset in (5, 35, 65, 95):
        send(sampler, clock, offset, offset*0.5)
    assert seconds(db) == 45  # 15s of content per 30s; buffering is not credited
    assert diagnosis(db)['timing_basis'] == 'content_progress'
    assert {row['playback_rate'] for row in db.query('SELECT playback_rate FROM watch_verified_samples')} == {1}


def test_misreported_speed_with_bounded_arrival_jitter(progress):
    db, _, sampler, _, clock = progress
    for offset, position in ((5, 10), (17, 30), (25, 50), (35, 70), (47, 90), (55, 110)):
        send(sampler, clock, offset, position)
    assert seconds(db) == 38


def replay(sampler, clock, steps, start=5):
    offset, position = start, 0.0
    send(sampler, clock, offset, position)
    for elapsed, moved in steps:
        offset += elapsed
        position += moved
        send(sampler, clock, offset, position)
    return offset - start


# Report intervals (seconds elapsed, seconds of content) captured on 2026-10-31,
# every client reporting PlaybackRate=1.
SWITCHING_1X_2X = [(10, 10)]*3 + [(9.5, 13), (9.5, 20), (10, 17), (10.8, 10), (9.1, 10), (10.4, 17), (10, 19),
                                 (10, 18), (9.8, 20), (10.5, 20), (9.8, 20), (10.2, 20), (9.4, 19), (10.8, 13)]
NEAR_1_15X_WHOLE_SECONDS = [(10, 10), (10, 11), (10, 10), (10.5, 11), (9.5, 11), (10.9, 10), (9.5, 5), (10.6, 10),
                            (9.4, 13), (10.7, 12), (9.6, 13), (9.9, 16), (10.4, 10), (9, 16), (10.5, 15)]
ARRIVAL_JITTER_1X = [(10, 10), (13.5, 10), (6.5, 10), (10, 10)]


@pytest.mark.parametrize('steps,minimum', [(SWITCHING_1X_2X, 0.95), (NEAR_1_15X_WHOLE_SECONDS, 0.9),
                                           (ARRIVAL_JITTER_1X, 0.9)])
def test_speed_switches_whole_second_positions_and_arrival_jitter_keep_credit(progress, steps, minimum):
    db, _, sampler, _, clock = progress
    wall = replay(sampler, clock, steps)
    assert minimum * wall <= seconds(db) <= wall


def test_restart_cannot_restore_continuity_candidates_or_backfill(progress):
    db, members, sampler, _, clock = progress
    for offset in (5, 15, 25, 35):
        send(sampler, clock, offset, offset*2)
    assert seconds(db) == 20
    restarted = UsageSampler(db, members, sampler._emby)
    for offset in (45, 55):
        send(restarted, clock, offset, offset*2)
    assert seconds(db) == 20
    send(restarted, clock, 65, 130)
    assert seconds(db) == 30


def test_duplicate_and_poll_reports_cannot_credit_same_interval_twice(progress):
    db, _, sampler, _, clock = progress
    for offset in (5, 15, 25, 35, 45):
        send(sampler, clock, offset, offset*2)
        send(sampler, clock, offset+1, offset*2)
        feed(sampler, BASE+offset+2, offset*2)
    assert seconds(db) == 30
    assert diagnosis(db)['counts']['duplicate_progress'] == 5


def test_continuity_diagnostics_preserve_reported_rate(progress):
    db, _, sampler, _, clock = progress
    for offset in (5, 15, 25, 35):
        send(sampler, clock, offset, offset*2)
    state = json.loads(db.one('SELECT state_json FROM watch_checkpoints')['state_json'])
    assert state['watch_observation']['rate'] == 1
    assert diagnosis(db)['credited_seconds'] == 10
