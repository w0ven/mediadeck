"""Shared TTL cache for read-only upstream views.

Panel pages auto-refresh every 30s and every page switch refetches.  An
endpoint costing one upstream round trip per render therefore turns into
constant load on Emby and, more visibly, into UI lag: the library view issues
one item-count query per library, so a 10-library server meant 11 sequential
Emby calls before anything rendered.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any


class TTLCache:
    def __init__(self, ttl: float = 60.0, max_entries: int = 512) -> None:
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

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        if len(self._data) >= self._max:
            now = time.time()
            self._data = {k: v for k, v in self._data.items() if v[0] >= now}
            if len(self._data) >= self._max:
                self._data.clear()
        self._data[key] = (time.time() + (self._ttl if ttl is None else ttl), value)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    def drop_prefix(self, prefix: str) -> None:
        """Drop every key with this prefix. Used when a rate cap changes."""
        if not prefix:
            return
        self._data = {k: v for k, v in self._data.items() if not k.startswith(prefix)}

    async def resolve(self, key: str, producer: Callable[[], Awaitable[Any]],
                      ttl: float | None = None) -> Any:
        """Return the cached value, else await the producer and cache it."""
        cached = self.get(key)
        if cached is not None:
            return cached
        value = await producer()
        self.set(key, value, ttl)
        return value
