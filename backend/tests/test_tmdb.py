"""TMDB lookup, whose only hard requirement is that it degrades quietly.

The owner may never configure a key. So the property under test is not "we
can talk to TMDB" but "everything still works when we cannot": no key means
no socket, no exception, and a request that still carries the id somebody
pasted. The link parser gets the most attention because it is the one place a
typo turns into a request for the wrong film.
"""
from __future__ import annotations

import asyncio

import pytest

from app.modules.tmdb import (
    CACHE_TTL,
    TmdbClient,
    parse_link,
    poster_url,
)

# Assembled rather than written out so nothing in this file resembles a real
# credential to a scanner.
FAKE_CREDS = "tmdb" + "-" + "placeholder-not-a-real-key"


def _client(api_creds: str = FAKE_CREDS, language: str = "zh-CN") -> TmdbClient:
    return TmdbClient(lambda: {"tmdb_api_key": api_creds,
                               "tmdb_language": language})


# -- link parsing ------------------------------------------------------------

@pytest.mark.parametrize(("text", "expected"), [
    ("https://www.themoviedb.org/movie/12345", ("movie", 12345)),
    ("https://www.themoviedb.org/tv/999", ("tv", 999)),
    ("https://www.themoviedb.org/movie/550-fight-club", ("movie", 550)),
    ("https://www.themoviedb.org/tv/1396-breaking-bad", ("tv", 1396)),
    ("themoviedb.org/movie/77", ("movie", 77)),
    ("http://themoviedb.org/tv/77", ("tv", 77)),
    # Language-prefixed links are what a member on a localised site copies.
    ("https://www.themoviedb.org/zh-CN/movie/424", ("movie", 424)),
    # Trailing query strings and surrounding chatter must not defeat it.
    ("https://www.themoviedb.org/movie/424?language=zh", ("movie", 424)),
    ("求这个 https://www.themoviedb.org/movie/424 谢谢", ("movie", 424)),
    # A bare id requires explicit type selection; never guess.
    ("12345", ("", 12345)),
    ("#12345", ("", 12345)),
    ("  550  ", ("", 550)),
])
def test_a_link_or_an_id_resolves_to_a_type_and_a_number(text, expected) -> None:
    assert parse_link(text) == expected


@pytest.mark.parametrize("text", [
    "", "   ", None,
    "随便找部好看的",
    "https://www.imdb.com/title/tt0137523/",
    # No id at all: guessing one would create a request for the wrong film.
    "https://www.themoviedb.org/movie/",
    "https://www.themoviedb.org/person/12345",
    "abc123",
    "0",
])
def test_text_that_names_no_title_is_refused_rather_than_guessed(text) -> None:
    assert parse_link(text) is None


def test_poster_paths_become_absolute_urls_and_empty_stays_empty() -> None:
    assert poster_url("/abc.jpg").endswith("/w342/abc.jpg")
    assert poster_url("") == ""
    assert poster_url(None) == ""
    # An already-absolute URL is passed through rather than double-prefixed.
    assert poster_url("https://cdn.example/x.jpg") == "https://cdn.example/x.jpg"


# -- no key means no request -------------------------------------------------

def test_without_a_key_lookup_returns_none_and_opens_no_socket() -> None:
    """The owner has no key today. That must not break requests, and it must
    not quietly send what members are asking for to a third party either."""
    client = _client(api_creds="")
    calls = []

    async def fail(*args, **kwargs):
        calls.append(args)
        raise AssertionError("no HTTP call may be made without a key")

    client._fetch = fail  # type: ignore[assignment]

    assert asyncio.run(client.lookup("movie", 550)) is None
    assert client.configured is False
    assert calls == []


def test_without_a_key_resolve_still_answers_with_the_requested_type() -> None:
    client = _client(api_creds="")
    media_type, meta = asyncio.run(client.resolve("tv", 1396))
    assert media_type == "tv"
    assert meta is None


# -- parsing an answer -------------------------------------------------------

def _stub(client: TmdbClient, payloads: dict) -> list:
    seen: list = []

    async def fetch(media_type, tmdb_id, language):
        seen.append((media_type, tmdb_id, language))
        return payloads.get((media_type, tmdb_id))

    client._fetch = fetch  # type: ignore[assignment]
    return seen


def test_a_film_yields_title_year_poster_and_overview() -> None:
    client = _client()
    _stub(client, {("movie", 550): {
        "title": "搏击俱乐部", "release_date": "1999-10-15",
        "poster_path": "/poster.jpg", "overview": "简介",
    }})

    found = asyncio.run(client.lookup("movie", 550))
    assert found == {"title": "搏击俱乐部", "year": 1999,
                     "poster_path": "/poster.jpg", "overview": "简介", "original_title": "", "seasons": []}


def test_a_series_reads_the_fields_tmdb_names_differently() -> None:
    """TV uses name/first_air_date; reading title/release_date would give a
    request with no name and no year on every series ever asked for."""
    client = _client()
    _stub(client, {("tv", 1396): {
        "name": "绝命毒师", "first_air_date": "2008-01-20",
        "poster_path": "/bb.jpg", "overview": "老师",
    }})

    found = asyncio.run(client.lookup("tv", 1396))
    assert found["title"] == "绝命毒师"
    assert found["year"] == 2008


def test_a_title_with_no_release_date_still_resolves_without_a_year() -> None:
    client = _client()
    _stub(client, {("movie", 7): {"title": "未定档", "release_date": ""}})
    found = asyncio.run(client.lookup("movie", 7))
    assert found["title"] == "未定档"
    assert found["year"] is None


