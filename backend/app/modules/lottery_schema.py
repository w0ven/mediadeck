"""Additive lottery storage; existing member/economy tables are never rewritten."""
import json

from app.modules import lottery_captcha, lottery_device

SCHEMA = """
CREATE TABLE IF NOT EXISTS lotteries (
 id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'draft', config_json TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL, created_at INTEGER NOT NULL,
 published_at INTEGER, settled_at INTEGER, bot_username TEXT NOT NULL DEFAULT '',
 last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS lottery_entries (
 lottery_id TEXT NOT NULL, tg_id TEXT NOT NULL, label TEXT NOT NULL, joined_at INTEGER NOT NULL,
 registered INTEGER NOT NULL, excluded INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(lottery_id,tg_id)
);
CREATE TABLE IF NOT EXISTS lottery_identity_key (
 id INTEGER PRIMARY KEY CHECK(id=1), secret TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lottery_signals (
 lottery_id TEXT NOT NULL, tg_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('ip','device')),
 digest TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY(lottery_id,tg_id,kind)
);
CREATE INDEX IF NOT EXISTS idx_lottery_signal_match ON lottery_signals(lottery_id,kind,digest);
CREATE TABLE IF NOT EXISTS lottery_awards (
 id INTEGER PRIMARY KEY AUTOINCREMENT, lottery_id TEXT NOT NULL, tg_id TEXT NOT NULL,
 label TEXT NOT NULL, prize_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 user_id TEXT NOT NULL DEFAULT '', delivered_at INTEGER, receipt_json TEXT NOT NULL DEFAULT '{}',
 error TEXT NOT NULL DEFAULT '', next_at INTEGER NOT NULL DEFAULT 0, UNIQUE(lottery_id,tg_id)
);
CREATE TABLE IF NOT EXISTS lottery_admissions (
 code TEXT PRIMARY KEY, award_id INTEGER NOT NULL UNIQUE, tg_id TEXT NOT NULL,
 group_id TEXT NOT NULL, days INTEGER NOT NULL, created_at INTEGER NOT NULL,
 used_at INTEGER, used_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_lottery_admission_tg ON lottery_admissions(tg_id);
CREATE TABLE IF NOT EXISTS lottery_notices (
 id INTEGER PRIMARY KEY AUTOINCREMENT, lottery_id TEXT NOT NULL, kind TEXT NOT NULL,
 payload_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', message_id INTEGER,
 attempts INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL, next_at INTEGER NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '', UNIQUE(lottery_id,kind)
);
CREATE TABLE IF NOT EXISTS lottery_dms (
 id INTEGER PRIMARY KEY AUTOINCREMENT, lottery_id TEXT NOT NULL, award_id INTEGER NOT NULL UNIQUE,
 tg_id TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', message_id INTEGER,
 attempts INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL, next_at INTEGER NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_lottery_dms_lottery ON lottery_dms(lottery_id);
CREATE TABLE IF NOT EXISTS lottery_ops (
 lottery_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 chat_id TEXT NOT NULL DEFAULT '', message_id INTEGER, shown TEXT NOT NULL DEFAULT '',
 target TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,
 next_at INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', PRIMARY KEY(lottery_id,kind)
);
CREATE INDEX IF NOT EXISTS idx_lottery_ops_message ON lottery_ops(kind,chat_id,message_id);
"""


def migrate(db):
    db._conn.executescript(SCHEMA + lottery_captcha.SCHEMA)
    db._ensure_column('lottery_awards', 'next_at', 'INTEGER NOT NULL DEFAULT 0')
    db._ensure_column('lottery_entries', 'excluded', 'INTEGER NOT NULL DEFAULT 0')
    # Additive only: published rows treat missing new config fields as off/unlimited.
    db._ensure_column('lotteries', 'cancelled_at', 'INTEGER')
    lottery_device.migrate(db)  # consented device check tables; no backfill
    # Never retrofit new fairness rules onto already published historical draws.
    for row in db._conn.execute('SELECT id,state,config_json FROM lotteries').fetchall():
        config = json.loads(row['config_json'])
        changed = 'anti_multi' not in config
        if changed:
            config['anti_multi'] = row['state'] == 'draft'
        if row['state'] == 'draft':
            for key in ('reminder', 'pin'):
                if key not in config:
                    config[key] = True
                    changed = True
        if changed:
            db._conn.execute('UPDATE lotteries SET config_json=? WHERE id=?',
                             (json.dumps(config, ensure_ascii=False), row['id']))
