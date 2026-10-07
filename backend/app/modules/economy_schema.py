"""Additive migration: legacy ledger, purchases and watch evidence stay intact."""


def migrate(db):
    for table, column, ddl in (
        ("checkins", "result_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("shop_items", "duration_days", "INTEGER NOT NULL DEFAULT 30"),
        ("shop_orders", "spec_json", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        db._ensure_column(table, column, ddl)
    db._conn.executescript("""
    CREATE TABLE IF NOT EXISTS economy_receipts (
        scope TEXT NOT NULL, request_id TEXT NOT NULL, user_id TEXT NOT NULL,
        request_json TEXT NOT NULL, result_json TEXT NOT NULL,
        PRIMARY KEY(scope,request_id,user_id)
    );
    CREATE TABLE IF NOT EXISTS group_points_intents (
        nonce TEXT PRIMARY KEY, chat_id TEXT NOT NULL,
        command_message_id INTEGER NOT NULL, reply_message_id INTEGER NOT NULL,
        actor_tg_id TEXT NOT NULL, actor_user_id TEXT NOT NULL,
        to_tg_id TEXT NOT NULL, to_user_id TEXT NOT NULL, to_name TEXT NOT NULL,
        amount INTEGER NOT NULL CHECK(amount>0), fee INTEGER NOT NULL,
        mode TEXT NOT NULL CHECK(mode IN ('transfer','mint')), thread_id INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
        confirmation_message_id INTEGER, status TEXT NOT NULL DEFAULT 'pending',
        result_json TEXT NOT NULL DEFAULT '{}', UNIQUE(chat_id,command_message_id)
    );
    CREATE TABLE IF NOT EXISTS inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT, emby_user_id TEXT NOT NULL,
        spec_json TEXT NOT NULL, source TEXT NOT NULL, created_at INTEGER NOT NULL,
        used_at INTEGER, expires_at INTEGER, result_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS inventory_owner ON inventory(emby_user_id,used_at);
    CREATE TABLE IF NOT EXISTS member_titles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, emby_user_id TEXT NOT NULL,
        tag TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER,
        revoked_at INTEGER, worn INTEGER NOT NULL DEFAULT 0, actor TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS title_tags (
        chat_id TEXT NOT NULL, tg_user_id TEXT NOT NULL, emby_user_id TEXT NOT NULL,
        title_id INTEGER, desired_tag TEXT NOT NULL DEFAULT '',
        applied_tag TEXT NOT NULL DEFAULT '', attempted_tag TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0, retry_at INTEGER NOT NULL DEFAULT 0,
        generation INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(chat_id,tg_user_id)
    );
    """)
    for column, ddl in (
        ("expiry_synced_at", "INTEGER"),
        ("expiry_retry_at", "INTEGER NOT NULL DEFAULT 0"),
        ("expiry_error", "TEXT NOT NULL DEFAULT ''"),
    ):
        db._ensure_column("inventory", column, ddl)
