from datetime import datetime

from app.modules.rank_poster import render_rank_poster
from app.modules.stats import ranking_bounds, ranking_stamp
from app.modules.telegram import TelegramBot


def test_rank_poster_renders_jpeg_without_covers() -> None:
    movies = [{"title": "Film One", "item_id": "m1", "plays": 3, "hours": 2}]
    shows = [{"title": "Show One", "item_id": "s1", "plays": 8, "hours": 5}]
    data = render_rank_poster(movies, shows, weekly=False, when="2026-09-09")
    assert data[:2] == b"\xff\xd8"
    assert len(data) > 4000


def test_rank_poster_accepts_broken_cover_bytes() -> None:
    data = render_rank_poster(
        [{"title": "Broken", "item_id": "x", "plays": 1, "hours": 1}],
        [], weekly=True, covers={"x": b"not-an-image"})
    assert data[:2] == b"\xff\xd8"


def test_ranking_bounds_are_complete_local_days() -> None:
    now = datetime(2026, 9, 10, 8, 0, 0).timestamp()
    since, until = ranking_bounds(1, now=now)
    assert datetime.fromtimestamp(since) == datetime(2026, 9, 9, 0, 0, 0)
    assert datetime.fromtimestamp(until) == datetime(2026, 9, 10, 0, 0, 0)
    week_since, week_until = ranking_bounds(7, now=now)
    assert datetime.fromtimestamp(week_since) == datetime(2026, 9, 3, 0, 0, 0)
    assert week_until == until
    assert ranking_stamp(1, now=now) == "2026-09-09"
    assert ranking_stamp(7, now=now) == "2026-09-03 ~ 2026-09-09"


def test_bulletin_split_keeps_newlines() -> None:
    parts = TelegramBot._split_bulletin("a\nb\nc\nd", limit=4)
    assert parts[0] == "a\nb"
    assert "\n".join(parts) == "a\nb\nc\nd"
