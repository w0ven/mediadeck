"""Only explicitly recorded group check-in messages may be deleted."""
from __future__ import annotations

import json
import secrets
import time
from contextvars import ContextVar

from app.modules.economy_rules import economy_write, encode
from app.modules.plugins import Field, Plugin, Spec

CHECKIN_TARGET: ContextVar[dict | None] = ContextVar('checkin_cleanup_target', default=None)


class CleanupService:
    def __init__(self, db):
        self.db = db

    def enqueue(self, bot_id, chat, thread, mid, source, actor, *, now=None):
        if (not str(bot_id).isdecimal() or int(bot_id) <= 0 or type(chat) is not int or chat >= 0
                or type(mid) is not int or mid <= 0 or type(thread) is not int or thread < 0
                or source not in ('command', 'feedback') or not str(actor).isdecimal()):
            raise ValueError('invalid check-in deletion provenance')
        clock = time.time() if now is None else float(now)
        payload = {'chat_id': chat, 'thread_id': thread, 'message_id': mid,
                   'source': source, 'actor_tg_id': str(actor)}
        key = f'checkin:{chat}:{mid}'
        with economy_write(self.db) as conn:
            row = conn.execute('SELECT * FROM play_jobs WHERE bot_id=? AND job_key=?', (str(bot_id), key)).fetchone()
            if row:
                if row['payload_json'] != encode(payload):
                    raise ValueError('message already belongs to a different cleanup target')
                return dict(row)
            conn.execute("INSERT INTO play_jobs(bot_id,job_key,kind,payload_json,created_at,due_at,expires_at) VALUES(?,?,'checkin.delete',?,?,?,?)",
                         (str(bot_id), key, encode(payload), clock, clock + 60, clock + 48 * 3600))
            return dict(conn.execute('SELECT * FROM play_jobs WHERE bot_id=? AND job_key=?', (str(bot_id), key)).fetchone())

    def claim(self, bot_id, *, now=None):
        clock = time.time() if now is None else float(now)
        with economy_write(self.db) as conn:
            row = conn.execute("SELECT * FROM play_jobs WHERE kind='checkin.delete' AND due_at<=? AND (state='queued' OR (state='running' AND lease_until<=?)) ORDER BY due_at,id LIMIT 1", (clock, clock)).fetchone()
            if not row:
                return None
            if row['bot_id'] != str(bot_id):
                conn.execute("UPDATE play_jobs SET state='blocked',last_error='Bot归属变化，未删除' WHERE id=?", (row['id'],))
                return {'skip': True}
            if row['expires_at'] <= clock:
                conn.execute("UPDATE play_jobs SET state='expired',last_error='超过Telegram删除时限，未删除' WHERE id=?", (row['id'],))
                return {'skip': True}
            lease = secrets.token_hex(12)
            conn.execute("UPDATE play_jobs SET state='running',lease_until=?,lease_token=?,attempts=attempts+1 WHERE id=?",
                         (clock + 45, lease, row['id']))
            out = dict(row)
            out.update(lease_token=lease, attempts=row['attempts'] + 1)
            return out

    def finish(self, job, state, error='', *, retry_after=0, now=None):
        if state not in ('deleted', 'gone', 'blocked', 'queued', 'failed'):
            raise ValueError('invalid deletion status')
        clock = time.time() if now is None else float(now)
        with economy_write(self.db) as conn:
            conn.execute('UPDATE play_jobs SET state=?,last_error=?,due_at=?,lease_until=0,lease_token=? WHERE id=? AND state=? AND lease_token=?',
                         (state, error, clock + retry_after, '', job['id'], 'running', job['lease_token']))

    def summary(self):
        rows = self.db.query("SELECT state,COUNT(*) AS n FROM play_jobs WHERE kind='checkin.delete' GROUP BY state")
        states = {r['state']: r['n'] for r in rows}
        return {'待清理': sum(states.get(s, 0) for s in ('queued', 'running')),
                '已删除': states.get('deleted', 0), '已不存在': states.get('gone', 0),
                '权限或归属待处理': states.get('blocked', 0),
                '失败或超期未删': states.get('failed', 0) + states.get('expired', 0)}

    def retry_blocked(self, bot_id):
        self.db.execute("UPDATE play_jobs SET state='queued',due_at=?,last_error='' WHERE kind='checkin.delete' AND state IN ('blocked','failed') AND bot_id=? AND expires_at>?",
                        (time.time(), str(bot_id), time.time()))


class CheckinCleanupPlugin(Plugin):
    spec = Spec(id='checkin_cleanup', name='群签到自动清理', category='points', icon='🧹',
                description='群签到指令和反馈60秒后清理，私聊保留。',
                fields=[Field('delay_seconds', '群消息保留秒数', kind='int', default=60, min=60, max=60)])

    async def run(self, config):
        bot = self.ctx.telegram
        if bot is not None:
            bot._check_bot_identity()
            CleanupService(self.ctx.db).retry_blocked(bot._active_bot_id)
            await bot._drain_checkin_deletes()
        return CleanupService(self.ctx.db).summary()

    def status(self):
        report = CleanupService(self.ctx.db).summary()
        issue = self.ctx.db.one("SELECT last_error FROM play_jobs WHERE kind='checkin.delete' AND last_error<>'' AND state NOT IN ('deleted','gone') ORDER BY id DESC LIMIT 1")
        if issue: report['最近未删原因'] = issue['last_error'][:240]
        return report

    readonly_status = status


def payload(job):
    return json.loads(job['payload_json'])
