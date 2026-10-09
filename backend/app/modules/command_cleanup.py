"""Only the current, trusted, recognized group command after a public response.

No history scan, no reply-target deletion. A lost delete ACK retries the same ID.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar

from app.modules.economy_rules import economy_write, encode
from app.modules.group_points import COMMANDS as POINT_COMMANDS
from app.modules.group_points import GroupPointsError, reliable_user
from app.modules.plugins import Plugin, Spec
from app.modules.red_packets import COMMANDS as PACKET_COMMANDS
from app.modules.report_delivery import CALL_DELIVERY

TARGET = ContextVar('group_command_response', default=None)
ENTRY_COMMANDS = {'/start', '/help', '/rules', '/cancel', '/usage', '/me', '/myinfo', '/rank', '/today',
                  '/manage', '/requests', '/request', '/req', '/uploader', '/register', '/claim', '/rebind', '/resetpw',
                  '/blackwhite', '/黑白板', '/牛牛', '/niuniu', '/牛牛帮助', '/炸金花', '/poker', '/zjh', '/看牌', '/炸金花帮助',
                  '/积分榜', '/pointsrank', '/游戏', '/games', '/玩法', '/刮刮乐', '/scratch9'} | set(POINT_COMMANDS) | set(PACKET_COMMANDS)


def capture_response(method, sent, result):
    target = TARGET.get()
    if (target and method in ('sendMessage', 'sendPhoto', 'editMessageText', 'editMessageMedia', 'editMessageCaption')
            and str(sent.get('chat_id')) == str(target['chat_id'])
            and (isinstance(result, dict) and type(result.get('message_id')) is int and result['message_id'] > 0
                 or result is True and method.startswith('editMessage'))):
        target['responded'] = True


class CommandCleanupService:
    def __init__(self, db):
        self.db = db

    def enqueue(self, target, *, now=None):
        clock = time.time() if now is None else now
        identity = {k: target[k] for k in ('chat_id', 'thread_id', 'message_id', 'actor_tg_id', 'verb')}
        bot = target['bot_id']
        if (not str(bot).isdecimal() or int(bot) <= 0 or type(identity['chat_id']) is not int or identity['chat_id'] >= 0
                or type(identity['message_id']) is not int or identity['message_id'] <= 0
                or not str(identity['actor_tg_id']).isdecimal() or not identity['verb'].startswith('/')):
            raise ValueError('invalid command provenance')
        key = f'command:{identity["chat_id"]}:{identity["message_id"]}'
        with economy_write(self.db) as conn:
            existing = conn.execute('SELECT * FROM play_jobs WHERE bot_id=? AND job_key=?', (bot, key)).fetchone()
            if existing:
                if existing['payload_json'] != encode(identity):
                    raise ValueError('different original command identity')
                return
            conn.execute("INSERT INTO play_jobs(bot_id,job_key,kind,payload_json,created_at,due_at,expires_at) VALUES(?,?,'group.command.delete',?,?,?,?)",
                         (bot, key, encode(identity), clock, clock, clock+47*3600))

    def claim(self, bot, *, now=None):
        clock = time.time() if now is None else now
        with economy_write(self.db) as conn:
            row = conn.execute("SELECT * FROM play_jobs WHERE kind='group.command.delete' AND bot_id=? AND due_at<=? AND (state='queued' OR (state='running' AND lease_until<=?)) ORDER BY id LIMIT 1", (bot, clock, clock)).fetchone()
            if not row:
                return None
            if row['expires_at'] <= clock:
                conn.execute("UPDATE play_jobs SET state='expired' WHERE id=?", (row['id'],))
                return {'skip': True}
            lease = secrets.token_hex(12)
            conn.execute("UPDATE play_jobs SET state='running',lease_until=?,lease_token=?,attempts=attempts+1 WHERE id=?", (clock+45, lease, row['id']))
            return dict(row, lease_token=lease, attempts=row['attempts']+1)

    def finish(self, row, state, *, delay=0, now=None):
        self.db.execute("UPDATE play_jobs SET state=?,due_at=?,lease_until=0,lease_token='',last_error=? WHERE id=? AND state='running' AND lease_token=?",
                        (state, (time.time() if now is None else now)+delay, '' if state in ('deleted', 'gone') else '群命令删除未确认或权限不足；业务不受影响', row['id'], row['lease_token']))

    def summary(self):
        return {r['state']: r['n'] for r in self.db.query("SELECT state,COUNT(*) n FROM play_jobs WHERE kind='group.command.delete' GROUP BY state")}


class GroupCommandCleanupMixin:
    @contextmanager
    def _group_command_context(self, message):
        target = None
        chat = message.get('chat') or {}
        parts = str(message.get('text') or '').strip().split()
        first = parts[0].lower() if parts else ''
        verb = first.split('@', 1)[0]
        recognized = bool(self._checkin_command(str(message.get('text') or '').strip())) or verb in ENTRY_COMMANDS or (verb.startswith('/') and callable(getattr(self, '_cmd_'+verb[1:], None)))
        if (self._db is not None and self._plugin_on('group_command_cleanup') and recognized
                and chat.get('type') in ('group', 'supergroup') and type(chat.get('id')) is int and chat['id'] < 0
                and self._group_chat_allowed(chat) and type(message.get('message_id')) is int and message['message_id'] > 0
                and not any(message.get(k) for k in ('sender_chat', 'forward_origin', 'forward_date', 'is_automatic_forward'))):
            try:
                actor = reliable_user(message)
                self._check_bot_identity()
                addressed = '@' not in first or (self._bot_username and first.split('@', 1)[1] == self._bot_username.lower())
                if addressed and self._active_bot_id.isdecimal():
                    target = {'bot_id': self._active_bot_id, 'chat_id': chat['id'], 'thread_id': self._thread_id(message) or 0,
                              'message_id': message['message_id'], 'actor_tg_id': actor, 'verb': verb, 'responded': False}
            except GroupPointsError:
                pass
        token = TARGET.set(target)
        try:
            yield
        finally:
            TARGET.reset(token)
            if target and target['responded']:
                # Persistence failure is housekeeping failure, never a reason to repeat business.
                try:
                    CommandCleanupService(self._db).enqueue(target)
                except Exception:  # noqa: BLE001, S110 - never repeat business due to housekeeping
                    pass

    async def _drain_group_commands(self, *, now=None, limit=30):
        if self._db is None or not self.enabled or not self._plugin_on('group_command_cleanup'):
            return
        from app.modules.telegram import _CALL_ERROR, _QUIET_CALL
        self._check_bot_identity()
        service = CommandCleanupService(self._db)
        for _ in range(limit):
            row = service.claim(self._active_bot_id, now=now)
            if row is None:
                break
            if row.get('skip'):
                continue
            target = json.loads(row['payload_json'])
            if not self._group_chat_allowed({'id': target['chat_id'], 'type': 'supergroup'}):
                service.finish(row, 'blocked', now=now)
                continue
            quiet = _QUIET_CALL.set(True)
            error_token = _CALL_ERROR.set(None)
            receipt = CALL_DELIVERY.set(None)
            try:
                try:
                    result = await self._call('deleteMessage', {'chat_id': target['chat_id'], 'message_id': target['message_id']}, timeout=10)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - exact-target delete is idempotent
                    result = None
                error = str(_CALL_ERROR.get() or '').lower()
                failure = CALL_DELIVERY.get() or {}
                if result is True:
                    state = 'deleted'
                elif 'message to delete not found' in error or 'message_id_invalid' in error:
                    state = 'gone'
                elif failure.get('state') == 'failed':
                    state = 'blocked'
                else:
                    state = 'queued' if row['attempts'] < 20 else 'failed'
                delay = max(int(failure.get('retry_after') or 0), min(300, 5*2**min(row['attempts'], 6)))
                service.finish(row, state, delay=delay, now=now)
            finally:
                CALL_DELIVERY.reset(receipt)
                _CALL_ERROR.reset(error_token)
                _QUIET_CALL.reset(quiet)


class GroupCommandCleanupPlugin(Plugin):
    spec = Spec(id='group_command_cleanup', name='群指令清理', icon='🧹', category='points',
                description='授权群内本人当前指令成功响应后删原命令；不删私聊、普通对话、回复目标或扫描历史。权限不足不影响业务。', fields=[])

    def readonly_status(self):
        return CommandCleanupService(self.ctx.db).summary()

    async def run(self, config):
        if self.ctx.telegram:
            await self.ctx.telegram._drain_group_commands()
        return self.readonly_status()
