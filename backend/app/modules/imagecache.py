"""Image cache — serve Emby artwork from local disk.

Why this is worth doing
-----------------------
A single library grid view fires dozens of poster requests, and every one of
them currently reaches Emby, is resized, and is sent again.  Artwork never
changes for a given item+size, so re-deriving it on every scroll is pure waste:
it burns Emby CPU exactly when the user is browsing, which is the moment the UI
most needs to feel instant.

Caching them locally turns a repeated upstream round trip into a disk read.

Design decisions that matter
----------------------------
**Keyed by a hash of the full request, not by item id.**  The same poster is
requested at a dozen sizes and in several formats; keying on the item alone
would serve a thumbnail where a banner was asked for.

**Cache only successes, and only images.**  An error page or an HTML login
redirect cached under a poster key would persist a transient Emby failure for
hours -- far worse than the miss it replaced.

**Two-level eviction.**  A byte budget with LRU eviction, plus a maximum age.
Without the age bound, artwork for deleted media would sit on disk forever;
without the byte bound, a large library would eventually fill the disk.

**Writes are atomic.**  A crash mid-write must not leave a truncated file that
is then served as a valid poster for the rest of its life.

**Negative results are remembered briefly.**  An item with no artwork would
otherwise be re-fetched on every render, which is the exact stampede this
module exists to prevent.
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import math
import os
import time
from pathlib import Path
from typing import Any

# Only these Emby image endpoints are proxied. An allowlist rather than a
# pattern: this endpoint is reachable from the front door, and it must not
# become a way to fetch arbitrary paths from Emby.
ALLOWED_IMAGE_TYPES = {
    "Primary", "Backdrop", "Banner", "Thumb", "Logo", "Art",
    "Disc", "Box", "Screenshot", "Menu", "Chapter", "BoxRear",
}
ALLOWED_CONTENT_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/gif", "image/avif",
}
# Emby's own default; a request without one still has to hash to something
# stable or every render would miss.
DEFAULT_MAX_BYTES = 8 * 1024 * 1024

NEGATIVE_TTL = 300.0


def _safe_id(value: str) -> str:
    """Item ids come from the URL; keep them to an alphabet that cannot escape
    the cache directory even if the router changes shape later."""
    return "".join(c for c in str(value) if c.isalnum() or c in "-_")[:64]


class ImageCache:
    def __init__(self, root: str | Path, max_bytes: int = 2 * 1024 ** 3,
                 max_age_seconds: int = 30 * 86400) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._max_bytes = max(64 * 1024 * 1024, int(max_bytes))
        self._max_age = max(3600, int(max_age_seconds))
        self._lock = asyncio.Lock()
        # Makes temp names unique between concurrent writers in one process.
        self._tmp_seq = itertools.count()
        # In-flight de-duplication: fifty clients opening the same page must
        # produce one upstream fetch, not fifty.
        self._inflight: dict[str, asyncio.Future] = {}
        self._negative: dict[str, float] = {}
        self._hits = 0
        self._misses = 0
        self._errors = 0
        self._bytes_served = 0
        self._evictions = 0
        self._last_sweep = 0.0

    # -- keys ----------------------------------------------------------------
    @staticmethod
    def key(item_id: str, image_type: str, params: dict[str, str]) -> str:
        # Only parameters that change the produced bytes participate; auth
        # tokens and cache-busters must not fragment the cache.
        significant = {
            k.lower(): str(v) for k, v in params.items()
            if k.lower() in ("maxwidth", "maxheight", "width", "height",
                             "quality", "tag", "format", "fillwidth",
                             "fillheight", "cropwhitespace", "index",
                             "backgroundcolor", "foregroundlayer")
        }
        raw = json.dumps([_safe_id(item_id), image_type, significant], sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _paths(self, key: str) -> tuple[Path, Path]:
        # Two-level fan-out: a single directory with a million entries makes
        # every lookup slow on most filesystems.
        shard = self._root / key[:2] / key[2:4]
        return shard / f"{key}.bin", shard / f"{key}.json"

    # -- read ----------------------------------------------------------------
    def lookup(self, key: str) -> tuple[bytes, str, str] | None:
        blob, meta_path = self._paths(key)
        if not blob.is_file() or not meta_path.is_file():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                return None
            stored_at = float(meta.get("stored_at") or 0)
            if not math.isfinite(stored_at):
                return None
        except (OSError, ValueError, TypeError):
            return None
        if time.time() - stored_at > self._max_age:
            self._discard(key)
            return None
        try:
            data = blob.read_bytes()
        except OSError:
            return None
        # Touch for LRU. Failure is not fatal: a read-only mtime just makes
        # eviction slightly less accurate.
        try:
            os.utime(blob, None)
        except OSError:
            pass
        return data, str(meta.get("content_type") or "image/jpeg"), str(meta.get("etag") or "")

    # -- write ---------------------------------------------------------------
    def store(self, key: str, data: bytes, content_type: str, etag: str = "") -> bool:
        if not data or content_type.split(";")[0].strip() not in ALLOWED_CONTENT_TYPES:
            return False
        if len(data) > DEFAULT_MAX_BYTES:
            return False
        blob, meta_path = self._paths(key)
        blob.parent.mkdir(parents=True, exist_ok=True)
        # Distinct temp names per file. `with_suffix` would collapse both onto
        # the same path -- blob and metadata share a stem and differ only by
        # extension -- so the metadata write would clobber the image, the
        # metadata rename would consume it, and the image rename would then
        # fail on a missing file. The visible symptom was a cache that stored
        # nothing while reporting no errors.
        token = f"{os.getpid()}-{next(self._tmp_seq)}"
        tmp_blob = blob.with_name(f"{blob.name}.tmp-{token}")
        tmp_meta = meta_path.with_name(f"{meta_path.name}.tmp-{token}")
        try:
            tmp_blob.write_bytes(data)
            tmp_meta.write_text(json.dumps({
                "stored_at": time.time(),
                "content_type": content_type,
                "etag": etag,
                "size": len(data),
            }), encoding="utf-8")
            # Metadata first, then the blob: the blob's presence is what
            # lookup() gates on, so it must never appear without its metadata.
            os.replace(tmp_meta, meta_path)
            os.replace(tmp_blob, blob)
            return True
        except OSError:
            tmp_blob.unlink(missing_ok=True)
            tmp_meta.unlink(missing_ok=True)
            return False

    def _discard(self, key: str) -> None:
        blob, meta = self._paths(key)
        blob.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)

    # -- negative caching ----------------------------------------------------
    def is_negative(self, key: str) -> bool:
        until = self._negative.get(key)
        if until is None:
            return False
        if until < time.time():
            self._negative.pop(key, None)
            return False
        return True

    def mark_negative(self, key: str) -> None:
        if len(self._negative) > 10000:
            now = time.time()
            self._negative = {k: v for k, v in self._negative.items() if v > now}
        self._negative[key] = time.time() + NEGATIVE_TTL

    # -- fetch ---------------------------------------------------------------
    async def fetch(self, key: str, producer: Any) -> tuple[bytes, str, str] | None:
        """Return cached bytes, fetching once if absent.

        Concurrent callers for the same key await a single fetch, so opening a
        library page does not turn one cold poster into N upstream requests.
        """
        hit = self.lookup(key)
        if hit:
            self._hits += 1
            self._bytes_served += len(hit[0])
            return hit
        if self.is_negative(key):
            self._misses += 1
            return None

        async with self._lock:
            existing = self._inflight.get(key)
            if existing is None:
                future: asyncio.Future = asyncio.get_running_loop().create_future()
                self._inflight[key] = future
                owner = True
            else:
                future, owner = existing, False

        if not owner:
            try:
                return await asyncio.shield(future)
            except Exception:  # noqa: BLE001 - the owner already recorded it
                return None

        try:
            result = await producer()
            if result:
                data, content_type, etag = result
                if self.store(key, data, content_type, etag):
                    self._misses += 1
                    self._bytes_served += len(data)
                else:
                    # Unsupported type or too large: still serve it, just do
                    # not keep it.
                    self._misses += 1
                    result = (data, content_type, etag)
            else:
                self.mark_negative(key)
                self._misses += 1
            if not future.done():
                future.set_result(result)
            return result
        except asyncio.CancelledError:
            # The owning request disconnected. Release coalesced requests and
            # allow an immediate retry; cancellation is not a negative image.
            if not future.done():
                future.set_result(None)
            raise
        except Exception:  # noqa: BLE001
            self._errors += 1
            self.mark_negative(key)
            if not future.done():
                # All callers use the same fail-open result. An exception on
                # an unobserved Future otherwise leaks into the event-loop log.
                future.set_result(None)
            return None
        finally:
            async with self._lock:
                self._inflight.pop(key, None)

    # -- maintenance ---------------------------------------------------------
    def sweep(self, force: bool = False) -> dict[str, Any]:
        """Evict by age, then by size (least recently used first)."""
        now = time.time()
        if not force and now - self._last_sweep < 300:
            return {"skipped": True}
        self._last_sweep = now

        entries: list[tuple[float, int, Path, Path]] = []
        total = 0
        removed_age = 0
        for blob in self._root.rglob("*.bin"):
            try:
                st = blob.stat()
            except OSError:
                continue
            meta = blob.with_suffix(".json")
            if now - st.st_mtime > self._max_age:
                blob.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                removed_age += 1
                continue
            entries.append((st.st_atime, st.st_size, blob, meta))
            total += st.st_size

        removed_size = 0
        if total > self._max_bytes:
            entries.sort(key=lambda e: e[0])  # oldest access first
            for _atime, size, blob, meta in entries:
                if total <= self._max_bytes * 0.9:
                    break
                blob.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                total -= size
                removed_size += 1

        self._evictions += removed_age + removed_size
        return {
            "skipped": False,
            "entries": len(entries) - removed_size,
            "bytes": total,
            "removed_expired": removed_age,
            "removed_lru": removed_size,
        }

    def stats(self) -> dict[str, Any]:
        total_requests = self._hits + self._misses
        entries = 0
        size = 0
        for blob in self._root.rglob("*.bin"):
            try:
                size += blob.stat().st_size
                entries += 1
            except OSError:
                continue
        return {
            "entries": entries,
            "bytes": size,
            "max_bytes": self._max_bytes,
            "usage_percent": round(size / self._max_bytes * 100, 1) if self._max_bytes else 0,
            "hits": self._hits,
            "misses": self._misses,
            "errors": self._errors,
            "hit_rate": round(self._hits / total_requests * 100, 1) if total_requests else None,
            "bytes_served": self._bytes_served,
            "evictions": self._evictions,
            "negative_entries": len(self._negative),
            "max_age_days": round(self._max_age / 86400, 1),
        }

    def clear(self) -> int:
        removed = 0
        for blob in self._root.rglob("*.bin"):
            blob.unlink(missing_ok=True)
            blob.with_suffix(".json").unlink(missing_ok=True)
            removed += 1
        self._negative.clear()
        return removed
