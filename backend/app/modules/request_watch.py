"""Watch accepted requests until the demanded title lands in the library.

Incremental rounds reverse-match one ``latest_items`` window against local
watches; only hits call ``request_lookup``. A slower full scan covers titles
that missed that window (late TMDB scrape, new episodes of an existing series).
"""

from __future__ import annotations

import json
import time
from typing import Any

WATCH_TTL = 10 * 86400
INCREMENTAL_LIMIT = 100
FULL_SCAN_EVERY = 6 * 3600
LIBRARY_KIND = "library"


def item_tmdb_id(item: dict[str, Any] | None) -> int | None:
    if not item:
        return None
    raw = item.get("tmdb_id")
    if raw in (None, ""):
        providers = item.get("ProviderIds") or item.get("provider_ids") or {}
        raw = providers.get("Tmdb", providers.get("tmdb"))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def episode_pairs(items: list[dict[str, Any]] | None) -> set[tuple[int, int]]:
    pairs: set[tuple[int, int]] = set()
    for item in items or []:
        for ep in item.get("episodes") or []:
            try:
                season, episode = int(ep.get("season")), int(ep.get("episode"))
            except (TypeError, ValueError):
                continue
            if season < 0 or episode <= 0:
                continue
            pairs.add((season, episode))
    return pairs


def _catalog_counts(catalog: list[dict[str, Any]] | None) -> dict[int, int]:
    counts: dict[int, int] = {}
    for row in catalog or []:
        try:
            number = int(row.get("season_number"))
            count = int(row.get("episode_count") or 0)
        except (TypeError, ValueError):
            continue
        if number <= 0 or count <= 0:
            continue
        counts[number] = count
    return counts


def required_pairs(
    media_type: str,
    demand: dict[str, Any] | None,
    catalog: list[dict[str, Any]] | None = None,
) -> set[tuple[int, int]] | None:
    """Episode pairs the demand wants. ``None`` means a movie: any item is enough."""
    if media_type == "movie":
        return None
    demand = demand or {}
    seasons = [int(s) for s in (demand.get("seasons") or [])]
    episodes = [int(e) for e in (demand.get("episodes") or [])]
    if demand.get("scope") == "episodes" and seasons and episodes:
        return {(seasons[0], episode) for episode in episodes}
    counts = _catalog_counts(catalog)
    wanted = seasons or list(counts)
    pairs: set[tuple[int, int]] = set()
    for season in wanted:
        count = counts.get(season)
        if not count:
            continue
        pairs.update((season, n) for n in range(1, count + 1))
    return pairs


def library_stage(
    media_type: str,
    demand: dict[str, Any] | None,
    items: list[dict[str, Any]] | None,
    catalog: list[dict[str, Any]] | None = None,
) -> str | None:
    """``complete``, ``partial``, or ``None`` when nothing relevant is in the library."""
    items = items or []
    if media_type == "movie":
        return "complete" if items else None
    present = episode_pairs(items)
    if not present:
        return None
    required = required_pairs(media_type, demand, catalog)
    if required is None:
        return "complete"
    if not required:
        # Whole series/season without a catalog: first content only, never "集齐".
        return "partial"
    found = present & required
    if not found:
        return None
    if found >= required:
        return "complete"
    return "partial"


async def _season_catalog(tmdb: Any, media_type: str, tmdb_id: int) -> list | None:
    if media_type != "tv" or tmdb is None:
        return None
    lookup = getattr(tmdb, "lookup", None) or getattr(tmdb, "lookup_request", None)
    if lookup is None:
        return None
    try:
        meta = await lookup(media_type, int(tmdb_id))
    except Exception:  # noqa: BLE001 - catalog is optional; never fail a watch round
        return None
    if not isinstance(meta, dict):
        return None
    seasons = meta.get("seasons")
    return seasons if isinstance(seasons, list) else None


async def inspect_watch(
    service: Any,
    library: Any,
    watch: dict[str, Any],
    *,
    tmdb: Any = None,
    now: int | None = None,
) -> str | None:
    """Look up one watch. Upstream errors skip this ticket and do not raise."""
    now = int(now if now is not None else time.time())
    rid = int(watch["request_id"])
    try:
        media_type = watch["media_type"]
        tmdb_id = int(watch["tmdb_id"])
        demand = watch.get("demand")
        if demand is None:
            demand = json.loads(watch.get("demand_json") or "{}")
        items = await library.request_lookup(media_type, tmdb_id)
        if not isinstance(items, list):
            raise TypeError("request_lookup must return a list")
        catalog = await _season_catalog(tmdb, media_type, tmdb_id)
        stage = library_stage(media_type, demand, items, catalog)
    except Exception:  # noqa: BLE001 - one bad lookup must not stop the round
        return "error"
    if stage:
        service.apply_library_stage(rid, stage, now=now)
        return stage
    service.touch_watch(rid, now=now)
    return None


async def _latest_window(library: Any, limit: int) -> list[dict[str, Any]] | None:
    try:
        latest = await library.latest_items(limit, watch=True)
    except TypeError:
        try:
            latest = await library.latest_items(limit)
        except Exception:  # noqa: BLE001 - skip the round
            return None
    except Exception:  # noqa: BLE001 - skip the round
        return None
    return latest if isinstance(latest, list) else None


async def poll_library_watches(
    service: Any,
    library: Any,
    *,
    tmdb: Any = None,
    now: int | None = None,
    full: bool = False,
    latest_limit: int = INCREMENTAL_LIMIT,
) -> dict[str, Any]:
    """One reverse-match round (or a full per-watch lookup). Never raises."""
    now = int(now if now is not None else time.time())
    summary: dict[str, Any] = {
        "ok": True,
        "checked": 0,
        "lookups": 0,
        "notified": 0,
        "expired": 0,
        "skipped": False,
        "full": full,
    }
    try:
        summary["expired"] = service.expire_due_watches(now=now)
        watches = service.active_watches()
    except Exception:  # noqa: BLE001 - a local db blip must not break the scheduler
        return {**summary, "ok": True, "skipped": True, "结果": "本轮跳过"}
    if not watches:
        return summary
    if library is None:
        return {**summary, "skipped": True, "结果": "媒体库未配置，本轮跳过"}

    targets = watches
    if not full:
        latest = await _latest_window(library, latest_limit)
        if latest is None:
            return {**summary, "skipped": True, "结果": "媒体库不可达，本轮跳过"}
        fresh = {item_tmdb_id(item) for item in latest}
        fresh.discard(None)
        targets = [watch for watch in watches if int(watch["tmdb_id"]) in fresh]
        summary["latest"] = len(latest)

    for watch in targets:
        summary["checked"] += 1
        summary["lookups"] += 1
        result = await inspect_watch(service, library, watch, tmdb=tmdb, now=now)
        if result in ("partial", "complete"):
            summary["notified"] += 1
    return summary
