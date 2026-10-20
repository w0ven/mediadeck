"""Additive activity ledgers. Installation never activates rewards or rewrites history."""


def migrate(db):
    db._conn.executescript("""
    CREATE TABLE IF NOT EXISTS activity_rules (
        id INTEGER PRIMARY KEY CHECK(id=1),
        effective_at REAL NOT NULL, rule_version TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS activity_watch_pending (
        run_key TEXT NOT NULL, ended_at REAL NOT NULL,
        emby_user_id TEXT NOT NULL, started_at REAL NOT NULL,
        PRIMARY KEY(run_key,ended_at)
    );
    CREATE INDEX IF NOT EXISTS activity_pending_time ON activity_watch_pending(ended_at);
    CREATE TRIGGER IF NOT EXISTS activity_verified_outbox
    AFTER INSERT ON watch_verified_samples
    WHEN EXISTS(SELECT 1 FROM activity_rules WHERE id=1 AND NEW.ended_at>effective_at)
    BEGIN
        INSERT OR IGNORE INTO activity_watch_pending
            (run_key,ended_at,emby_user_id,started_at)
        SELECT NEW.run_key,NEW.ended_at,NEW.emby_user_id,MAX(NEW.started_at,effective_at)
        FROM activity_rules WHERE id=1;
    END;
    CREATE TABLE IF NOT EXISTS activity_watch_intervals (
        emby_user_id TEXT NOT NULL, day TEXT NOT NULL,
        started_at REAL NOT NULL, ended_at REAL NOT NULL CHECK(ended_at>started_at),
        PRIMARY KEY(emby_user_id,day,started_at)
    );
    CREATE TABLE IF NOT EXISTS activity_watch_days (
        emby_user_id TEXT NOT NULL, day TEXT NOT NULL,
        verified_seconds REAL NOT NULL DEFAULT 0,
        paid_steps INTEGER NOT NULL DEFAULT 0 CHECK(paid_steps BETWEEN 0 AND 12),
        points INTEGER NOT NULL DEFAULT 0 CHECK(points BETWEEN 0 AND 48),
        growth INTEGER NOT NULL DEFAULT 0 CHECK(growth BETWEEN 0 AND 60),
        rule_version TEXT NOT NULL, updated_at INTEGER NOT NULL,
        PRIMARY KEY(emby_user_id,day)
    );
    CREATE TABLE IF NOT EXISTS growth_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emby_user_id TEXT NOT NULL, delta INTEGER NOT NULL,
        reason TEXT NOT NULL, ref TEXT NOT NULL, created_at INTEGER NOT NULL,
        UNIQUE(emby_user_id,reason,ref)
    );
    CREATE INDEX IF NOT EXISTS growth_owner ON growth_ledger(emby_user_id,id);
    CREATE TABLE IF NOT EXISTS activity_config (
        id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, config_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS activity_members (
        emby_user_id TEXT PRIMARY KEY, special_level INTEGER NOT NULL DEFAULT 0,
        enrolled_at INTEGER NOT NULL, auto_renew INTEGER NOT NULL DEFAULT 0,
        lifecycle TEXT NOT NULL DEFAULT 'ready', deletion_token TEXT NOT NULL DEFAULT '',
        deletion_error TEXT NOT NULL DEFAULT '', deletion_updated_at INTEGER NOT NULL DEFAULT 0,
        deletion_lease INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS activity_web_tokens (
        digest TEXT PRIMARY KEY, user_id TEXT NOT NULL, tg_user_id TEXT NOT NULL,
        kind TEXT NOT NULL, expires_at INTEGER NOT NULL, used_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS activity_effect_sync (
        user_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL DEFAULT '', bandwidth INTEGER,
        retry_at INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS activity_notices (
        user_id TEXT NOT NULL, expiry INTEGER NOT NULL, stage TEXT NOT NULL,
        sent_at INTEGER, retry_at INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(user_id,expiry,stage)
    );
    CREATE TABLE IF NOT EXISTS activity_migrations (
        request_id TEXT PRIMARY KEY, preview_hash TEXT NOT NULL, result_json TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );
    CREATE TRIGGER IF NOT EXISTS activity_delete_exclusion
    BEFORE UPDATE OF expires_at,overrides_json,group_id,status ON members
    WHEN EXISTS(SELECT 1 FROM activity_members WHERE emby_user_id=OLD.emby_user_id AND lifecycle='deleting')
    AND (NEW.expires_at IS NOT OLD.expires_at OR NEW.overrides_json IS NOT OLD.overrides_json
         OR NEW.group_id IS NOT OLD.group_id OR NEW.status IS NOT OLD.status)
    BEGIN SELECT RAISE(ABORT,'账号删除结果待确认，暂不能变更期限或资格'); END;
    CREATE TRIGGER IF NOT EXISTS activity_delete_spending
    BEFORE INSERT ON points_ledger
    WHEN NEW.delta<0 AND EXISTS(SELECT 1 FROM activity_members
        WHERE emby_user_id=NEW.emby_user_id AND lifecycle='deleting')
    BEGIN SELECT RAISE(ABORT,'账号删除处理中，不能新增消费或冻结'); END;
    CREATE TRIGGER IF NOT EXISTS activity_delete_inventory
    BEFORE UPDATE OF used_at ON inventory
    WHEN NEW.used_at IS NOT OLD.used_at AND EXISTS(SELECT 1 FROM activity_members
        WHERE emby_user_id=NEW.emby_user_id AND lifecycle='deleting')
    BEGIN SELECT RAISE(ABORT,'账号删除处理中，不能使用道具'); END;
    """)
    db._ensure_column('activity_members', 'deletion_lease', 'INTEGER NOT NULL DEFAULT 0')
