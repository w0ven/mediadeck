"""TMDB lookup for media requests.

The point of this module is that a request should name a *film*, not a string
somebody typed. "那个有船的电影" is not something an uploader can act on, and
two members asking for the same title in different words are two rows nobody
can deduplicate. A TMDB id makes the request identical to itself, which is
what the partial unique index on media_requests relies on.

**The panel works without a key.** The owner may not have one, and a request
feature that refuses to accept requests until an API key is configured is a
feature that is switched off. With no key, lookup returns None and the request
is stored under a ``#12345`` placeholder title: less pleasant, still actionable
because the id is right there. This is why every call site treats enrichment as
optional rather than as a precondition.

Nothing here raises on network failure. TMDB being unreachable must not take
a member's request with it -- the id was already valid before we asked.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from app.core.cache import TTLCache

API_ROOT = "https://api.themoviedb.org/3"
IMAGE_ROOT = "https://image.tmdb.org/t/p"
POSTER_SIZE = "w342"

# Lookups are cached for an hour: a title's name and year do not change, and
# several uploaders opening the same request should not each cost a round trip.
CACHE_TTL = 3600.0

HTTP_TIMEOUT = 10.0

MEDIA_TYPES = ("movie", "tv")

# themoviedb.org/movie/12345, /tv/12345-some-slug, with or without scheme,
# language prefix (/zh-CN/movie/…) or query string.
_LINK_RE = re.compile(
    r"themoviedb\.org/(?:[a-z]{2}(?:-[A-Za-z]{2})?/)?(movie|tv)/(\d+)",
    re.IGNORECASE)
# A bare id carries no type. The caller must ask movie vs TV before lookup.
_ID_RE = re.compile(r"^#?(\d{1,12})$")


def poster_url(poster_path: str, size: str = POSTER_SIZE) -> str:
    """Absolute URL for a stored poster path, or '' if there is none."""
    path = str(poster_path or "").strip()
    if not path:
        return ""
    if path.startswith(("http://", "https://")):
        return path
    return f"{IMAGE_ROOT}/{size}/{path.lstrip('/')}"


def parse_link(text: str) -> tuple[str, int] | None:
    """Pull (media_type, tmdb_id) out of whatever the member sent.

    Returns None rather than guessing when the text names no id: a wrong id
    silently creates a request for the wrong film, which is worse for everyone
    than being asked to paste the link again.

    A bare number returns an empty type: callers MUST ask movie vs TV, never guess.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    found = _LINK_RE.search(raw)
    if found:
        return found.group(1).lower(), int(found.group(2))
    bare = _ID_RE.match(raw)
    if bare:
        value = int(bare.group(1))
        if value > 0:
            return "", value
    return None


