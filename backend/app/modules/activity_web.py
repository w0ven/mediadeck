"""Member-only web session via a private Bot one-time code, including expired accounts."""
import hashlib
import secrets
import time

from app.modules.economy_rules import economy_write

COOKIE = 'mediadeck_activity'


def digest(token):
    return hashlib.sha256(str(token).encode()).hexdigest()


def issue_code(db, uid, tg_uid):
    now = int(time.time())
    code = secrets.token_urlsafe(24)
    with economy_write(db) as conn:
        member = conn.execute('SELECT tg_user_id FROM members WHERE emby_user_id=?', (uid,)).fetchone()
        if not member or not tg_uid or str(member[0]) != str(tg_uid):
            raise ValueError('绑定已变化，请重新确认')
        conn.execute("DELETE FROM activity_web_tokens WHERE user_id=? AND kind='code'", (uid,))
        conn.execute('INSERT INTO activity_web_tokens VALUES(?,?,?,?,?,NULL)',
                     (digest(code), uid, str(tg_uid), 'code', now+300))
    return code


def exchange(db, code):
    now = int(time.time())
    with economy_write(db) as conn:
        row = conn.execute("SELECT t.* FROM activity_web_tokens t JOIN members m ON m.emby_user_id=t.user_id "
                           "AND CAST(m.tg_user_id AS TEXT)=t.tg_user_id WHERE digest=? AND kind='code' "
                           "AND t.expires_at>? AND t.used_at IS NULL", (digest(code), now)).fetchone()
        if not row:
            raise ValueError('登录码无效或过期，请在Bot私聊重新获取')
        conn.execute('UPDATE activity_web_tokens SET used_at=? WHERE digest=?', (now, row['digest']))
        token = secrets.token_urlsafe(32)
        conn.execute('INSERT INTO activity_web_tokens VALUES(?,?,?,?,?,NULL)',
                     (digest(token), row['user_id'], row['tg_user_id'], 'session', now+3600))
        return token


def resolve(db, token):
    if not token:
        return None
    row = db.one("SELECT t.user_id FROM activity_web_tokens t JOIN members m ON m.emby_user_id=t.user_id "
                 "AND CAST(m.tg_user_id AS TEXT)=t.tg_user_id WHERE digest=? AND kind='session' AND t.expires_at>?",
                 (digest(token), int(time.time())))
    return row['user_id'] if row else None
