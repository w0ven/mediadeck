"""Signed-in devices as Emby sees them: one entry per active (device name, player) pair.

Emby keeps a device record while any token row exists—even IsActive=0 rows left
behind by sign-out or password resets—and players register a fresh ReportedDeviceId
whenever a user adds a backup server line or reinstalls the app without signing out.
When the local Emby auth database is configured, only IsActive=1 personal tokens count
as signed in and fully inactive ghosts are pruned. Within one account, active records
sharing the same non-generic device name and player app merge into a single logical
device for display, cap counting, removal, and auto-logout. The observational
``devices`` table (device groups) is unaffected.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.modules.device_groups import _NAME_WHITESPACE, _UNKNOWN_NAMES

CACHE_SECONDS = 20.0
WHITELIST_MAX_DEVICES = 10
# A device used this recently may be mid-playback; it waits for the next sweep.
RECENT_GRACE_SECONDS = 600

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


def shape(row: dict[str, Any], token_entry: dict[str, Any] | None = None) -> dict[str, Any]:
    raw_name = str(row.get('Name') or '').strip(_NAME_WHITESPACE)
    name = raw_name or '未知设备'
    if token_entry is None:
        app = str(row.get('AppName') or '').strip(_NAME_WHITESPACE)
        version = str(row.get('AppVersion') or '').strip()
        last_active = _epoch(row.get('DateLastActivity'))
    elif str(row.get('LastUserId') or '') == str(token_entry.get('user_id') or ''):
        app = str(row.get('AppName') or token_entry.get('app') or '').strip(_NAME_WHITESPACE)
        version = str(row.get('AppVersion') or token_entry.get('version') or '').strip()
        stamps = [ts for ts in (_epoch(row.get('DateLastActivity')), token_entry.get('last_active'))
                  if ts is not None]
        last_active = max(stamps) if stamps else None
    else:
        app = str(token_entry.get('app') or row.get('AppName') or '').strip(_NAME_WHITESPACE)
        version = str(token_entry.get('version') or '').strip()
        last_active = token_entry.get('last_active')
    return {'id': str(row.get('Id') or ''), 'name': name, 'app': app,
            'version': version, 'last_active': last_active, 'kind': device_kind(name, app)}


def _merge_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse same-(name, app) installs for one user while keeping all underlying IDs."""
    ordered = sorted((r for r in rows if r.get('id')),
                     key=lambda r: (-(r['last_active'] or 0), r['id']))
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in ordered:
        did = str(row['id'])
        clean_name = str(row.get('name') or '').strip(_NAME_WHITESPACE)
        clean_app = str(row.get('app') or '').strip(_NAME_WHITESPACE)
        if clean_name.lower() not in _UNKNOWN_NAMES and clean_app:
            key: tuple[str, ...] = ('name_app', clean_name, clean_app)
        else:
            key = ('id', did)
        group = groups.get(key)
        if group is None:
            groups[key] = dict(row, ids=[did])
        else:
            if did not in group['ids']:
                group['ids'].append(did)
            if not group.get('version') and row.get('version'):
                group['version'] = row['version']
    return list(groups.values())


def _public_row(group: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in group.items() if k != 'ids'}


def device_cap(member: dict[str, Any] | None) -> int | None:
    """Signed-in device limit; None means uncapped (operators, accounts outside levels)."""
    from app.modules.groups import WHITELIST_GROUP_ID
    if not member or 'admin' in str(member.get('roles') or ''):
        return None
    if member.get('group_id') == WHITELIST_GROUP_ID:
        return WHITELIST_MAX_DEVICES
    level = member.get('activity_level') if member.get('activity_managed') else None
    cap = (level or {}).get('max_devices')
    return int(cap) if isinstance(cap, int) and cap > 0 else None


