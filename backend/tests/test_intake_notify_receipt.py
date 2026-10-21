"""Notifier ACK parsing; real historical success, never a send/replay."""
import os
import time
from datetime import datetime

import pytest

from app.modules.intake import FsReader, IntakeCollector, IntakePaths, parse_notify_tail

# Exact authoritative log record from the owner handoff and production log.
REAL_ACK = '[2026-10-20 05:55:41]     -> -1001234567890 OK'
EXPECTED_TS = datetime.fromisoformat('2026-10-20T05:55:41+08:00').timestamp()


def test_actual_success_not_confused_with_newer_chatter(monkeypatch):
    monkeypatch.setattr(time, 'time', lambda: EXPECTED_TS + 900)
    text = (
        '[2026-10-20 05:55:40]   emby episode exact-path fallback OK 752106 S01: found=[0, 1] paths=20/20\n'
        '[2026-10-20 05:55:40]   flush series: 有你的恋歌 S01 eps=20 emby_id=752106\n'
        + REAL_ACK + '\n'
        '[2026-10-20 06:00:02] received upload-bind-guard event=Alert\n'
        '[2026-10-20 06:00:02]   skip event=Alert\n'
        '[2026-10-20 06:06:08]   dedup skip series 有你的恋歌 S01: eps=[5] already_notified=[0, 1, 2]\n'
        '[2026-10-20 06:07:00] -> -1001234567890 FAIL\n'
    )
    out = parse_notify_tail(text)
    assert out == {'available': True, 'line': REAL_ACK, 'ts': EXPECTED_TS, 'age_seconds': 900.0}


@pytest.mark.parametrize('suffix', ['', ' message_id=123456'])
def test_explicit_ack_with_optional_message_id(suffix):
    out = parse_notify_tail(REAL_ACK + suffix)
    assert out['available'] and out['ts'] == EXPECTED_TS


@pytest.mark.parametrize('body', [
    '-> -1001234567890 FAIL',
    '-> -1001234567890 ERROR',
    '-> -1001234567890 NOT OK',
    '-> -1001234567890 OKAY',
    '-> -1001234567890 OK error=timeout',
    'request -> -1001234567890 OK',
    'ERROR -> -1001234567890 OK',
    '-> -1001234567890 OK message_id=None',
    '-> -1001234567890 OK message_id=0',
    '-> -1001234567890 OK message_id=-1',
    '-> @group OK',
    '-> 0 OK',
    'emby episode exact-path fallback OK 752106 S01',
    'Bot getMe 200 True',
    'HTTP request OK',
    'telegram send err: TimeoutError',
    'telegram receipt method=sendPhoto message_id=1234',
])
def test_non_delivery_is_never_success(body):
    assert not parse_notify_tail('[2026-10-20 05:55:41] ' + body)['available']


@pytest.mark.parametrize('line', ['', '-> -1001234567890 OK',
                                  '[2026-99-20 05:55:41] -> -1001234567890 OK',
                                  '[2026-10-20 25:55:41] -> -1001234567890 OK'])
def test_empty_or_invalid_time_has_no_ack(line):
    assert not parse_notify_tail(line)['available']


@pytest.mark.parametrize('tz', ['UTC', 'Asia/Shanghai'])
def test_beijing_receipt_independent_of_collector_timezone(tz, monkeypatch):
    original = os.environ.get('TZ')
    try:
        os.environ['TZ'] = tz
        time.tzset()
        assert parse_notify_tail(REAL_ACK)['ts'] == EXPECTED_TS
    finally:
        if original is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = original
        time.tzset()


def test_old_receipt_keeps_true_age_and_missing_log_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(time, 'time', lambda: EXPECTED_TS + 7 * 86400)
    out = parse_notify_tail(REAL_ACK)
    assert out['ts'] == EXPECTED_TS and out['age_seconds'] == 7 * 86400
    collector = IntakeCollector(paths=IntakePaths(notify_log=str(tmp_path/'missing.log')), fs=FsReader())
    assert collector.collect_notify()['last_sent']['available'] is False
