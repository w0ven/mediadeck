"""Server-sent events — push updates instead of polling.

A 30-second poll is wrong in both directions at once: a stream that starts
now is invisible for up to 30s, while an idle panel keeps hammering Emby
forever.  It also re-renders the page under the operator's cursor, which is
why editing a form used to lose keystrokes.

SSE fits this shape better than websockets: the traffic is one-way, it is
plain HTTP (so it survives the same reverse proxies as the rest of the panel),
and browsers reconnect on their own.

The stream sends whole snapshots rather than diffs.  Snapshots are small here,
and a client that reconnects mid-flight then cannot end up rendering a diff
against state it never received.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

# Slow enough not to hammer Emby, fast enough that "now playing" feels live.
DEFAULT_INTERVAL = 3.0
# Proxies drop idle connections; a comment frame keeps the pipe warm.
KEEPALIVE_INTERVAL = 15.0
PRODUCER_TIMEOUT = 10.0


def format_event(event: str, data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


class EventStream:
    """Polls the given producers server-side and pushes only what changed."""

    def __init__(self, producers: dict[str, Callable[[], Awaitable[Any]]],
                 interval: float = DEFAULT_INTERVAL) -> None:
        self._producers = producers
        self._interval = interval

    async def _collect(self, name: str) -> Any:
        try:
            return await asyncio.wait_for(self._producers[name](), PRODUCER_TIMEOUT)
        except Exception:  # noqa: BLE001 - one failing panel must not kill the stream
            return None

    async def iterate(self, names: list[str]) -> AsyncIterator[str]:
        wanted = list(dict.fromkeys(n for n in names if n in self._producers)) or list(self._producers)
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max(1, len(wanted)))

        async def poll(name: str) -> None:
            last: str | None = None
            while True:
                value = await self._collect(name)
                if value is not None:
                    try:
                        encoded = json.dumps(value, ensure_ascii=False, default=str)
                    except (TypeError, ValueError):
                        pass
                    else:
                        if encoded != last:
                            await queue.put(f"event: {name}\ndata: {encoded}\n\n")
                            last = encoded
                await asyncio.sleep(self._interval)

        # Independent topics: a wedged upstream must not hide local snapshots
        # or suppress keepalives. One bounded queue applies client backpressure.
        workers = [asyncio.create_task(poll(name)) for name in wanted]
        try:
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), KEEPALIVE_INTERVAL)
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


async def safe_stream(iterator: AsyncIterator[str]) -> AsyncIterator[str]:
    """Swallow client disconnects, which are normal, not errors."""
    try:
        with contextlib.suppress(asyncio.CancelledError, GeneratorExit):
            async for chunk in iterator:
                yield chunk
    finally:
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()