class MemberDevices:
    def __init__(self, emby: Callable[[], Any]):
        self._emby = emby
        self._snapshot_data: tuple[list[dict[str, Any]], dict[str, Any] | None] | None = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._snapshot_data = None

    async def _snapshot(self, fresh: bool) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        async with self._lock:
            if (fresh or self._snapshot_data is None
                    or time.monotonic() - self._at > CACHE_SECONDS):
                emby = self._emby()
                rows = list(await emby.list_devices())
                reader = getattr(emby, 'device_token_state', None)
                token_state = await reader() if reader is not None else None
                self._snapshot_data = (rows, token_state)
                self._at = time.monotonic()
            return self._snapshot_data

    async def _grouped_for_user(self, user_id: str, *, fresh: bool = False) -> list[dict[str, Any]]:
        uid = str(user_id)
        rows, token_state = await self._snapshot(fresh)
        if token_state is None:
            matched = [shape(r) for r in rows if str(r.get('LastUserId') or '') == uid]
        else:
            active_by_device = token_state.get('active_by_device') or {}
            matched = []
            for row in rows:
                did = str(row.get('Id') or '')
                if not did:
                    continue
                entry = next((e for e in active_by_device.get(did, ())
                              if str(e.get('user_id') or '') == uid), None)
                if entry is not None:
                    matched.append(shape(row, entry))
        return _merge_rows(matched)

    async def for_user(self, user_id: str, *, fresh: bool = False) -> list[dict[str, Any]]:
        return [_public_row(g) for g in await self._grouped_for_user(user_id, fresh=fresh)]

    async def enforce(self, cap_for: Callable[[str], int | None], *, budget: int = 40,
                      now: float | None = None) -> list[dict[str, Any]]:
        """Sign out each capped member's least recently used logical devices beyond the cap,
        and sweep fully inactive ghost device rows within the remaining budget.

        Emby administrators and system/API-key devices are never touched. At most
        ``budget`` deletions per call keep a sweep from flooding Emby.
        """
        moment = time.time() if now is None else now
        emby = self._emby()
        admins = {str(u.get('Id')) for u in await emby.list_users()
                  if (u.get('Policy') or {}).get('IsAdministrator')}
        rows, token_state = await self._snapshot(True)
        by_user: dict[str, list[dict[str, Any]]] = {}
        admin_devices: set[str] = set()
        if token_state is None:
            for row in rows:
                did = str(row.get('Id') or '')
                uid = str(row.get('LastUserId') or '')
                if not did or not uid:
                    continue
                if uid in admins:
                    admin_devices.add(did)
                else:
                    by_user.setdefault(uid, []).append(shape(row))
        else:
            active_by_device = token_state.get('active_by_device') or {}
            for row in rows:
                did = str(row.get('Id') or '')
                if not did:
                    continue
                entries = active_by_device.get(did, ())
                if any(str(e.get('user_id') or '') in admins for e in entries):
                    admin_devices.add(did)
                    continue
                for entry in entries:
                    uid = str(entry.get('user_id') or '')
                    if uid and uid not in admins:
                        by_user.setdefault(uid, []).append(shape(row, entry))

        removed: list[dict[str, Any]] = []
        deleted_ids: set[str] = set()
        ops = 0
        for uid, user_rows in by_user.items():
            cap = cap_for(uid)
            if cap is None:
                continue
            groups = _merge_rows(user_rows)
            if len(groups) <= cap:
                continue
            for group in groups[cap:]:
                if ops >= budget:
                    break
                target_ids = [did for did in group['ids']
                              if did not in deleted_ids and did not in admin_devices]
                if not target_ids:
                    continue
                if group['last_active'] and moment - group['last_active'] < RECENT_GRACE_SECONDS:
                    continue
                if ops > 0 and ops + len(target_ids) > budget:
                    break
                if len(target_ids) > budget:
                    target_ids = target_ids[:budget]
                for did in target_ids:
                    await emby.delete_device(did)
                    deleted_ids.add(did)
                    ops += 1
                removed.append(dict(_public_row(group), user_id=uid, cap=cap))

        if token_state is not None and ops < budget:
            dead_ids = token_state.get('dead_device_ids') or set()
            for row in rows:
                if ops >= budget:
                    break
                did = str(row.get('Id') or '')
                uid = str(row.get('LastUserId') or '')
                if (not did or did in deleted_ids or did in admin_devices
                        or did not in dead_ids or not uid or uid in admins):
                    continue
                await emby.delete_device(did)
                deleted_ids.add(did)
                ops += 1

        if deleted_ids:
            async with self._lock:
                self._snapshot_data = None
        return removed

    async def prune_inactive_for_user(self, user_id: str, *, budget: int = 40) -> int:
        """Drop cache and delete fully inactive device rows owned by ``user_id``."""
        uid = str(user_id or '').strip()
        async with self._lock:
            self._snapshot_data = None
        if not uid:
            return 0
        emby = self._emby()
        reader = getattr(emby, 'device_token_state', None)
        if reader is None:
            return 0
        token_state = await reader()
        if token_state is None:
            return 0
        dead_ids = token_state.get('dead_device_ids') or set()
        if not dead_ids:
            return 0
        admins = {str(u.get('Id')) for u in await emby.list_users()
                  if (u.get('Policy') or {}).get('IsAdministrator')}
        if uid in admins:
            return 0
        rows = list(await emby.list_devices())
        pruned = 0
        for row in rows:
            if pruned >= budget:
                break
            did = str(row.get('Id') or '')
            if did and did in dead_ids and str(row.get('LastUserId') or '') == uid:
                await emby.delete_device(did)
                pruned += 1
        if pruned:
            async with self._lock:
                self._snapshot_data = None
        return pruned

    async def remove(self, user_id: str, device_id: str) -> dict[str, Any]:
        """Sign one of this member's logical devices out (all matching underlying IDs)."""
        target = str(device_id)
        groups = await self._grouped_for_user(user_id, fresh=True)
        match = next((g for g in groups if target in g['ids']), None)
        if match is None:
            raise LookupError('设备不存在或已经移除')
        emby = self._emby()
        for did in match['ids']:
            await emby.delete_device(did)
        async with self._lock:
            self._snapshot_data = None
        return _public_row(match)
