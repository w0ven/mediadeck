"""Signed-in devices as Emby sees them: one row per client install with a live login.

Emby keeps a device record while its access token exists and drops it when the
device is deleted, which also signs that client out. The observational
``devices`` table (device groups) is unaffected.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

CACHE_SECONDS = 20.0

_KINDS = (
    ('tv', re.compile(r'apple ?tv|android ?tv|androidtv|\btv\b|[-_]tv|bravia|shield|mibox|fire ?tv|chromecast', re.IGNORECASE)),
    ('tablet', re.compile(r'ipad|tablet|\bpad\b|matepad|\btab\b', re.IGNORECASE)),
    ('phone', re.compile(r'iphone|android|harmony|honor|huawei|xiaomi|redmi|oppo|vivo|oneplus|samsung|pixel|phone', re.IGNORECASE)),
    ('web', re.compile(r'chrome|firefox|safari|edge|emby web|browser', re.IGNORECASE)),
    ('desktop', re.compile(r'windows|mac|linux|desktop|\bpc\b|^user-', re.IGNORECASE)),
)


def device_kind(name: str, app: str) -> str:
    text = f'{name} {app}'
    return next((kind for kind, pattern in _KINDS if pattern.search(text)), 'other')


def _epoch(value: Any) -> int | None:
    text = str(value or '').strip()
    if not text:
        return None
    # Emby writes seven fractional digits; Python accepts at most six.
    text = re.sub(r'(\.\d{6})\d+', r'\1', text.replace('Z', '+00:00'))
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


def ago_text(ts: int | None, now: float | None = None) -> str:
    """Beijing-time relative stamp matching the Mini App device list."""
    from datetime import timedelta, timezone
    if not ts:
        return '—'
    moment = time.time() if now is None else now
    seconds = moment - ts
    if seconds < 60:
        return '刚刚'
    if seconds < 3600:
        return f'{int(seconds // 60)} 分钟前'
    bj = timezone(timedelta(hours=8))
    then, today = datetime.fromtimestamp(ts, bj), datetime.fromtimestamp(moment, bj)
    days = (today.date() - then.date()).days
    if days == 0:
        return '今天 ' + then.strftime('%H:%M')
    if days == 1:
        return '昨天 ' + then.strftime('%H:%M')
    return f'{days} 天前' if days < 30 else then.strftime('%m-%d')


def shape(row: dict[str, Any]) -> dict[str, Any]:
    name = str(row.get('Name') or '').strip() or '未知设备'
    app = str(row.get('AppName') or '').strip()
    return {'id': str(row.get('Id') or ''), 'name': name, 'app': app,
            'version': str(row.get('AppVersion') or '').strip(),
            'last_active': _epoch(row.get('DateLastActivity')), 'kind': device_kind(name, app)}


class MemberDevices:
    def __init__(self, emby: Callable[[], Any]):
        self._emby = emby
        self._rows: list[dict[str, Any]] | None = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    async def _all(self, fresh: bool) -> list[dict[str, Any]]:
        async with self._lock:
            if fresh or self._rows is None or time.monotonic() - self._at > CACHE_SECONDS:
                self._rows = list(await self._emby().list_devices())
                self._at = time.monotonic()
            return self._rows

    async def for_user(self, user_id: str, *, fresh: bool = False) -> list[dict[str, Any]]:
        rows = [shape(r) for r in await self._all(fresh) if str(r.get('LastUserId') or '') == str(user_id)]
        return sorted((r for r in rows if r['id']), key=lambda r: -(r['last_active'] or 0))

    async def remove(self, user_id: str, device_id: str) -> dict[str, Any]:
        """Sign one of this member's devices out; never touches another account's device."""
        match = next((r for r in await self.for_user(user_id, fresh=True) if r['id'] == str(device_id)), None)
        if match is None:
            raise LookupError('设备不存在或已经移除')
        await self._emby().delete_device(match['id'])
        async with self._lock:
            self._rows = None
        return match
