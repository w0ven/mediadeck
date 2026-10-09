"""One public purchase receipt per successful order and authorized interaction group."""
from __future__ import annotations

import asyncio
import secrets
import time
from html import escape

from app.modules.economy_rules import economy_write
from app.modules.report_delivery import CALL_DELIVERY


def migrate(db):
    db._conn.execute('''CREATE TABLE IF NOT EXISTS shop_notices (
        order_id INTEGER NOT NULL REFERENCES shop_orders(id), bot_id TEXT NOT NULL,
        chat_id TEXT NOT NULL, display_name TEXT NOT NULL, item_name TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
        due_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
        lease_token TEXT NOT NULL DEFAULT '', message_id INTEGER,
        PRIMARY KEY(order_id,bot_id,chat_id))''')


def destination(config):
    from app.modules.settings import parse_group_interaction_chats
    if config.get('shop_purchase_broadcast') is not True or not config.get('enabled'):
        return '', []
    bot = str(config.get('bot_token') or '').split(':', 1)[0]
    if not bot.isdecimal() or int(bot) <= 0:
        return '', []
    try:
        return bot, parse_group_interaction_chats(config.get('group_interaction_chats'))
    except ValueError:
        return '', []


def record(conn, order_id, member, item, config):
    bot, chats = destination(config)
    name = str(member.get('tg_display_name') or '成员')[:40]
    for chat in chats:
        conn.execute('INSERT OR IGNORE INTO shop_notices(order_id,bot_id,chat_id,display_name,item_name) VALUES(?,?,?,?,?)',
                     (order_id, bot, chat, name, str(item['name'])[:80]))


def view(row):
    return '🛍 <b>好物入袋</b>\n\n'+escape(row['display_name'])+' 购入了 <b>'+escape(row['item_name'])+'</b>\n🌷 愿这份好物为你添一分欢喜。'


class ShopNotices:
    def __init__(self, db):
        self.db = db

    def claim(self, config, *, now=None):
        clock = time.time() if now is None else float(now)
        bot, chats = destination(config)
        identity = str(config.get('bot_token') or '').split(':', 1)[0]
        with economy_write(self.db) as conn:
            conn.execute("UPDATE shop_notices SET state='unknown' WHERE bot_id=? AND state='sending' AND lease_until<=?", (identity, clock))
            rows = conn.execute("SELECT * FROM shop_notices WHERE bot_id=? AND state IN ('queued','retry') ORDER BY order_id,chat_id", (identity,)).fetchall()
            for raw in rows:
                row = dict(raw)
                key = (row['order_id'], row['bot_id'], row['chat_id'])
                if not bot or row['chat_id'] not in chats or not conn.execute('SELECT 1 FROM shop_orders WHERE id=?', (row['order_id'],)).fetchone():
                    conn.execute("UPDATE shop_notices SET state='cancelled' WHERE order_id=? AND bot_id=? AND chat_id=?", key)
                    continue
                if row['due_at'] > clock:
                    continue
                token = secrets.token_hex(12)
                conn.execute("UPDATE shop_notices SET state='sending',attempts=attempts+1,lease_until=?,lease_token=? WHERE order_id=? AND bot_id=? AND chat_id=?",
                             (clock+90, token, *key))
                return dict(row, lease_token=token, attempts=row['attempts']+1)
        return None

    def finish(self, row, response, failure, *, now=None):
        clock = time.time() if now is None else float(now)
        mid = response.get('message_id') if isinstance(response, dict) else None
        state = ('sent' if type(mid) is int and mid > 0 else
                 'retry' if failure.get('state') == 'retry' and row['attempts'] < 3 else
                 'failed' if failure.get('state') in ('failed', 'retry') else 'unknown')
        self.db.execute("UPDATE shop_notices SET state=?,message_id=?,due_at=? WHERE order_id=? AND bot_id=? AND chat_id=? AND state='sending' AND lease_token=?",
                        (state, mid if state == 'sent' else None, clock+max(5, int(failure.get('retry_after') or 10)), row['order_id'], row['bot_id'], row['chat_id'], row['lease_token']))


class ShopNoticeBotMixin:
    async def _shop_notice_tick(self):
        if self._db is None:
            return
        service = ShopNotices(self._db)
        for _ in range(20):
            job = service.claim(self._cfg())
            if job is None:
                break
            # Recheck immediately before transport; no other notification destination.
            bot, chats = destination(self._cfg())
            if job['bot_id'] != bot or job['chat_id'] not in chats:
                service.finish(job, None, {'state': 'failed'})
                continue
            CALL_DELIVERY.set(None)
            try:
                result = await self._call('sendMessage', {'chat_id': job['chat_id'], 'text': view(job), 'parse_mode': 'HTML', 'disable_web_page_preview': True})
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - uncertain transport is not a retry instruction
                result = None
            service.finish(job, result, CALL_DELIVERY.get() or {})
