"""Independent charts, real period comparisons and all COLA poster families."""
import asyncio
import io
from datetime import datetime
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from test_tg_interaction_context import VIEWER, click, command
from test_tg_interaction_context import env as interaction_env  # noqa: F401

from app.core.db import Database
from app.modules.plugins_builtin import ViewingReportPlugin, _fmt_bytes, _summarise, _top_titles
from app.modules.rank_poster import (
    BRAND,
    fetch_rank_images,
    render_rank_poster,
    render_viewing_poster,
    render_watch_poster,
)
from app.modules.stats import StatsService, ranking_bounds


@pytest.fixture
def chart(tmp_path):
    db = Database(tmp_path / 'charts.db')
    now = datetime(2026, 9, 23, 1, 30).timestamp()  # noqa: DTZ001 - host-local calendar contract
    since, until = ranking_bounds(1, now=now)

    def add(title, kind='Movie', count=1, at=None, series='', user_prefix='user'):
        at = since + 3600 if at is None else at
        with db.write() as conn:
            conn.executemany('INSERT INTO play_events(emby_user_id,item_id,item_name,item_type,series_name,seconds,started_at,ended_at) '
                             'VALUES(?,?,?,?,?,?,?,?)',
                             [(f'{user_prefix}-{i % 4}', f'{kind}-{title}', title, kind, series, 600, at, at + 600)
                              for i in range(count)])
    yield StatsService(db), add, now, since, until
    db.close()


def test_popular_series_cannot_push_movies_out_of_their_own_top10(chart):
    stats, add, now, _, _ = chart
    for index in range(45):
        add(f'Episode {index}', 'Episode', 5, series=f'Show {index:02d}')
    for index in range(13):
        add(f'Film {index:02d}')
    assert all(row['type'] == 'Episode' for row in stats.top_titles(1, 40, calendar=True, now=now))
    movies, shows = stats.top_titles_split(1, 10, calendar=True, now=now)
    assert len(movies) == len(shows) == 10
    assert [row['title'] for row in movies] == [f'Film {i:02d}' for i in range(10)]
    assert all(row['plays'] == 1 for row in movies)


def test_same_title_movie_and_series_have_separate_totals_and_other_types_are_excluded(chart):
    stats, add, now, _, _ = chart
    add('Same title', count=3, series='Incorrect series metadata')
    add('Episode one', 'Episode', 4, series='Same title')
    add('Same title', 'Series', 1)
    add('Same title', 'Audio', 20)
    movies, shows = stats.top_titles_split(1, 10, calendar=True, now=now)
    assert len(movies) == len(shows) == 1
    assert movies[0]['title'] == shows[0]['title'] == 'Same title'
    assert movies[0]['plays'] == 3 and shows[0]['plays'] == 5
    assert movies[0]['viewers'] == 3 and shows[0]['viewers'] == 4
    assert len(stats.top_titles(1, 10, calendar=True, now=now)) == 3


def test_real_shortage_remains_short_and_calendar_boundaries_exclude_future_events(chart):
    stats, add, now, since, until = chart
    add('First', at=since)
    add('Second', at=until - 1)
    add('Before', at=since - 1)
    add('After', at=until)
    movies, shows = stats.top_titles_split(1, 10, calendar=True, now=now)
    assert [row['title'] for row in movies] == ['First', 'Second']
    assert shows == []


def test_movement_uses_previous_equal_calendar_window_with_stable_ties(chart):
    stats, add, now, since, _ = chart
    add('B', count=5, at=since - 3600)
    add('A', count=3, at=since - 3600)
    add('Old', count=1, at=since - 3600)
    add('A', count=5)
    add('B', count=3)
    add('New', count=1)
    movies, shows = stats.title_rankings(1, 10, now=now)
    assert shows == []
    assert [row['title'] for row in movies] == ['A', 'B', 'New']
    assert [row['rank_delta'] for row in movies] == [1, -1, None]
    assert all(row['comparison_available'] for row in movies)
    assert movies[-1]['previous_rank'] is None
    assert stats.title_rankings(1, 10, now=now) == (movies, shows)


def test_absent_previous_chart_does_not_invent_new_or_movement_labels(chart):
    stats, add, now, _, _ = chart
    add('First record')
    row = stats.title_rankings(1, now=now)[0][0]
    assert row['comparison_available'] is False
    assert row['rank_delta'] is None and row['previous_rank'] is None


