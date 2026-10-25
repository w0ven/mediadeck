"""Server-observed private access, scoped by public Bot ID; never credentials.

A saved private menu ID proves an older successful private interaction. An
explicit newer block wins. Unknown historical users must send /start again.
"""
import json
import re
import time

from app.modules.economy_rules import economy_write, encode

REASON = '请先私聊启动 Bot，再返回活动报名；如已屏蔽，请先解除屏蔽并发送 /start'


def identity(bot):
    value = bot._token().split(':', 1)[0]
    return value if re.fullmatch(r'[1-9][0-9]*', value) else ''


def key(bot_id, tg_id):
    if not re.fullmatch(r'[1-9][0-9]*', str(bot_id)) or not re.fullmatch(r'[1-9][0-9]*', str(tg_id)):
        return ''
    return f'telegram.private:{bot_id}:{tg_id}'


def _value(source, name):
    sql, params = 'SELECT value FROM meta WHERE key=?', (name,)
    row = source.one(sql, params) if hasattr(source, 'one') else source.execute(sql, params).fetchone()
    return row['value'] if row else None


def marker(conn, bot_id, tg_id):
    return _value(conn, key(bot_id, tg_id))


def ready(conn, bot_id, tg_id):
    if not key(bot_id, tg_id):
        return False
    stored = marker(conn, bot_id, tg_id)
    if stored is not None:
        return json.loads(stored).get('state') == 'ready'
    # Exact private-session key only: group panels are namespaced with g:.
    message_id = _value(conn, f'telegram.panel:{bot_id}:{tg_id}')
    return bool(message_id and str(message_id).isdigit() and int(message_id) > 0)


def observe(db, bot_id, update):
    """Called for Bot API updates, never a Mini App's claimed client flag."""
    if db is None or not isinstance(update, dict):
        return
    event = update.get('my_chat_member') or update.get('message') or {}
    chat, sender = event.get('chat') or {}, event.get('from') or {}
    tg_id, date, update_id = chat.get('id'), event.get('date'), update.get('update_id')
    if (chat.get('type') != 'private' or type(tg_id) is not int or tg_id <= 0
            or type(sender.get('id')) is not int or sender['id'] != tg_id or sender.get('is_bot') is not False
            or event.get('sender_chat') or event.get('is_automatic_forward')
            or type(date) is not int or not 0 < date <= time.time() + 30
            or type(update_id) is not int or update_id < 0 or not key(bot_id, tg_id)):
        return
    state = 'ready'
    if 'my_chat_member' in update:
        member = event.get('new_chat_member') or {}
        user = member.get('user') or {}
        if str(user.get('id')) != bot_id or user.get('is_bot') is not True:
            return
        status = member.get('status')
        if status not in ('member', 'kicked', 'left'):
            return
        state = 'ready' if status == 'member' else 'blocked'
    elif type(event.get('message_id')) is not int or event['message_id'] <= 0:
        return
    value = {'state': state, 'date': date, 'update_id': update_id}
    with economy_write(db) as conn:
        old = marker(conn, bot_id, tg_id)
        if old is not None:
            previous = json.loads(old)
            if (date, update_id) <= (previous['date'], previous['update_id']):
                return
        conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (key(bot_id, tg_id), encode(value)))


def delivery_blocked(db, bot_id, tg_id, expected, now):
    """A definite unreachable DM revokes access, unless newer interaction arrived."""
    if not key(bot_id, tg_id):
        return
    with economy_write(db) as conn:
        if marker(conn, bot_id, tg_id) != expected:
            return
        previous = json.loads(expected) if expected else {}
        value = {'state': 'blocked', 'date': max(now, previous.get('date', 0)),
                 'update_id': previous.get('update_id', -1)}
        conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (key(bot_id, tg_id), encode(value)))
