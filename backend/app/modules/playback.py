"""Emby playback interception — the piece that makes multi-node real.

Without this module the scheduler is decorative: it can pick a node, but no
actual Emby client ever asks it.  Emby hands clients a stream URL pointing at
the Emby host, and every byte is served from there.

Flow:

    client --> GET /emby/Videos/{ItemId}/stream.mkv?...  (points at us)
      1. is this a direct play request?  (transcodes must NOT move)
      2. ask Emby which file backs this item
      3. find the nodes that can actually serve that file
      4. affinity-pick one, sign the URL, 302 the client to it

Three rules matter more than anything else here.

**The affinity key is the media path, not the request URL.**  Two clients
playing the same movie send different session ids, api keys and container
extensions.  Keying on the URL would scatter one file across every node and
defeat the cache locality the scheduler exists to create.

**Path mapping is per node, per media root.**  A real server has more than one
media root -- this stack has ``/media`` (union mount) and ``/media-gd3`` (a
second Drive).  A single global "strip this prefix" rule cannot express that:
with ``strip_prefix=/media``, ``/media-gd3/x.mkv`` becomes ``-gd3/x.mkv`` and
the entire second library 404s.  Each node declares which roots it mirrors, and
a node that does not mirror a file's root is never selected for it.

**Fail open, always.**  Feature off, transcode, unknown item, no node that can
serve the path, Emby unreachable -- everything falls back to the Emby origin.
A panel bug must never turn into "playback is broken"; the worst acceptable
outcome is "playback did not get accelerated this time".
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from app.modules.signing import public_url, sign_url

# Emby serves the untouched file at .../original.<ext>; that is direct play by
# definition.  .../stream.<ext> is direct play only when Static=true, otherwise
# Emby intends to remux.  Everything else on this path (HLS playlists, DASH
# manifests, segments) is generated on the Emby host and does not exist on a
# node, so it must never be redirected.
DIRECT_ORIGINAL = "original"
DIRECT_STREAM = "stream"


@dataclass
class Decision:
    """Outcome of one interception, kept explicit so it can be logged/tested."""

    redirected: bool
    target: str
    reason: str
    node: str | None = None
    media_path: str | None = None
    pool: str | None = None
    signed: bool = False
    utag: str = ""
    rate_bps: int = 0


class TTLCache:
    """Tiny TTL cache for item -> path lookups.

    Every stream request would otherwise hit the Emby API; a popular title
    starting on 50 clients must not become 50 metadata calls.
    """

    def __init__(self, ttl: float = 300.0, max_entries: int = 4096) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if not entry:
            return None
        expires, value = entry
        if expires < time.time():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        if len(self._data) >= self._max:
            now = time.time()
            self._data = {k: v for k, v in self._data.items() if v[0] >= now}
            if len(self._data) >= self._max:
                self._data.clear()
        self._data[key] = (time.time() + self._ttl, value)

    def clear(self) -> None:
        self._data.clear()


def caller_token(headers: Any, query: dict[str, str]) -> str:
    """Extract the caller's own Emby credential from a playback request.

    Emby clients present it in several shapes depending on client and version;
    missing any one of them here would make legitimate playback look
    unauthenticated and silently disable acceleration for those clients.
    """
    for header in ("x-emby-token", "x-mediabrowser-token"):
        value = headers.get(header)
        if value:
            return str(value).strip()
    # Authorization: MediaBrowser Client="...", Token="abc"
    auth = headers.get("authorization") or headers.get("x-emby-authorization") or ""
    match = re.search(r'token\s*=\s*"?([^",\s]+)"?', str(auth), re.IGNORECASE)
    if match:
        return match.group(1).strip()
    for key in ("api_key", "ApiKey", "apikey", "X-Emby-Token"):
        if query.get(key):
            return str(query[key]).strip()
    return ""


def caller_device(headers: Any, query: dict[str, str]) -> str:
    """Extract the caller's device id from a playback request.

    This is what disambiguates a fleet-wide admin api_key: Emby scopes
    ``/emby/Sessions?api_key=`` to the credential, so a *user* token names its
    owner on its own, but an admin key sees every session and identifies
    nobody. The device id names the one session actually playing.

    Emby clients spell it inconsistently across versions, and every spelling
    missed here downgrades that client back to an unattributed, uncapped
    stream -- so accept all of them rather than the modern one only.
    """
    for header in ("x-emby-device-id", "x-mediabrowser-device-id"):
        value = headers.get(header)
        if value:
            return str(value).strip()
    auth = headers.get("authorization") or headers.get("x-emby-authorization") or ""
    match = re.search(r'deviceid\s*=\s*"?([^",\s]+)"?', str(auth), re.IGNORECASE)
    if match:
        return match.group(1).strip()
    for key in ("DeviceId", "deviceId", "deviceid", "device_id"):
        if query.get(key):
            return str(query[key]).strip()
    return ""


def is_transcode_request(path: str, query: dict[str, str]) -> bool:
    """Decide whether Emby is generating this response rather than serving a file.

    Default-deny: anything not positively recognised as direct play is treated
    as transcode and left to Emby. Guessing wrong in that direction merely
    skips acceleration; guessing wrong the other way redirects a client to a
    node for a file that does not exist there.
    """
    tail = path.rsplit("/", 1)[-1].lower()
    endpoint = tail.split(".", 1)[0]
    if endpoint == DIRECT_ORIGINAL:
        # Real clients request original.mkv without Static=true; requiring it
        # here would classify every real direct play as a transcode.
        return False
    if endpoint == DIRECT_STREAM:
        static = (query.get("Static") or query.get("static") or "").lower()
        return static not in ("true", "1")
    return True


def match_pool(media_path: str, pools: list[Any]) -> tuple[Any, str] | None:
    """Find the node pool serving this Emby path, longest prefix wins.

    Longest-prefix matters: ``/media`` and ``/media-gd3`` both "start with"
    ``/media``, and picking the shorter one silently mangles every gd3 path.
    """
    path = (media_path or "").replace("\\", "/")
    best: tuple[Any, str] | None = None
    best_len = -1
    for pool in pools or []:
        prefix = str(getattr(pool, "emby_prefix", "") or "").rstrip("/")
        if not prefix:
            continue
        matches = path == prefix or path.startswith(prefix + "/")
        if matches and len(prefix) > best_len:
            rel = path[len(prefix):].lstrip("/")
            best, best_len = (pool, rel), len(prefix)
    return best


class PlaybackRouter:
    def __init__(self, emby: Any, scheduler: Any, config_provider: Any,
                 emby_config_provider: Any, rate_resolver: Any = None) -> None:
        self._emby = emby
        self._scheduler = scheduler
        self._config = config_provider
        self._emby_config = emby_config_provider
        # Optional async callable: caller token -> (rate_bytes_per_s, utag).
        # Lives outside this module so the router stays ignorant of member
        # semantics; None means "sign without a per-user cap" (mock mode).
        self._rate_resolver = rate_resolver
        self._cache = TTLCache()
        self._auth_cache = TTLCache(ttl=60.0)
        self._log: list[dict[str, Any]] = []

    # -- helpers -------------------------------------------------------------
    def _origin(self) -> str:
        return (self._emby_config() or {}).get("url", "").rstrip("/")

    def _passthrough(self, request_path: str, query: dict[str, str],
                     reason: str) -> Decision:
        origin = self._origin()
        target = f"{origin}/{request_path.lstrip('/')}" if origin else request_path
        if query:
            target = f"{target}?{urlencode(query)}"
        return Decision(redirected=False, target=target, reason=reason)

    def _record(self, decision: Decision, item_id: str) -> None:
        self._log.append({
            "ts": time.time(),
            "item_id": item_id,
            "redirected": decision.redirected,
            "node": decision.node,
            "pool": decision.pool,
            "reason": decision.reason,
            "media_path": decision.media_path,
            "signed": decision.signed,
            "utag": decision.utag,
            "rate_bps": decision.rate_bps,
        })
        if len(self._log) > 500:
            del self._log[:-500]

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._log[-max(1, min(limit, 500)):]

    def invalidate(self) -> None:
        self._cache.clear()
        self._auth_cache.clear()

    async def _authorised(self, item_id: str, token: str, cache_scope: str) -> bool:
        verify = getattr(self._emby, "verify_item_access", None)
        if verify is None:  # pragma: no cover - adapter contract guarantees it
            return False
        # The panel may run far from Emby (here: Montreal vs Germany), so an
        # uncached check would add a cross-continent round trip to every
        # playback start. Cache positives only, and briefly: a revoked user
        # keeps access for at most this long, while a denial is always
        # re-checked so revocation cannot be cached into place.
        key = f"auth:{cache_scope}:{item_id}:{hashlib.sha256(token.encode()).hexdigest()[:16]}"
        if self._auth_cache.get(key):
            return True
        try:
            allowed = bool(await verify(item_id, token))
        except Exception:  # noqa: BLE001 - treat any failure as "not authorised"
            return False
        if allowed:
            self._auth_cache.set(key, True)
        return allowed

    async def _media_path(self, item_id: str, media_source_id: str | None,
                          cache_scope: str) -> str | None:
        cache_key = f"{cache_scope}:{item_id}:{media_source_id or ''}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached or None
        try:
            sources = await self._emby.item_media_paths(item_id)
        except Exception:  # noqa: BLE001 - fail open, never break playback
            return None
        path = None
        if media_source_id:
            # An explicit edition/source must never silently select another file.
            path = sources.get(media_source_id)
        elif sources:
            path = next(iter(sources.values()))
        # Cache negatives too, so a bad item id cannot hammer Emby.
        self._cache.set(cache_key, path or "")
        return path

    # -- main entry ----------------------------------------------------------
    async def route(self, item_id: str, request_path: str,
                    query: dict[str, str], caller_token: str = "",
                    require_auth: bool = False,
                    caller_device: str = "", cache_scope: str = "direct",
                    only_node: str = "") -> Decision:
        """``only_node``: restrict selection to one node. Used by pinned
        external entries whose stream domain proxies exactly that node; any
        other choice would 404 on the friend's CDN. If that node cannot serve
        the item the normal fail-open passthrough applies."""
        cfg = self._config() or {}

        if not cfg.get("enabled"):
            return self._passthrough(request_path, query, "disabled")

        if cfg.get("direct_only", True) and is_transcode_request(request_path, query):
            decision = self._passthrough(request_path, query, "transcode")
            self._record(decision, item_id)
            return decision

        # The panel sits on the playback path, so it must not become a way
        # around Emby's own authentication. Without this an unauthenticated
        # caller could guess an item id and receive a signed media URL.
        # Falling back to the origin (rather than 403) keeps the fail-open
        # contract: Emby then rejects the request itself, exactly as before.
        if require_auth and not await self._authorised(item_id, caller_token, cache_scope):
            decision = self._passthrough(request_path, query, "unauthorised")
            self._record(decision, item_id)
            return decision

        media_source_id = query.get("MediaSourceId") or query.get("mediaSourceId")
        media_path = await self._media_path(item_id, media_source_id, cache_scope)
        if not media_path:
            decision = self._passthrough(request_path, query, "unresolved-item")
            self._record(decision, item_id)
            return decision

        # Only nodes that actually mirror this media root may serve it.
        def can_serve(state: Any) -> bool:
            if only_node and getattr(state.node, "name", "") != only_node:
                return False
            return match_pool(media_path, getattr(state.node, "pools", [])) is not None

        chosen = self._scheduler.pick(context=media_path, predicate=can_serve)
        if not chosen:
            decision = self._passthrough(
                request_path, query,
                "pinned-node-unavailable" if only_node else "no-capable-node")
            decision.media_path = media_path
            self._record(decision, item_id)
            return decision

        matched = match_pool(media_path, chosen.node.pools)
        if not matched:  # pragma: no cover - predicate already guarantees this
            decision = self._passthrough(request_path, query, "no-pool-match")
            decision.media_path = media_path
            self._record(decision, item_id)
            return decision
        pool, relative = matched
        if not relative:
            decision = self._passthrough(request_path, query, "empty-mapped-path")
            decision.media_path = media_path
            self._record(decision, item_id)
            return decision

        url_path = f"{str(pool.url_prefix).rstrip('/')}/{relative}"
        secret = str(getattr(chosen.node, "sign_secret", "") or "")
        rate_bps, utag = 0, ""
        if secret:
            # Per-user bandwidth cap and anonymised tag ride inside the
            # signature: the node enforces the cap and attributes real bytes
            # to the user, and neither can be edited off the URL.
            if self._rate_resolver is not None and caller_token:
                try:
                    rate_bps, utag = await self._rate_resolver(
                        caller_token, caller_device, cache_scope)
                except Exception:  # noqa: BLE001 - leave access decisions to origin
                    rate_bps, utag = 0, ""
                if not utag:
                    decision = self._passthrough(request_path, query, "unattributed-caller")
                    self._record(decision, item_id)
                    return decision
            target = sign_url(
                chosen.node.base_url, url_path, secret,
                int(getattr(chosen.node, "sign_ttl_seconds", 21600) or 21600),
                arg_digest=str(getattr(chosen.node, "sign_arg_digest", "md5")),
                arg_expires=str(getattr(chosen.node, "sign_arg_expires", "expires")),
                rate_bps=rate_bps, utag=utag,
            )
        else:
            target = public_url(chosen.node.base_url, url_path)

        decision = Decision(
            redirected=True, target=target, reason="redirected",
            node=chosen.node.name, media_path=media_path,
            pool=pool.name, signed=bool(secret),
            utag=utag,
            rate_bps=int(rate_bps),
        )
        self._record(decision, item_id)
        return decision