def test_weekly_movement_compares_seven_days_not_one_day(chart):
    stats, add, now, _, _ = chart
    start, _ = ranking_bounds(7, now=now)
    add('Before', count=5, at=start - 3600)
    add('Current', count=3, at=start - 3600)
    add('Current', count=5, at=start + 3600)
    add('Before', count=3, at=start + 3600)
    rows = stats.title_rankings(7, now=now)[0]
    assert rows[0]['title'] == 'Current' and rows[0]['rank_delta'] == 1


def test_movie_series_and_comparison_share_one_clock_when_midnight_passes(chart, monkeypatch):
    stats, add, _, since, until = chart
    add('Previous movie', at=since - 3600)
    add('Previous episode', 'Episode', at=since - 3600, series='Previous series')
    add('Current movie', at=since + 3600)
    add('Current episode', 'Episode', at=since + 3600, series='Current series')
    moments = iter([until - 0.001, until + 1])
    monkeypatch.setattr('app.modules.stats.time.time', lambda: next(moments, until + 1))
    movies, shows = stats.title_rankings(1)
    assert movies[0]['title'] == 'Previous movie' and shows[0]['title'] == 'Previous series'
    assert not movies[0]['comparison_available'] and not shows[0]['comparison_available']


@pytest.fixture
def drawn(monkeypatch):
    values = []
    original = ImageDraw.ImageDraw.text

    def record(self, xy, text, *args, **kwargs):
        values.append(str(text))
        return original(self, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', record)
    return values


def decoded(data, size):
    with Image.open(io.BytesIO(data)) as image:
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        assert image.size == size and image.format == 'JPEG'


@pytest.mark.parametrize('weekly', [False, True])
def test_title_posters_use_cola_brand_real_counts_and_no_padding(drawn, weekly):
    data = render_rank_poster([
        {'title': 'A film with a long name 测试电影名称', 'item_id': 'one', 'plays': 8, 'viewers': 3},
        {'title': 'Another film', 'plays': 3, 'viewers': 2}], [], weekly=weekly,
        when='2026-09-22', covers={'one': b'broken image'})
    decoded(data, (1200, 2400))
    assert BRAND in drawn
    assert not any('MediaDeck' in item or 'SMP' in item for item in drawn)
    assert '电影 TOP 2' in drawn and '本期暂无剧集播放记录' in drawn
    assert '本期共 2 部 · 展示全部' in drawn and '03' not in drawn
    assert '3人 · 8次' in drawn and '新上榜' not in drawn
    assert ('每周观影榜' if weekly else '每日观影榜') in drawn


@pytest.mark.parametrize('weekly', [False, True])
def test_personal_rank_style_keeps_profile_privacy_whitelist_and_time_semantics(drawn, weekly):
    data = render_watch_poster([
        {'username': 'do-not-leak-login', 'tg_display_name': '可乐', 'tg_user_id': '1',
         'seconds': 3900, 'plays': 3, 'group_id': 'whitelist'},
        {'username': 'another-private-login', 'seconds': 0, 'incomplete': True, 'plays': 1}],
        weekly=weekly, avatars={'1': b'bad image'})
    decoded(data, (1200, 2400))
    assert BRAND in drawn and '可乐' in drawn and '白名单 · 专属' in drawn
    assert '1小时5分' in drawn and '记录不完整' in drawn
    assert not any('private-login' in item or 'do-not-leak' in item for item in drawn)
    assert '未绑定' in drawn


def test_invisible_telegram_names_use_public_handle_or_generic_label(drawn):
    data = render_watch_poster([
        {'username': 'private-login', 'tg_display_name': '\u3164\u200b',
         'tg_username': 'visible_handle', 'tg_user_id': '1', 'seconds': 60},
        {'username': 'another-secret-login', 'tg_display_name': '\u2800',
         'tg_user_id': '2', 'seconds': 30},
    ])
    decoded(data, (1200, 2400))
    assert '@visible_handle' in drawn and 'Telegram用户' in drawn
    assert not any('private-login' in text or 'secret-login' in text for text in drawn)


@pytest.mark.parametrize('empty', [False, True])
def test_personal_weekly_reports_share_brand_and_preserve_real_empty_state(drawn, empty):
    titles = [] if empty else [{'title': '本周喜欢的电影', 'plays': 2, 'item_id': 'x'}]
    data = render_viewing_poster(name='示例会员', label='周报', days=7,
                                 hours=0 if empty else 4.5, plays=0 if empty else 3,
                                 traffic='暂无实测', titles=titles, whitelist=True)
    decoded(data, (1200, 1500 if empty else 1440))
    assert BRAND in drawn and '我的观影周报' in drawn
    assert '私人观影报告 · 仅发送给本人' in drawn
    assert ('本期暂无观影记录' in drawn) is empty
    assert 'MediaDeck' not in ' '.join(drawn)


def test_optional_art_has_bounded_concurrency_deadline_and_preserves_input_order():
    async def run():
        active = peak = 0
        cancelled = []
        async def fetch(item):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                if item == 'slow':
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        cancelled.append(item)
                        raise
                if item == 'bad':
                    raise ValueError('unavailable image')
                await asyncio.sleep(0)
                return item.encode()
            finally:
                active -= 1
        result = await fetch_rank_images(fetch, [{'item_id': item} for item in ('a', 'slow', 'bad', 'b', 'c', 'a')], timeout=0.03)
        assert list(result) == ['a', 'b', 'c'] and peak <= 4
        assert active == 0 and cancelled == ['slow']
    asyncio.run(run())


def test_personal_report_does_not_turn_unavailable_traffic_into_zero():
    hours, plays, total = _summarise({'series': [{'hours': 2.5, 'plays': 3, 'bytes': None}]})
    assert (hours, plays, total) == (2.5, 3, None)
    assert _fmt_bytes(total) == '暂无实测'
    assert _summarise({'series': [{'bytes': 0}]})[2] == 0
    assert _fmt_bytes(0) == '0 B'


def test_personal_favorites_also_keep_movie_and_series_with_the_same_name_separate():
    rows = _top_titles({'recent_plays': [
        {'item_name': '同名', 'item_type': 'Movie', 'item_id': 'movie'},
        {'item_name': 'Episode', 'series_name': '同名', 'item_type': 'Episode', 'item_id': 'episode'},
        {'item_name': 'Next', 'series_name': '同名', 'item_type': 'Episode', 'item_id': 'episode2'},
    ]})
    assert len(rows) == 2
    assert sorted(row['plays'] for row in rows) == [1, 2]


def test_actual_bot_daily_weekly_member_charts_and_private_report_use_the_new_renderer(request):
    env = request.getfixturevalue('interaction_env')
    movie = {'item_id': 'm', 'title': '电影', 'plays': 5, 'viewers': 2, 'hours': 1}
    show = {'item_id': 's', 'title': '剧集', 'plays': 7, 'viewers': 3, 'hours': 2}
    member = {'user_id': 'u1', 'tg_user_id': '903', 'tg_display_name': '观影者',
              'group_id': 'whitelist', 'hours': 2, 'seconds': 7200, 'plays': 3}

    class Stats:
        def top_titles_split(self, **kwargs):
            return [dict(movie)], [dict(show)]
        def title_rankings(self, **kwargs):
            return self.top_titles_split(**kwargs)
        def top_users(self, **kwargs):
            return [dict(member)]

    env.bot._stats = Stats()
    delivered = []

    async def transport(method, fields, files, timeout=40):
        assert method == 'sendPhoto'
        delivered.append(files['photo'][1])
        return await env.tg.call(method, fields)

    env.bot._call_multipart = transport

    async def run():
        for days in (1, 7):
            assert await env.bot.broadcast_rankings('901', days)
            assert await env.bot.broadcast_watch_rank('901', days)
        mid = await command(env, '/start', chat=VIEWER, user=VIEWER)
        for action in ('rank', 'rank:168', 'heat:1', 'heat:7'):
            previous = mid
            mid = await click(env, action, mid, chat=VIEWER, user=VIEWER)
            assert mid != previous
            assert (str(VIEWER), int(mid)) in env.bot._photo_panels
            assert env.tg.message(VIEWER, mid).get('reply_markup')
        report = ViewingReportPlugin(SimpleNamespace(emby=env.bot._emby))
        photo = await report._poster({'tg_display_name': '可乐', 'group_id': 'whitelist'},
                                     '周报', 7, 2.5, 3, None,
                                     {'recent_plays': [{'item_id': 'm', 'item_type': 'Movie', 'item_name': '电影'}]})
        assert photo is not None
        decoded(photo, (1200, 1440))
    asyncio.run(run())
    assert len(delivered) == 8
    for photo in delivered:
        decoded(photo, (1200, 2400))