class TmdbClient:
    """Thin read-only TMDB wrapper. Never raises for the caller."""

    def __init__(self, config_provider: Any) -> None:
        # A provider rather than a value: the operator can paste a key into
        # the settings page while the bot is running, and the next request
        # should use it without a restart.
        self._config = config_provider
        self._cache = TTLCache(ttl=CACHE_TTL, max_entries=512)

    # -- config --------------------------------------------------------------

    def _cfg(self) -> dict[str, Any]:
        try:
            return self._config() or {}
        except Exception:  # noqa: BLE001 - a broken provider just means no key
            return {}

    def _api_creds(self) -> str:
        return str(self._cfg().get("tmdb_api_key") or "").strip()

    def _language(self) -> str:
        return str(self._cfg().get("tmdb_language") or "zh-CN").strip() or "zh-CN"

    @property
    def configured(self) -> bool:
        return bool(self._api_creds())

    async def search(self, query: str, page: int = 1, media_type: str = '', year: int | None = None) -> dict[str, Any]:
        """Paginated name search. Unavailable is distinct from an empty result."""
        if not self.configured:
            return {'available': False, 'results': [], 'page': 1, 'total_pages': 0}
        page = max(1, min(int(page), 500))
        endpoint = media_type if media_type in MEDIA_TYPES else 'multi'
        params = {'api_key': self._api_creds(), 'language': 'zh-CN',
                  'query': str(query).strip()[:100], 'page': page, 'include_adult': 'false'}
        if year and endpoint != 'multi':
            params['primary_release_year' if endpoint == 'movie' else 'first_air_date_year'] = int(year)
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                response = await client.get(f'{API_ROOT}/search/{endpoint}', params=params)
            response.raise_for_status()
            body = response.json()
            rows = []
            for item in body.get('results', []):
                kind = item.get('media_type', endpoint)
                if kind not in MEDIA_TYPES:
                    continue
                meta = self._parse(kind, item)
                if year and meta['year'] != int(year):
                    continue
                rows.append(dict(meta, media_type=kind, tmdb_id=int(item['id'])))
            return {'available': True, 'results': rows, 'page': page,
                    'total_pages': min(500, int(body.get('total_pages') or 0))}
        except Exception:  # noqa: BLE001 - optional upstream delivery must not replay a business action
            return {'available': False, 'results': [], 'page': page, 'total_pages': 0}

    # -- lookup --------------------------------------------------------------

    async def lookup_request(self, media_type: str, tmdb_id: int) -> dict[str, Any] | None:
        """Request cards prefer the Chinese title, with the original name alongside."""
        return await self.lookup(media_type, tmdb_id, language="zh-CN")

    async def lookup(self, media_type: str, tmdb_id: int, *, language: str | None = None) -> dict[str, Any] | None:
        """Title, year, poster and overview for one id.

        None means "no answer", for any reason: no key, unknown id, TMDB down.
        Callers must treat all three the same, because the member is waiting
        and the request is still valid without the metadata.
        """
        media_type = str(media_type or "").lower()
        if media_type not in MEDIA_TYPES:
            return None
        try:
            tmdb_id = int(tmdb_id)
        except (TypeError, ValueError):
            return None
        if tmdb_id <= 0:
            return None

        language = language or self._language()
        cache_key = f"{media_type}:{tmdb_id}:{language}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        # Checked after the cache so a key removed at runtime does not throw
        # away answers already held, but before any socket is opened: with no
        # key the request would be rejected anyway, and sending it would leak
        # what the member asked for to a third party for nothing.
        if not self.configured:
            return None

        # Guarded here as well as inside _fetch: "lookup never raises" is a
        # promise to the request flow, and it has to hold for every way the
        # fetch can fail, not only the ones httpx names.
        try:
            payload = await self._fetch(media_type, tmdb_id, language)
        except Exception:  # noqa: BLE001 - upstream failure is not the member's
            return None
        if payload is None:
            return None
        try:
            parsed = self._parse(media_type, payload)
        except Exception:  # noqa: BLE001 - an odd payload is a miss, not a crash
            return None
        self._cache.set(cache_key, dict(parsed))
        return parsed

    async def resolve(self, media_type: str, tmdb_id: int
                      ) -> tuple[str, dict[str, Any] | None]:
        """Look up ``media_type``, then the other one if that found nothing.

        A bare id from a member carries no type. Trying both is one extra call
        in the failure case and saves asking them to go and find out which
        kind of thing they just asked for.
        """
        found = await self.lookup(media_type, tmdb_id)
        if found is not None:
            return media_type, found
        other = "tv" if media_type == "movie" else "movie"
        found = await self.lookup(other, tmdb_id)
        if found is not None:
            return other, found
        return media_type, None

    async def _fetch(self, media_type: str, tmdb_id: int,
                     language: str) -> dict[str, Any] | None:
        url = f"{API_ROOT}/{media_type}/{tmdb_id}"
        params = {"api_key": self._api_creds(), "language": language}
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                r = await client.get(url, params=params)
            if r.status_code != 200:
                return None
            body = r.json()
        except Exception:  # noqa: BLE001 - upstream failure is not the member's
            return None
        return body if isinstance(body, dict) else None

    @staticmethod
    def _parse(media_type: str, body: dict[str, Any]) -> dict[str, Any]:
        """TMDB names the same fields differently for films and series."""
        if media_type == "tv":
            title = body.get("name") or body.get("original_name") or ""
            released = str(body.get("first_air_date") or "")
        else:
            title = body.get("title") or body.get("original_title") or ""
            released = str(body.get("release_date") or "")
        year = 0
        if len(released) >= 4 and released[:4].isdigit():
            year = int(released[:4])
        return {
            "title": str(title).strip(),
            "original_title": str(body.get("original_name" if media_type == "tv" else "original_title") or "").strip(),
            "year": year or None,
            "poster_path": str(body.get("poster_path") or ""),
            "overview": str(body.get("overview") or "").strip(),
            "seasons": [{'season_number': s.get('season_number'), 'episode_count': s.get('episode_count'),
                         'name': s.get('name', '')} for s in body.get('seasons', [])],
        }
