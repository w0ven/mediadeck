"""The dashboard shows artwork and playback progress, so the data must carry it.

Two things are pinned down here:

- A session payload without ``ItemId`` can only print a title where the poster
  belongs, and one without runtime cannot draw a progress bar. Both have to
  survive the adapter's reshaping of Emby's session object.
- ``/api/emby/latest`` feeds the poster wall. It must return whole titles, cap
  its own limit, and never hand back an entry with no artwork, which would
  punch a grey hole in an otherwise dense grid.
"""
from __future__ import annotations

import asyncio
import base64

import httpx
from fastapi.testclient import TestClient

from app.adapters.live import LiveEmby
from app.main import app


def _basic(user: str = "admin", password: str = "change-me") -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _live_emby(handler) -> LiveEmby:
    """A LiveEmby whose HTTP calls are answered by ``handler``.

    Drives the real reshaping code rather than a re-implementation of it, so
    these assertions fail if the adapter changes shape.
    """
    emby = LiveEmby(lambda: {
        "enabled": True,
        "url": "http://emby.test",
        "api_key": "k",
        "timeout_seconds": 5,
        "verify_ssl": False,
    })
    emby._client = lambda timeout, verify: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(handler), base_url="http://emby.test")
    return emby


# -- latest items -----------------------------------------------------------

def test_latest_returns_titles_for_the_poster_wall() -> None:
    with TestClient(app) as client:
        r = client.get("/api/emby/latest", headers=_basic())
        assert r.status_code == 200
        items = r.json()
        assert items, "poster wall would render empty"
        for it in items:
            # Every tile addresses its artwork by id; no id means no tile.
            assert it["Id"]
            assert it["Name"]
            assert it["Type"] in {"Movie", "Series"}


def test_latest_limit_is_clamped_not_trusted() -> None:
    """A hand-edited URL must not turn one page load into a huge Emby query."""
    with TestClient(app) as client:
        big = client.get("/api/emby/latest?limit=500", headers=_basic())
        assert big.status_code == 200
        assert len(big.json()) <= 24
        assert client.get("/api/emby/latest?limit=0", headers=_basic()).status_code == 200


def test_latest_requires_auth() -> None:
    with TestClient(app) as client:
        assert client.get("/api/emby/latest").status_code == 401


def test_latest_drops_items_that_have_no_artwork() -> None:
    """An entry with no Primary image is a grey hole in the grid, not a tile."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Items": [
            {"Id": "1", "Name": "Has art", "Type": "Movie",
             "ImageTags": {"Primary": "abc"}},
            {"Id": "2", "Name": "No art", "Type": "Movie", "ImageTags": {}},
            {"Id": "3", "Name": "No tags at all", "Type": "Series"},
        ]})

    items = asyncio.run(_live_emby(handler).latest_items(12))
    assert [i["Id"] for i in items] == ["1"]


def test_latest_asks_emby_for_whole_titles_only() -> None:
    """A series that gained 12 episodes must not fill the wall with one show."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"Items": []})

    asyncio.run(_live_emby(handler).latest_items(9))
    assert seen["IncludeItemTypes"] == "Movie,Series"
    assert seen["SortBy"] == "DateCreated"
    assert seen["Limit"] == "9"


def test_latest_watch_keeps_unartworked_items_and_reads_tmdb() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"Items": [
            {"Id": "1", "Name": "Has art", "Type": "Movie",
             "ImageTags": {"Primary": "abc"}, "ProviderIds": {"Tmdb": "11"}},
            {"Id": "2", "Name": "No art", "Type": "Series", "ImageTags": {},
             "ProviderIds": {"tmdb": "22"}},
        ]})

    items = asyncio.run(_live_emby(handler).latest_items(100, watch=True))
    assert "ProviderIds" in seen["Fields"]
    assert seen["Limit"] == "100"
    assert [(i["Id"], i.get("tmdb_id")) for i in items] == [("1", 11), ("2", 22)]


# -- session artwork + progress ---------------------------------------------

def test_session_carries_artwork_id_and_progress() -> None:
    with TestClient(app) as client:
        sessions = client.get("/api/emby/sessions", headers=_basic()).json()
        assert sessions, "dashboard would show no playback"
        s = sessions[0]
        assert s.get("ItemId")          # without this there is no poster to fetch
        assert s.get("RunTimeTicks")
        assert "PositionTicks" in s
        pct = s.get("ProgressPercent")
        assert pct is None or 0 <= pct <= 100


def test_session_keeps_the_fields_the_old_table_renders() -> None:
    """The card did not replace the table; both read the same payload."""
    with TestClient(app) as client:
        s = client.get("/api/emby/sessions", headers=_basic()).json()[0]
        for field in ("UserName", "Client", "PlayMethod", "Item", "SpeedMBps"):
            assert field in s, f"{field} disappeared from the session payload"


def test_progress_is_computed_from_position_and_runtime() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{
            "Id": "s1", "UserId": "u1", "UserName": "someone", "Client": "app",
            "PlayState": {"PositionTicks": 30_000_000_000, "PlayMethod": "DirectPlay"},
            "NowPlayingItem": {"Id": "i1", "Name": "Film", "Type": "Movie",
                               "RunTimeTicks": 120_000_000_000},
        }])

    s = asyncio.run(_live_emby(handler).active_sessions())[0]
    assert s["ItemId"] == "i1"
    assert s["ProgressPercent"] == 25.0


def test_progress_is_absent_rather_than_zero_when_runtime_is_unknown() -> None:
    """0% and "unknown" look identical in a bar but mean different things.

    A live stream reports no runtime. Reporting that as 0.0 would draw an empty
    bar that looks like a session which just started, so the field is left
    unset and the UI omits the bar entirely.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{
            "Id": "s2", "UserId": "u2", "UserName": "someone", "Client": "app",
            "PlayState": {"PositionTicks": 0},
            "NowPlayingItem": {"Id": "i2", "Name": "Live channel", "Type": "TvChannel"},
        }])

    s = asyncio.run(_live_emby(handler).active_sessions())[0]
    assert s["ProgressPercent"] is None
    assert s["ItemId"] == "i2"


def test_sessions_without_playback_are_not_listed() -> None:
    """An idle client is a connection, not something to render a card for."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"Id": "idle", "UserName": "someone", "Client": "app"},
            {"Id": "playing", "UserName": "other", "Client": "app",
             "PlayState": {"PositionTicks": 10},
             "NowPlayingItem": {"Id": "i3", "Name": "Film", "RunTimeTicks": 100}},
        ])

    sessions = asyncio.run(_live_emby(handler).active_sessions())
    assert [s["Id"] for s in sessions] == ["playing"]
