"""Today's verified snapshot, separate commands, posters and non-switching pager."""
import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from test_cola_rankings import decoded
from test_cola_rankings import drawn as drawn_fixture  # noqa: F401
from test_tg_interaction_context import VIEWER, click, command
from test_tg_interaction_context import env as interaction_env  # noqa: F401
from test_verified_watch import BASE, feed
from test_verified_watch import env as watch_env  # noqa: F401
from watch_fixtures import verified

from app.modules.rank_poster import render_rank_poster, render_watch_poster
from app.modules.stats import StatsService, ranking_bounds, ranking_stamp

BEIJING = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 6, 1, 40, tzinfo=BEIJING).timestamp()


def test_today_bounds_and_stamp_are_beijing_even_on_utc_host(monkeypatch):
    old_tz = os.environ.get('TZ')
    try:
        monkeypatch.setenv('TZ', 'UTC')
        time.tzset()
        since, until = ranking_bounds(1, now=NOW, today=True)
        assert since == datetime(2026, 10, 6, tzinfo=BEIJING).timestamp()
        assert until == NOW
        assert ranking_stamp(1, now=NOW, today=True) == '2026-10-06 · 截至 01:40（北京时间）'
    finally:
        if old_tz is None:
            monkeypatch.delenv('TZ')
        else:
            monkeypatch.setenv('TZ', old_tz)
        time.tzset()


def test_today_users_movies_shows_clip_same_window_and_ignore_old_history(request):
    env = request.getfixturevalue('interaction_env')
    stats = StatsService(env.db)
    since, _ = ranking_bounds(1, now=NOW, today=True)
    verified(env.db, 'u1', since - 30, 60, title='Cross midnight', item='m')
    verified(env.db, 'u1', since + 60, 120, title='Movie', item='m2')
    verified(env.db, 'u1', NOW - 30, 60, title='Episode', kind='Episode', series='Series', item='s')
    verified(env.db, 'u2', NOW, 600, title='Future')
    verified(env.db, 'u2', since - 3600, 600, title='Yesterday')
    env.db.execute("INSERT INTO play_events(emby_user_id,item_name,item_type,seconds,started_at,ended_at) "
                   "VALUES('u1','Unverified','Movie',43200,?,?)", (since, NOW))
    board = stats.today_snapshot(now=NOW)
    assert [(r['user_id'], r['seconds']) for r in board['users']] == [('u1', 180)]
    assert [(r['title'], r['seconds']) for r in board['movies']] == [('Movie', 120), ('Cross midnight', 30)]
    assert [(r['title'], r['seconds']) for r in board['shows']] == [('Series', 30)]
    assert sum(r['seconds'] for r in board['movies'] + board['shows']) == 180
    assert all('rank_delta' not in r for r in board['movies'] + board['shows'])
    assert board['stamp'] == ranking_stamp(1, now=NOW, today=True)
    assert env.db.one("SELECT seconds FROM play_events WHERE item_name='Unverified'")['seconds'] == 43200


def test_today_live_playback_pause_resume_and_seek_keep_verified_only(request):
    _, _, sampler, stats = request.getfixturevalue('watch_env')
    start = BASE + 60
    feed(sampler, start, 0)
    feed(sampler, start + 30, 30)
    feed(sampler, start + 60, 30, paused=True)
    feed(sampler, start + 90, 30)
    feed(sampler, start + 120, 60)
    feed(sampler, start + 150, 900)
    board = stats.today_snapshot(now=start + 180)
    assert board['users'][0]['seconds'] == board['movies'][0]['seconds'] == 60
    assert board['shows'] == []


@pytest.mark.parametrize('chat', [-100700, VIEWER])
@pytest.mark.parametrize('text,method,days,today', [
    ('/rank', 'broadcast_watch_rank', 1, False),
    ('/today', 'broadcast_watch_rank', 1, True),
    ('/rank 影片', 'broadcast_rankings', 1, False),
    ('/today 影片', 'broadcast_rankings', 1, True),
    ('/rank 7', 'broadcast_watch_rank', 7, False),
    ('/rank 168', 'broadcast_watch_rank', 7, False),
])
def test_separate_commands_and_legacy_week_route(request, chat, text, method, days, today):
    env = request.getfixturevalue('interaction_env')
    env.bot.broadcast_watch_rank = AsyncMock(return_value=True)
    env.bot.broadcast_rankings = AsyncMock(return_value=True)
    asyncio.run(command(env, text, chat=chat, user=VIEWER))
    target = getattr(env.bot, method)
    target.assert_awaited_once_with(chat, days, **({'today': True} if today else {}))


