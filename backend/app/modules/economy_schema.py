"""Versioned economy migration; financial history and existing rights stay intact."""

import json


def retire_legacy_catalogue(db):
    """Only delete the four retired products, never their orders or grants.

    Orders already own name/cost/kind/amount snapshots and have no catalogue
    foreign key. Backfill only absent historical JSON; never rewrite a buyer's
    existing snapshot from a subsequently edited item.
    """
    conn = db._conn
    if conn.execute("SELECT 1 FROM meta WHERE key='legacy_shop_retired'").fetchone():
        return
    kinds = ("traffic", "days", "bandwidth", "invite")
    for row in conn.execute(
        "SELECT * FROM shop_orders WHERE kind IN (?,?,?,?) AND spec_json IN ('{}','')", kinds
    ).fetchall():
        spec = {"kind": row["kind"], "name": row["item_name"], "cost": row["cost"],
                "amount": row["amount"], "historical": True}
        conn.execute("UPDATE shop_orders SET spec_json=? WHERE id=?",
                     (json.dumps(spec, ensure_ascii=False, sort_keys=True), row["id"]))
    conn.execute("DELETE FROM shop_items WHERE kind IN (?,?,?,?)", kinds)
    conn.execute("INSERT INTO meta(key,value) VALUES('legacy_shop_retired','1')")


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
    retire_legacy_catalogue(db)
    for table, column, ddl in (
        ('members', 'group_revision', 'INTEGER NOT NULL DEFAULT 0'),
        ('shop_items', 'purchase_notice', "TEXT NOT NULL DEFAULT ''"),
        ('shop_items', 'retention_days', 'INTEGER NOT NULL DEFAULT 7'),
        ('shop_items', 'revision', 'INTEGER NOT NULL DEFAULT 1'),
        ('inventory', 'notice_state', "TEXT NOT NULL DEFAULT 'pending'"),
    ):
        db._ensure_column(table, column, ddl)
    db._conn.executescript("""
    CREATE TABLE IF NOT EXISTS whitelist_card_grants (
        emby_user_id TEXT PRIMARY KEY, previous_group_id TEXT,
        group_revision INTEGER NOT NULL, enforce_account_expiry INTEGER NOT NULL DEFAULT 0,
        expires_at INTEGER, card_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'active', updated_at INTEGER NOT NULL,
        synced_at INTEGER, retry_at INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS red_packets (
        nonce TEXT PRIMARY KEY, chat_id TEXT NOT NULL, command_message_id INTEGER NOT NULL,
        actor_tg_id TEXT NOT NULL, actor_user_id TEXT NOT NULL, actor_name TEXT NOT NULL,
        funding TEXT NOT NULL CHECK(funding IN ('user','reward')),
        mode TEXT NOT NULL CHECK(mode IN ('random','equal')),
        audience TEXT NOT NULL CHECK(audience IN ('all','whitelist')),
        total INTEGER NOT NULL CHECK(total>0), parts INTEGER NOT NULL CHECK(parts>0),
        config_json TEXT NOT NULL, command_json TEXT NOT NULL, thread_id INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL, confirm_by INTEGER NOT NULL,
        card_message_id INTEGER, card_send_state TEXT NOT NULL DEFAULT 'pending', status TEXT NOT NULL DEFAULT 'draft',
        confirmed_at INTEGER, expires_at INTEGER, allocations_json TEXT NOT NULL DEFAULT '[]',
        remaining INTEGER NOT NULL DEFAULT 0 CHECK(remaining>=0), claimed_count INTEGER NOT NULL DEFAULT 0,
        refunded INTEGER NOT NULL DEFAULT 0, voided INTEGER NOT NULL DEFAULT 0,
        render_version INTEGER NOT NULL DEFAULT 0, rendered_version INTEGER NOT NULL DEFAULT 0,
        next_publish_at INTEGER NOT NULL DEFAULT 0, publish_error TEXT NOT NULL DEFAULT '',
        settlement_retry_at INTEGER NOT NULL DEFAULT 0, settlement_error TEXT NOT NULL DEFAULT '',
        UNIQUE(chat_id,command_message_id)
    );
    CREATE INDEX IF NOT EXISTS red_packets_due ON red_packets(status,expires_at);
    CREATE TABLE IF NOT EXISTS red_packet_claims (
        nonce TEXT NOT NULL REFERENCES red_packets(nonce), user_id TEXT NOT NULL, tg_user_id TEXT NOT NULL,
        amount INTEGER NOT NULL CHECK(amount>0), slot INTEGER NOT NULL, claimed_at INTEGER NOT NULL,
        PRIMARY KEY(nonce,user_id), UNIQUE(nonce,tg_user_id), UNIQUE(nonce,slot)
    );
    """)
    # Additive public presentation snapshots only; existing money/history stays.
    db._ensure_column('red_packets', 'public_actor_name', "TEXT NOT NULL DEFAULT ''")
    db._ensure_column('red_packets', 'result_page', 'INTEGER NOT NULL DEFAULT 0')
    db._ensure_column('red_packet_claims', 'display_name', "TEXT NOT NULL DEFAULT ''")
    db._ensure_column('red_packet_claims', 'tg_username', "TEXT NOT NULL DEFAULT ''")
    db._ensure_column('red_packets', 'permanent', 'INTEGER NOT NULL DEFAULT 0')
    db._ensure_column('red_packets', 'receipt_message_id', 'INTEGER')
    db._ensure_column('red_packets', 'receipt_page', 'INTEGER NOT NULL DEFAULT 0')
    db._ensure_column('red_packets', 'receipt_payload', "TEXT NOT NULL DEFAULT ''")
    for effect in ('pin','unpin','receipt'):
        for key, declaration in (('state', "TEXT NOT NULL DEFAULT ''"), ('attempts', 'INTEGER NOT NULL DEFAULT 0'), ('lease', 'REAL NOT NULL DEFAULT 0'), ('due', 'REAL NOT NULL DEFAULT 0'), ('error', "TEXT NOT NULL DEFAULT ''")):
            db._ensure_column('red_packets', effect+'_'+key, declaration)
    from app.modules.play_schema import migrate as migrate_play
    migrate_play(db)
    from app.modules.shop_notices import migrate as migrate_shop_notices
    migrate_shop_notices(db)
    from app.modules.scratch9 import migrate as migrate_scratch9
    migrate_scratch9(db)