def test_the_configured_language_is_sent_upstream() -> None:
    client = _client(language="en-US")
    seen = _stub(client, {("movie", 1): {"title": "X"}})
    asyncio.run(client.lookup("movie", 1))
    assert seen == [("movie", 1, "en-US")]


# -- caching -----------------------------------------------------------------

def test_a_second_lookup_of_the_same_title_is_served_from_cache() -> None:
    """Several uploaders opening the same request should cost one round trip."""
    client = _client()
    seen = _stub(client, {("movie", 550): {"title": "搏击俱乐部",
                                           "release_date": "1999-10-15"}})

    first = asyncio.run(client.lookup("movie", 550))
    second = asyncio.run(client.lookup("movie", 550))

    assert first == second
    assert len(seen) == 1, "the second lookup must not hit the network"
    assert CACHE_TTL == 3600.0


def test_the_cache_is_keyed_by_type_id_and_language() -> None:
    client = _client()
    seen = _stub(client, {("movie", 5): {"title": "A"}, ("tv", 5): {"name": "B"}})

    assert asyncio.run(client.lookup("movie", 5))["title"] == "A"
    assert asyncio.run(client.lookup("tv", 5))["title"] == "B"
    assert len(seen) == 2


def test_a_cached_answer_cannot_be_mutated_by_its_caller() -> None:
    """The cache hands out copies: a caller that edits the dict it got back
    would otherwise poison every later lookup of that title."""
    client = _client()
    _stub(client, {("movie", 550): {"title": "搏击俱乐部"}})

    first = asyncio.run(client.lookup("movie", 550))
    first["title"] = "被改坏了"
    second = asyncio.run(client.lookup("movie", 550))

    assert second["title"] == "搏击俱乐部"


# -- failures are not the member's problem -----------------------------------

def test_an_unknown_id_returns_none_and_is_not_cached_as_an_answer() -> None:
    client = _client()
    seen = _stub(client, {})  # nothing matches: upstream 404
    assert asyncio.run(client.lookup("movie", 404404)) is None
    assert asyncio.run(client.lookup("movie", 404404)) is None
    assert len(seen) == 2


def test_an_upstream_failure_is_swallowed_rather_than_raised() -> None:
    """TMDB being down must not take a member's request with it."""
    client = _client()

    async def boom(*args, **kwargs):
        raise RuntimeError("connection reset")

    client._fetch = boom  # type: ignore[assignment]
    assert asyncio.run(client.lookup("movie", 550)) is None


def test_a_broken_config_provider_reads_as_no_key_instead_of_crashing() -> None:
    def broken():
        raise RuntimeError("settings unavailable")

    client = TmdbClient(broken)
    assert client.configured is False
    assert asyncio.run(client.lookup("movie", 550)) is None


@pytest.mark.parametrize(("media_type", "tmdb_id"), [
    ("person", 1), ("", 1), ("movie", 0), ("movie", -3), ("movie", "abc"),
])
def test_nonsense_arguments_are_refused_before_any_call(media_type, tmdb_id) -> None:
    client = _client()
    _stub(client, {})
    assert asyncio.run(client.lookup(media_type, tmdb_id)) is None


# -- resolve falls back to the other type ------------------------------------

def test_a_bare_id_that_is_not_a_film_is_retried_as_a_series() -> None:
    """Members paste ids without knowing the type. One extra call beats
    sending them away to find out what kind of thing they just asked for."""
    client = _client()
    seen = _stub(client, {("tv", 1396): {"name": "绝命毒师",
                                         "first_air_date": "2008-01-20"}})

    media_type, meta = asyncio.run(client.resolve("movie", 1396))

    assert media_type == "tv"
    assert meta["title"] == "绝命毒师"
    assert [s[0] for s in seen] == ["movie", "tv"]


def test_resolve_stops_at_the_first_hit_without_trying_the_other_type() -> None:
    client = _client()
    seen = _stub(client, {("movie", 550): {"title": "搏击俱乐部"}})

    media_type, meta = asyncio.run(client.resolve("movie", 550))

    assert media_type == "movie" and meta["title"] == "搏击俱乐部"
    assert [s[0] for s in seen] == ["movie"]


def test_resolve_keeps_the_asked_type_when_neither_matches() -> None:
    client = _client()
    _stub(client, {})
    media_type, meta = asyncio.run(client.resolve("tv", 31337))
    assert media_type == "tv" and meta is None


def test_request_lookup_prefers_chinese_and_keeps_original_name():
    client=_client(language='en-US')
    seen=_stub(client,{('movie',550):{'title':'搏击俱乐部','original_title':'Fight Club'}})
    result=asyncio.run(client.lookup_request('movie',550))
    assert result['title']=='搏击俱乐部' and result['original_title']=='Fight Club'
    assert seen[0][-1]=='zh-CN'


def test_search_paginates_filters_and_reports_unavailable(monkeypatch):
    import httpx
    original=httpx.AsyncClient
    calls=[]
    def handler(request):
        calls.append(request)
        if request.url.params.get('query')=='fail':
            return httpx.Response(503)
        return httpx.Response(200,json={'total_pages':3,'results':[
            {'id':10,'title':'中文标题','original_title':'Original','release_date':'1999-01-01','poster_path':'/p.jpg'}]})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    result=asyncio.run(_client().search('Original',page=2,media_type='movie',year=1999))
    assert result['available'] and result['page']==2 and result['total_pages']==3
    assert result['results'][0]['tmdb_id']==10
    assert calls[0].url.path=='/3/search/movie'
    assert calls[0].url.params['primary_release_year']=='1999'
    assert calls[0].url.params['language']=='zh-CN'
    assert asyncio.run(_client().search('fail'))['available'] is False
    before=len(calls)
    assert asyncio.run(_client('').search('No key'))['available'] is False
    assert len(calls)==before