@pytest.mark.parametrize('text', ['/today 7', '/rank nonsense', '/today 影片 7'])
def test_bad_parameters_explain_usage_instead_of_showing_wrong_day(request, text):
    env = request.getfixturevalue('interaction_env')
    env.bot.broadcast_watch_rank = AsyncMock(return_value=True)
    env.bot.broadcast_rankings = AsyncMock(return_value=True)
    asyncio.run(command(env, text, chat=VIEWER, user=VIEWER))
    body = next(p['text'] for m, p in env.tg.calls if m == 'sendMessage')
    assert '口令参数无效' in body
    assert '/rank' in body and '/today' in body
    env.bot.broadcast_watch_rank.assert_not_awaited()
    env.bot.broadcast_rankings.assert_not_awaited()


@pytest.mark.parametrize('with_photo', [True, False])
def test_today_poster_caption_and_pager_reuse_one_snapshot(request, monkeypatch, with_photo):
    env = request.getfixturevalue('interaction_env')
    board = {'now': NOW, 'stamp': ranking_stamp(1, now=NOW, today=True), 'movies': [], 'shows': [],
             'users': [{'user_id': f'u{i}', 'tg_display_name': f'观众{i:02}', 'seconds': 600-i,
                        'plays': 1} for i in range(23)]}
    calls = []

    def snapshot(**kwargs):
        calls.append(kwargs)
        return board
    env.bot._stats = type('Stats', (), {'today_snapshot': staticmethod(snapshot)})()
    env.bot._watch_rank_poster = AsyncMock(return_value=b'jpeg' if with_photo else None)
    monkeypatch.setattr('app.modules.telegram.time.time', lambda: NOW)

    async def run():
        mid = await command(env, '/today', chat=VIEWER, user=VIEWER)
        assert len(calls) == 1
        env.bot._watch_rank_poster.assert_awaited_once_with(1, today=True, board=board)
        assert '今日观影榜' in env.tg.text(VIEWER, mid)
        assert board['stamp'] in env.tg.text(VIEWER, mid)
        actions = env.tg.actions(VIEWER, mid)
        assert any(b['callback_data'] == 'urank:2_1_today' for b in actions)
        assert not any(b['callback_data'].startswith(('rank:', 'heat:', 'top:')) for b in actions)
        await click(env, 'urank:2_1_today', mid, chat=VIEWER, user=VIEWER)
        method = 'editMessageCaption' if with_photo else 'editMessageText'
        payload = next(p for m, p in env.tg.calls if m == method)
        body = payload['caption'] if with_photo else payload['text']
        assert payload['message_id'] == mid
        assert '观众10' in body and '观众00' not in body
        assert board['stamp'] in body and len(calls) == 1
        env.bot._today_rank_boards[(str(VIEWER), mid)] = (NOW-1, board)
        await click(env, 'urank:3_1_today', mid, chat=VIEWER, user=VIEWER)
        assert any('快照已过期' in p.get('text', '') for m, p in env.tg.calls if m == 'answerCallbackQuery')
        assert len(calls) == 1
    asyncio.run(run())


def test_today_posters_are_dated_and_never_show_daily_movement(request):
    drawn = request.getfixturevalue('drawn_fixture')
    stamp = ranking_stamp(1, now=NOW, today=True)
    rows = [{'title': 'Movie', 'seconds': 120, 'plays': 1, 'comparison_available': True, 'rank_delta': 2}]
    decoded(render_rank_poster(rows, [], today=True, when=stamp), (1200, 2400))
    assert '今日观影榜' in drawn and stamp in drawn
    assert not any('↑' in t or '↓' in t or '新上榜' in t for t in drawn)
    drawn.clear()
    decoded(render_watch_poster([{'tg_display_name': '观众', 'seconds': 120, 'plays': 1}],
                                today=True, when=stamp), (1200, 2400))
    assert '今日观影达人榜' in drawn and stamp in drawn


def test_today_unavailable_is_not_misreported_as_empty(request):
    env = request.getfixturevalue('interaction_env')
    def unavailable(**kwargs):
        raise RuntimeError('snapshot unavailable')
    env.bot._stats = type('Stats', (), {'today_snapshot': staticmethod(unavailable)})()
    board = env.bot._today_board()
    assert board['unavailable']
    assert '暂时不可用' in env.bot._watch_rank_pages(today=True, board=board)[0]
    assert '暂时不可用' in env.bot._rankings_text(today=True, board=board)
    assert asyncio.run(env.bot._watch_rank_poster(1, today=True, board=board)) is None
    assert asyncio.run(env.bot._rankings_poster(1, today=True, board=board)) is None
