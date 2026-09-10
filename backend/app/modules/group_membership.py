"""Linked Telegram chats: strict membership checks and scoped account deletion.

No Telegram member enumeration, no migration sweep, no invitation/ACL changes.
The existing member deletion service still owns Emby-first deletion and history.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import copy
import hashlib
import json
import re
import secrets
import threading
import time
from functools import partial
from html import escape
from typing import Any, ClassVar
from urllib.parse import urlsplit

from app.core.errors import ConfigError
from app.modules.member_ops import execute_delete
from app.modules.plugins import Field, Plugin, Spec

RULE_DEFAULTS = {'targets': [], 'gate_enabled': False, 'delete_enabled': False,
                 'generation': '', 'enabled_since': 0}
PLUGIN_ID = 'group_membership'


def normalize_rules(raw: Any, *, verified: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError('群组/频道规则必须是对象')
    out = copy.deepcopy(RULE_DEFAULTS)
    for key in ('gate_enabled', 'delete_enabled'):
        value = raw.get(key, False)
        if not isinstance(value, bool):
            raise ConfigError('关联规则开关必须是布尔值')
        out[key] = value
    targets = raw.get('targets', [])
    if not isinstance(targets, list) or len(targets) > 64:
        raise ConfigError('最多关联64个群组或频道')
    seen = set()
    for item in targets:
        if not isinstance(item, dict):
            raise ConfigError('关联目标格式错误')
        cid = str(item.get('chat_id') or '').strip()
        if not re.fullmatch(r'-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{3,31}', cid):
            raise ConfigError('关联目标须填写聊天数字ID或@名称')
        cid = str(int(cid)) if not cid.startswith('@') else cid.lower()
        if cid in seen:
            raise ConfigError('关联目标不能重复')
        seen.add(cid)
        enabled = item.get('enabled', True)
        if not isinstance(enabled, bool):
            raise ConfigError('关联项目开关必须是布尔值')
        url = str(item.get('join_url') or '').strip()
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise ConfigError('加入链接格式无效') from exc
        if url and (len(url) > 1024 or parsed.scheme != 'https'
                    or parsed.hostname not in ('t.me', 'telegram.me')
                    or parsed.username or parsed.password or port):
            raise ConfigError('加入链接须为不含凭据的 https://t.me/ 链接')
        row = {'chat_id': cid, 'join_url': url, 'enabled': enabled,
               'title': '', 'type': '', 'verification': 'unverified'}
        if verified:
            row.update({key: item.get(key, row[key]) for key in ('title', 'type', 'verification')})
        out['targets'].append(row)
    active = [t for t in out['targets'] if t['enabled']]
    if out['gate_enabled'] or out['delete_enabled']:
        if not active:
            raise ConfigError('启用前请先关联群组或频道')
        if not verified or any(t['verification'] != 'ready' for t in active):
            raise ConfigError('关联目标或Bot权限未核实，不能启用门禁/删除')
    return out


def presence(row: Any, uid: str) -> str:
    if not isinstance(row, dict) or str((row.get('user') or {}).get('id')) != str(uid):
        return 'unknown'
    status = row.get('status')
    if status in ('creator', 'administrator', 'member'):
        return 'present'
    if status in ('left', 'kicked'):
        return 'absent'
    if status == 'restricted' and isinstance(row.get('is_member'), bool):
        return 'present' if row['is_member'] else 'absent'
    return 'unknown'


class GroupMembership:
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.db = bot._db
        self.members = bot._members
        self._query_slots = asyncio.Semaphore(3)
        self._locks: dict[str, asyncio.Lock] = {}
        self._scan_task: asyncio.Task | None = None
        self._epoch = 0
        self._member_epochs: dict[str, int] = {}
        self._event_heads: dict[str, list] = {}
        self._event_locks: dict[str, asyncio.Lock] = {}
        self._io_slots = asyncio.Semaphore(4)
        # These are the two rolling UI results, not cached member authority.
        # Initial construction happens before the Bot serves updates.
        self._last_event_key = self._key('last_event')
        self._last_event = self._read_meta('last_event')
        saved = self._read_meta('latest')
        self._latest = saved if isinstance(saved, dict) else {}
        if self._latest.get('running'):
            self._latest.update(running=False, interrupted=True)

    def _key(self, suffix: str) -> str:
        return f'telegram.membership:{self.bot._token().split(":", 1)[0]}:{suffix}'

    def _load(self, suffix: str) -> Any:
        if suffix == 'last_event':
            return copy.deepcopy(self._last_event) if self._last_event_key == self._key(suffix) else None
        return self._read_meta(suffix)

    def _read_meta(self, suffix: str, *, key: str | None = None) -> Any:
        if self.db is None:
            return None
        row = self.db.one('SELECT value FROM meta WHERE key=?', (key or self._key(suffix),))
        try:
            return json.loads(row.get('value')) if row else None
        except (TypeError, ValueError):
            return None

    def _save(self, suffix: str, data: Any, *, key: str | None = None) -> None:
        if self.db is not None:
            self.db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                            (key or self._key(suffix), json.dumps(data, ensure_ascii=False)))
        if suffix == 'last_event' and (key is None or key == self._key(suffix)):
            self._last_event_key = key or self._key(suffix)
            self._last_event = data

    async def _io(self, fn: Any, *args: Any, cancel_flag: threading.Event | None = None, **kwargs: Any) -> Any:
        # SQLite calls cannot be cancelled. Keep the slot/owner until the
        # worker settles, including repeated shutdown cancellation requests.
        async with self._io_slots:
            worker = asyncio.get_running_loop().run_in_executor(
                None, partial(contextvars.copy_context().run, fn, *args, **kwargs))
            return await self._settle(worker, cancel_flag=cancel_flag)

    async def _settle(self, worker: Any, *, cancel_flag: threading.Event | None = None) -> Any:
        cancelled = None
        while True:
            try:
                result = await asyncio.shield(worker)
                break
            except asyncio.CancelledError as exc:
                if worker.cancelled():
                    raise
                cancelled = exc
                if cancel_flag is not None:
                    cancel_flag.set()
        if cancelled is not None:
            raise cancelled
        return result

    async def _persist(self, suffix: str, data: Any, *, key: str | None = None) -> None:
        await self._io(self._save, suffix, copy.deepcopy(data), key=key or self._key(suffix))

    def rules(self) -> dict[str, Any]:
        return self.bot._cfg().get('membership_rules') or copy.deepcopy(RULE_DEFAULTS)

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps([self.bot._token(), self.rules(), self._epoch],
                                         sort_keys=True).encode()).hexdigest()

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        async with self._query_slots:
            try:
                return await self.bot._call(method, payload, timeout=15)
            except Exception:  # noqa: BLE001 - unknown is never absence
                return None

    async def verify_target(self, target: dict[str, Any]) -> dict[str, Any]:
        row = {**target, 'verification': 'unknown', 'title': '', 'type': ''}
        if not self.bot.enabled:
            return row
        chat = await self.call('getChat', {'chat_id': target['chat_id']})
        if not isinstance(chat, dict) or chat.get('type') not in ('group', 'supergroup', 'channel'):
            return row
        cid = str(chat.get('id') or '')
        if not re.fullmatch(r'-\d+', cid):
            return row
        # A migrated/renamed identity may not silently replace a saved chat ID.
        if not str(target['chat_id']).startswith('@') and cid != str(target['chat_id']):
            return row
        row.update(chat_id=cid, title=str(chat.get('title') or cid), type=chat['type'])
        bot_id = self.bot._token().split(':', 1)[0]
        if not bot_id.isdigit():
            return row
        own = await self.call('getChatMember', {'chat_id': cid, 'user_id': int(bot_id)})
        if (isinstance(own, dict) and str((own.get('user') or {}).get('id')) == bot_id
                and own.get('status') in ('administrator', 'creator')):
            row['verification'] = 'ready'
        else:
            row['verification'] = 'permission_unknown'
        return row

    async def prepare_rules(self, raw: dict[str, Any], *, force_verify: bool = False) -> dict[str, Any]:
        # First parse the draft without trusting browser-supplied verification.
        if not isinstance(raw, dict):
            raise ConfigError('群组/频道规则必须是对象')
        draft = normalize_rules({**raw, 'gate_enabled': False, 'delete_enabled': False})
        current = self.rules()
        switches = ('gate_enabled', 'delete_enabled')
        same_targets = [{k: t[k] for k in ('chat_id', 'join_url', 'enabled')} for t in draft['targets']] == [
            {k: t[k] for k in ('chat_id', 'join_url', 'enabled')} for t in current['targets']]
        reducing = (all(isinstance(raw.get(k, False), bool) for k in switches)
                    and all(not raw.get(k, False) or current[k] for k in switches)
                    and any(current[k] and not raw.get(k, False) for k in switches))
        if not force_verify and same_targets and reducing:
            return normalize_rules({**current, **{k: raw.get(k, False) for k in switches}}, verified=True)
        if (not force_verify and (current['gate_enabled'] or current['delete_enabled'])
                and raw.get('gate_enabled') is False and raw.get('delete_enabled') is False):
            # Turning off protection must not wait for an unavailable Telegram
            # query while an earlier scan is still running. Re-enable rechecks.
            return draft
        draft['targets'] = [await self.verify_target(t) for t in draft['targets']]
        draft.update(gate_enabled=raw.get('gate_enabled', False), delete_enabled=raw.get('delete_enabled', False))
        return normalize_rules(draft, verified=True)

    async def check(self, tg_id: str) -> dict[str, Any]:
        rows = []
        active = [t for t in self.rules()['targets'] if t['enabled']]
        if not active or not str(tg_id).isdigit():
            return {'state': 'unknown', 'targets': rows}
        for target in active:
            verified = await self.verify_target(target)
            state = 'unknown'
            if verified['verification'] == 'ready':
                result = await self.call('getChatMember', {'chat_id': verified['chat_id'], 'user_id': int(tg_id)})
                state = presence(result, tg_id)
            rows.append({**verified, 'state': state})
        states = {r['state'] for r in rows}
        state = 'unknown' if 'unknown' in states else 'absent' if 'absent' in states else 'present'
        return {'state': state, 'targets': rows}

    async def admin_state(self, member: dict[str, Any]) -> str:
        if self.bot.is_admin(member):
            return 'exempt'
        try:
            users = await self.bot._emby.list_users()
            user = next((u for u in users if str(u.get('Id')) == str(member['emby_user_id'])), None)
            policy = (user or {}).get('Policy') or {}
            flag = policy.get('IsAdministrator')
            if not isinstance(flag, bool):
                return 'unknown'
            return 'exempt' if flag else 'ordinary'
        except Exception:  # noqa: BLE001 - administrator identity must be certain
            return 'unknown'

    def _current(self, before: dict[str, Any], fingerprint: str, event: tuple | None) -> bool:
        now = self.db.one('SELECT tg_user_id,tg_bound_at,created_at,roles FROM members WHERE emby_user_id=?',
                          (before['emby_user_id'],))
        return (self.bot.enabled and self.rules()['delete_enabled'] and fingerprint == self.fingerprint()
                and bool(now) and now.get('tg_user_id') == before.get('tg_user_id')
                and now.get('created_at') == before.get('created_at')
                and now.get('tg_bound_at') == before.get('tg_bound_at')
                and 'admin' not in str(now.get('roles') or '').split(',')
                and (event is None or self._event_heads.get(self._key(event[0])) == event[1]))

    async def inspect(self, before: dict[str, Any], *, source: str,
                      fingerprint: str, event: tuple | None = None) -> dict[str, Any]:
        uid = str(before['emby_user_id'])
        async with self._locks.setdefault(uid, asyncio.Lock()):
            row = {'user_id': uid, 'username': before.get('username', ''), 'tg_user_id': before.get('tg_user_id', ''),
                   'group_id': before.get('group_id', ''), 'source': source, 'state': 'unknown', 'action': 'kept', 'targets': []}
            member = await self._io(self.members.get, uid)
            if (not member or member.get('tg_user_id') != before.get('tg_user_id')
                    or member.get('tg_bound_at') != before.get('tg_bound_at')):
                row['action'] = 'cancelled'
                return row
            if not member.get('tg_user_id'):
                row['state'] = 'unbound'
                return row
            admin = await self.admin_state(member)
            if admin != 'ordinary':
                row['state'] = admin
                return row
            row.update(await self.check(str(member['tg_user_id'])))
            if row['state'] != 'absent' or not self.rules()['delete_enabled']:
                row['action'] = 'detected'
                return row
            if not await self._io(self._current, member, fingerprint, event):
                row['action'] = 'cancelled'
                return row
            row['action'] = 'rechecking'
            if self._latest.get('running') and self._latest.get('source') == source:
                self._latest['current'] = {'username': row['username'], 'action': 'rechecking'}
            service = self
            bot_loop = asyncio.get_running_loop()
            cancelled = threading.Event()

            async def on_bot_loop(coroutine):
                return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coroutine, bot_loop))

            class CheckedDelete:
                async def list_users(self):
                    return await on_bot_loop(service.bot._emby.list_users())

                async def delete_user(self, user_id):
                    # This is called by execute_delete after its own awaited
                    # list operation: never delete using the earlier scan.
                    observed = service._member_epochs.get(str(member['tg_user_id']), 0)
                    check = await on_bot_loop(service.check(str(member['tg_user_id'])))
                    row['targets'] = check['targets']
                    latest = service.members.get(uid)  # already on the I/O worker
                    admin = await on_bot_loop(service.admin_state(latest)) if latest else 'unknown'
                    if (check['state'] != 'absent' or admin != 'ordinary'
                            or not service._current(member, fingerprint, event)
                            or cancelled.is_set()
                            or observed != service._member_epochs.get(str(member['tg_user_id']), 0)):
                        row.update(state=admin if admin != 'ordinary' else check['state'], action='cancelled')
                        raise ConfigError('自动删除复核未通过，账号保留')
                    return await service.bot._emby.delete_user(user_id)

            def delete_on_worker():
                # execute_delete's synchronous preview/authorize/audit/delete
                # all stay together on this worker. LiveEmby.delete_user owns
                # a per-call HTTP client, so the final fresh DB check has no
                # cross-loop scheduling gap before issuing the remote delete.
                return asyncio.run(execute_delete(self.members, CheckedDelete(), uid,
                    actor='telegram.membership:' + source, cascade=False, delete_emby=True, confirm_ids=[uid],
                    authorize=lambda: not cancelled.is_set() and self._current(member, fingerprint, event)))

            result = await self._io(delete_on_worker, cancel_flag=cancelled)
            if result.get('deleted'):
                row['action'] = 'deleted'
            elif row['action'] != 'cancelled':
                row['action'] = 'failed_retained'
            await self._io(self.members.audit, 'telegram.membership', 'telegram.membership.result', uid,
                json.dumps({'source': source, 'action': row['action'], 'state': row['state'],
                            'chats': [t['chat_id'] for t in row['targets']]}, ensure_ascii=False),
                ok=row['action'] != 'failed_retained')
            return row

    def status(self) -> dict[str, Any]:
        return copy.deepcopy(self._latest)

    def start_scan(self, source: str = 'manual') -> dict[str, Any]:
        if not self.bot.enabled or not any(t['enabled'] for t in self.rules()['targets']):
            raise ConfigError('请先启用Bot并保存至少一个关联目标')
        if self._scan_task and not self._scan_task.done():
            return self.status()
        self._latest = {'id': secrets.token_hex(8), 'running': True, 'source': source,
                        'started_at': time.time(), 'processed': 0, 'total': 0, 'rows': []}
        self._scan_task = asyncio.create_task(self._scan(source, self.fingerprint(), self._key('latest')))
        self.bot._in_flight.add(self._scan_task)
        self._scan_task.add_done_callback(self.bot._in_flight.discard)
        return self.status()

    def _progress_text(self) -> str:
        latest = self._latest or {}
        total = int(latest.get('total') or 0)
        processed = int(latest.get('processed') or 0)
        rows = latest.get('rows') or []
        counts: dict[str, int] = {}
        for row in rows:
            action = str((row or {}).get('action') or 'kept')
            counts[action] = counts.get(action, 0) + 1
        current = (latest.get('current') or {}).get('username') or ''
        if latest.get('running'):
            stage = '开始运行…' if processed == 0 else f'运行中 {processed}/{total}'
        elif latest.get('cancelled'):
            stage = '已取消'
        elif latest.get('error'):
            stage = '失败：' + str(latest.get('error'))
        else:
            stage = '已完成'
        extra = f'\n当前：{escape(str(current))}' if current else ''
        return (f'⚑ <b>关联群组/频道成员检测</b>\n{stage}{extra}\n'
                f'已核 {processed}/{total} · 删除 {counts.get("deleted", 0)} · '
                f'保留 {counts.get("kept", 0) + counts.get("detected", 0)} · '
                f'取消 {counts.get("cancelled", 0)}')

    async def _announce(self) -> None:
        post = getattr(self.bot, 'post_job_progress', None)
        if not callable(post):
            return
        with contextlib.suppress(Exception):
            await post('group_membership', self._progress_text())

    async def _scan(self, source: str, fingerprint: str, state_key: str) -> None:
        try:
            await self._persist('latest', self._latest, key=state_key)
            await self._announce()
            rows = await self._io(self.db.query,
                'SELECT emby_user_id,username,tg_user_id,tg_bound_at,group_id,created_at FROM members ORDER BY emby_user_id')
            self._latest['total'] = len(rows)
            await self._announce()
            for member in rows:
                if fingerprint != self.fingerprint() or not self.bot.enabled:
                    self._latest['cancelled'] = True
                    break
                self._latest['current'] = {'username': member['username'], 'action': 'checking'}
                self._latest['rows'].append(await self.inspect(member, source=source, fingerprint=fingerprint))
                self._latest['processed'] += 1
                self._latest['current'] = None
                processed = self._latest['processed']
                total = int(self._latest.get('total') or 0)
                if processed == 1 or processed == total or processed % 10 == 0:
                    await self._announce()
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            self._latest['cancelled'] = True
            raise
        except Exception as exc:  # noqa: BLE001 - keep partial progress, never expose secrets
            self._latest['error'] = type(exc).__name__
        finally:
            self._latest.update(running=False, current=None, finished_at=time.time())
            try:
                await self._persist('latest', self._latest, key=state_key)
            except Exception as exc:  # visible failure, never report durable success
                self._latest.update(error=type(exc).__name__, persistence_failed=True)
                self.bot._last_error = '成员检测结果持久化失败'
                raise
            await self._announce()

    async def handle_update(self, update: dict[str, Any]) -> bool:
        event = update.get('chat_member') or update.get('my_chat_member')
        message = update.get('message') or {}
        if not event and not any(k in message for k in ('left_chat_member', 'migrate_to_chat_id', 'migrate_from_chat_id')):
            return False
        event = event or message
        cid = str((event.get('chat') or {}).get('id') or '')
        if cid not in {t['chat_id'] for t in self.rules()['targets'] if t['enabled']}:
            return True
        if 'my_chat_member' in update or 'migrate_to_chat_id' in message or 'migrate_from_chat_id' in message:
            self._epoch += 1
            return True
        target = (event.get('new_chat_member') or {}).get('user') or message.get('left_chat_member') or {}
        tg_id = str(target.get('id') or '')
        date, update_id = event.get('date'), update.get('update_id')
        if (not tg_id.isdigit() or not isinstance(date, int) or not isinstance(update_id, int)
                or date <= self.rules().get('enabled_since', 0) or date > time.time() + 30):
            return True
        state = presence(event.get('new_chat_member'), tg_id) if 'new_chat_member' in event else 'absent'
        key = f'event:{cid}:{tg_id}'
        marker = [date, update_id, state]
        state_key = self._key(key)
        result_key = self._key('last_event')
        fingerprint = self.fingerprint()
        head = self._event_heads.get(state_key)
        if head and tuple(head[:2]) >= tuple(marker[:2]):
            return True
        # Invalidate before ANY await, including waiting for a shared SQLite
        # lock. Durability follows under a per-event-key ordering lock.
        self._event_heads[state_key] = marker
        self._member_epochs[tg_id] = self._member_epochs.get(tg_id, 0) + 1
        # Own the complete ordered read/write even when shutdown arrives while
        # waiting for the lock/read; an accepted join must not disappear merely
        # because cancellation came before its INSERT. No delete runs on cancel.
        current = await self._settle(asyncio.create_task(self._write_event(key, state_key, marker)))
        if not current or self._event_heads.get(state_key) != marker:
            return True
        was_present = bool(message.get('left_chat_member')) or presence(event.get('old_chat_member'), tg_id) == 'present'
        if state != 'absent' or not was_present or not self.rules()['delete_enabled']:
            return True
        member = await self._io(self.members.find_by_telegram, tg_id)
        if member and date > max(int(member.get('tg_bound_at') or 0), int(member.get('created_at') or 0)):
            # Second-resolution timestamps cannot prove an event belongs to a
            # binding created in the same second: leave it for a fresh scan.
            row = await self.inspect(member, source='event', fingerprint=fingerprint, event=(key, marker))
            await self._persist('last_event', {**row, 'at': time.time()}, key=result_key)
        return True

    async def _write_event(self, key: str, state_key: str, marker: list) -> bool:
        async with self._event_locks.setdefault(state_key, asyncio.Lock()):
            old = await self._io(self._read_meta, key, key=state_key)
            if old and tuple(old[:2]) >= tuple(marker[:2]):
                if self._event_heads.get(state_key) == marker:
                    self._event_heads[state_key] = old
                return False
            if self._event_heads.get(state_key) != marker:
                return False
            await self._persist(key, marker, key=state_key)
            return True

    async def gate(self, chat_id: Any, tg_id: str) -> bool:
        if not self.rules()['gate_enabled']:
            return True
        member = await self._io(self.bot._member_for_chat, tg_id)
        if member and await self.admin_state(member) == 'exempt':
            return True
        fingerprint = self.fingerprint()
        observed = self._member_epochs.get(tg_id, 0)
        result = await self.check(tg_id)
        if fingerprint != self.fingerprint() or observed != self._member_epochs.get(tg_id, 0):
            result['state'] = 'unknown'
        if result['state'] == 'present':
            return True
        self.bot._pending.pop(self.bot._pkey(chat_id), None)
        title = '⏳ 暂时无法核实成员状态' if result['state'] == 'unknown' else '🔒 请先加入群组并关注频道'
        lines = [f'<b>{title}</b>', '所有启用的关联项都需满足。']
        labels = {'present': '已完成', 'absent': '未加入 / 未关注', 'unknown': '暂无法验证'}
        buttons = []
        for target in result['targets']:
            name = escape(str(target.get('title') or target['chat_id']))
            lines.append(name + '：' + labels[target['state']])
            if target['join_url']:
                buttons.append([{'text': ('关注频道' if target.get('type') == 'channel' else '加入群组') + ' · ' + str(target.get('title') or target['chat_id']),
                                 'url': target['join_url']}])
        buttons.append([{'text': '✓ 我已完成，重新核实', 'callback_data': 'membership_recheck'}])
        if result['state'] == 'unknown':
            lines.append('查询失败不会被当作退群，请稍后重试或联系管理员。')
        await self.bot._show(chat_id, '\n'.join(lines), buttons)
        return False


class GroupMembershipPlugin(Plugin):
    """Use the existing scheduler/calendar/enable switch, not a second timer."""
    FIELDS: ClassVar[list[Field]] = [Field('mode', '检测周期', kind='select', default='daily', options=[('daily', '每天'), ('interval', '固定间隔')]),
              Field('hour', '每天执行时间（整点）', kind='int', default=4, min=0, max=23),
              Field('interval_hours', '间隔小时', kind='int', default=6, min=1, max=168)]

    def __init__(self, service: GroupMembership, registry: Any):
        self.service, self.registry = service, registry

    def defaults(self):
        return {field.key: field.default for field in self.FIELDS}

    @property
    def spec(self):
        cfg = self.registry.config(PLUGIN_ID) if self.registry.get(PLUGIN_ID) else self.defaults()
        return Spec(id=PLUGIN_ID, name='关联群组/频道成员检测', category='task', icon='⚑', fields=self.FIELDS,
            description='核查已绑定TG会员；删除开关关闭时仅检测，开启时复核后可删除存量不合规本人，管理员豁免，白名单适用。',
            interval=int(cfg['interval_hours']) * 3600 if cfg['mode'] == 'interval' else 0,
            hour=int(cfg['hour']) if cfg['mode'] == 'daily' else None)

    async def run(self, config):
        status = self.service.start_scan('scheduled_or_manual_task')
        return {'ok': True, '检测任务': status['id'], '提示': '已启动或已有检测进行中，请在成员检测页查看进度/结果'}
