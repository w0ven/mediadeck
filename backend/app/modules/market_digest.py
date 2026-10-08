"""Future-only authorized group summaries. Unknown delivery is never blindly replayed."""
from __future__ import annotations

import re
import secrets
import time
from datetime import datetime, timedelta
from html import escape

from app.modules.economy_rules import BEIJING, economy_write, encode


def times(raw):
    parts = str(raw).replace('，', ',').split(',')
    result = []
    for part in parts:
        part = part.strip()
        if not re.fullmatch(r'[0-9]{2}:[0-9]{2}', part):
            raise ValueError('群资讯时段须为北京时间HH:MM，逗号分隔，最多两个')
        hour, minute = map(int, part.split(':'))
        if not 9 <= hour < 22 or not 0 <= minute < 60:
            raise ValueError('群资讯只能在北京时间09:00～21:59发送')
        result.append(hour*60+minute)
    if not 1 <= len(result) <= 2 or len(set(result)) != len(result):
        raise ValueError('群资讯每天须为1～2个不同的时段')
    return sorted(result)


def in_window(clock):
    return 9 <= datetime.fromtimestamp(clock, BEIJING).hour < 22


def migrate(db):
    db._conn.executescript("""
    CREATE TABLE IF NOT EXISTS market_digest_control (
        bot_id TEXT PRIMARY KEY, signature TEXT NOT NULL, observed_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS market_digest_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, chat_id TEXT NOT NULL,
        slot TEXT NOT NULL, day TEXT NOT NULL, scheduled_at REAL NOT NULL,
        due_at REAL NOT NULL, expires_at REAL NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL DEFAULT '', message_id INTEGER,
        thread_id INTEGER NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '',
        lease_until REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
        UNIQUE(chat_id,slot)
    );
    CREATE INDEX IF NOT EXISTS market_digest_due ON market_digest_jobs(bot_id,state,due_at);
    """)


def payload(db, clock):
    event = db.one('SELECT n.title,n.body,c.name FROM market_news n JOIN market_companies c ON c.code=n.code ORDER BY n.slot DESC LIMIT 1')
    body = '📈 <b>小群市场 · 模拟资讯</b>\n'
    if event:
        body += escape(event['name']+' · '+event['title'])+'\n'+escape(event['body'])
    else:
        body += '市场观察\n按真实挂单判断成交机会；资讯不改变价格。'
    rows = db.query("SELECT side,COUNT(*) n FROM market_orders WHERE state='open' AND remaining>0 AND expires_at>? GROUP BY side", (clock,))
    counts = {r['side']:r['n'] for r in rows}
    start = datetime.fromtimestamp(clock, BEIJING).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    trade = db.one('SELECT COUNT(*) n,COALESCE(SUM(quantity),0) shares FROM market_trades WHERE created_at>=? AND created_at<=?', (start, clock))
    body += f'\n\n待买 {counts.get("buy",0)} 单 · 待卖 {counts.get("sell",0)} 单'
    body += f'\n今日成交 {trade["n"]} 笔 · {trade["shares"]} 股' if trade['n'] else '\n今日暂无成交'
    body += '\n<i>卖出需真实买家，不保证成交。</i>'
    return {'text':body,'parse_mode':'HTML','reply_markup':{'inline_keyboard':[[
        {'text':label,'callback_data':'std:'+view} for label,view in (('看市场','home'),('看挂单','book'),('玩法帮助','help'))]]}}


class DigestService:
    def __init__(self, db, bot_id):
        self.db, self.bot_id = db, str(bot_id)

    def sync(self, groups, slots, enabled, *, startup=False, now=None):
        clock = time.time() if now is None else float(now)
        groups = sorted({str(g) for g in groups})
        signature = encode({'groups':groups,'slots':slots,'enabled':bool(enabled)})
        with economy_write(self.db) as conn:
            control = conn.execute('SELECT * FROM market_digest_control WHERE bot_id=?', (self.bot_id,)).fetchone()
            changed = not control or control['signature'] != signature
            gap = bool(control and clock-control['observed_at'] > 30)
            # A crash may have accepted an in-flight send. Never replay it.
            conn.execute("UPDATE market_digest_jobs SET state='unknown',last_error='发送确认中断，需后台核实；不自动重发',lease_token='',lease_until=0 WHERE bot_id=? AND state='sending' AND (lease_until<=? OR ?)", (self.bot_id,clock,int(startup)))
            if startup or gap or changed:
                conn.execute("UPDATE market_digest_jobs SET state='expired',last_error='错过排期，不补发历史' WHERE bot_id=? AND state IN ('pending','retry') AND scheduled_at<=?", (self.bot_id,clock))
            if changed or not enabled:
                conn.execute("UPDATE market_digest_jobs SET state='cancelled',last_error='配置或授权变更，未发送' WHERE bot_id=? AND state IN ('pending','retry')", (self.bot_id,))
            conn.execute('INSERT INTO market_digest_control(bot_id,signature,observed_at) VALUES(?,?,?) ON CONFLICT(bot_id) DO UPDATE SET signature=excluded.signature,observed_at=excluded.observed_at', (self.bot_id,signature,clock))
            if not enabled: return
            today = datetime.fromtimestamp(clock, BEIJING).replace(hour=0,minute=0,second=0,microsecond=0)
            for offset in (0,1):
                day = today+timedelta(days=offset)
                for minute in slots:
                    scheduled = (day+timedelta(minutes=minute)).timestamp()
                    if scheduled <= clock: continue  # only future slots, including on enable/restart
                    slot = day.strftime('%Y-%m-%d')+'T'+f'{minute//60:02}:{minute%60:02}'
                    for group in groups:
                        prior = conn.execute('SELECT * FROM market_digest_jobs WHERE chat_id=? AND slot=?', (group,slot)).fetchone()
                        active = conn.execute("SELECT COUNT(*) FROM market_digest_jobs WHERE chat_id=? AND day=? AND (attempts>0 OR state IN ('pending','retry','sending','sent','unknown'))", (group,day.strftime('%Y-%m-%d'))).fetchone()[0]
                        if prior:
                            if prior['state']=='cancelled' and prior['attempts']==0 and prior['bot_id']==self.bot_id and active<2:
                                conn.execute("UPDATE market_digest_jobs SET state='pending',last_error='',due_at=scheduled_at WHERE id=?", (prior['id'],))
                            continue
                        if active >= 2: continue
                        expires = min(scheduled+300,(day+timedelta(hours=22)).timestamp())
                        conn.execute('INSERT INTO market_digest_jobs(bot_id,chat_id,slot,day,scheduled_at,due_at,expires_at) VALUES(?,?,?,?,?,?,?)', (self.bot_id,group,slot,day.strftime('%Y-%m-%d'),scheduled,scheduled,expires))
            conn.execute("UPDATE market_digest_jobs SET state='expired',last_error='已过发送窗口，未补发' WHERE bot_id=? AND state IN ('pending','retry') AND expires_at<=?", (self.bot_id,clock))

    def claim(self, groups, *, now=None):
        clock = time.time() if now is None else float(now)
        if not in_window(clock): return None
        with economy_write(self.db) as conn:
            conn.execute("UPDATE market_digest_jobs SET state='unknown',last_error='发送租约中断，需后台核实；不自动重发',lease_token='',lease_until=0 WHERE bot_id=? AND state='sending' AND lease_until<=?", (self.bot_id,clock))
            job = conn.execute("SELECT * FROM market_digest_jobs WHERE bot_id=? AND state IN ('pending','retry') AND due_at<=? AND expires_at>? ORDER BY due_at,id LIMIT 1", (self.bot_id,clock,clock)).fetchone()
            if not job: return None
            job = dict(job)
            if job['chat_id'] not in set(map(str,groups)):
                conn.execute("UPDATE market_digest_jobs SET state='cancelled',last_error='原群已不再授权' WHERE id=?", (job['id'],));return None
            other = conn.execute('SELECT COUNT(*) FROM market_digest_jobs WHERE chat_id=? AND day=? AND attempts>0 AND id<>?', (job['chat_id'],job['day'],job['id'])).fetchone()[0]
            if other >= 2:
                conn.execute("UPDATE market_digest_jobs SET state='cancelled',last_error='本群已达每日两个时段上限' WHERE id=?", (job['id'],));return None
            token = secrets.token_hex(12)
            body = job['payload_json'] or encode(payload(self.db,clock))
            conn.execute("UPDATE market_digest_jobs SET state='sending',lease_token=?,lease_until=?,attempts=attempts+1,payload_json=? WHERE id=?", (token,clock+90,body,job['id']))
            return dict(job,lease_token=token,attempts=job['attempts']+1,payload_json=body)

    def finish(self, job, response, failure, *, now=None):
        clock = time.time() if now is None else float(now)
        mid = response.get('message_id') if isinstance(response,dict) else None
        valid = type(mid) is int and mid > 0
        state, error, due = 'unknown', '发送结果未知，需后台核实；不自动重发', clock
        if valid: state,error = 'sent',''
        elif failure.get('state') == 'failed': state,error = 'failed',str(failure.get('reason') or '发送权限或参数被拒绝')[:180]
        elif failure.get('state') == 'retry':
            due = clock+max(5,int(failure.get('retry_after') or 30))
            state = 'retry' if job['attempts']<3 and due<job['expires_at'] and in_window(due) else 'failed'
            error = str(failure.get('reason') or '发送失败')[:180] + ('，等待本时段有限重试' if state=='retry' else '，已停止本时段自动重试')
        thread = (response.get('message_thread_id') or 0) if isinstance(response,dict) and response.get('is_topic_message') else 0
        if type(thread) is not int or thread<0: thread=0
        self.db.execute('UPDATE market_digest_jobs SET state=?,message_id=?,thread_id=?,last_error=?,due_at=?,lease_token=?,lease_until=0 WHERE id=? AND state=? AND lease_token=?', (state,mid if valid else None,thread,error,due,'',job['id'],'sending',job['lease_token']))

    def summary(self):
        rows=self.db.query('SELECT state,COUNT(*) n FROM market_digest_jobs WHERE bot_id=? GROUP BY state', (self.bot_id,))
        states={r['state']:r['n'] for r in rows}
        report={'群资讯待排期':states.get('pending',0),'群资讯已送达':states.get('sent',0),'群资讯待重试':states.get('retry',0),'群资讯需核实':states.get('unknown',0),'群资讯失败':states.get('failed',0)}
        issue=self.db.one("SELECT last_error FROM market_digest_jobs WHERE bot_id=? AND state IN ('unknown','failed','retry') ORDER BY id DESC LIMIT 1", (self.bot_id,))
        if issue:report['最近群资讯异常']=issue['last_error']
        return report
