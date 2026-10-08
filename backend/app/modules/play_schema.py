"""Additive records for existing-ledger escrow and durable Bot work."""


def migrate(db):
    db._conn.executescript("""
    CREATE TABLE IF NOT EXISTS play_escrows (
        scope TEXT NOT NULL, ref TEXT NOT NULL, user_id TEXT NOT NULL,
        amount INTEGER NOT NULL CHECK(amount>=0), reserved INTEGER NOT NULL CHECK(reserved>0),
        spent INTEGER NOT NULL DEFAULT 0 CHECK(spent>=0), released INTEGER NOT NULL DEFAULT 0 CHECK(released>=0),
        created_at INTEGER NOT NULL,
        PRIMARY KEY(scope,ref,user_id), CHECK(amount+spent+released=reserved)
    );
    CREATE TABLE IF NOT EXISTS play_funds (
        kind TEXT NOT NULL CHECK(kind IN ('blackwhite','poker','issuance','fee')),
        ref TEXT NOT NULL, amount INTEGER NOT NULL DEFAULT 0 CHECK(amount>=0),
        received INTEGER NOT NULL DEFAULT 0 CHECK(received>=0), paid INTEGER NOT NULL DEFAULT 0 CHECK(paid>=0),
        PRIMARY KEY(kind,ref), CHECK(amount+paid=received),
        CHECK(kind NOT IN ('issuance','fee') OR paid=0)
    );
    CREATE TABLE IF NOT EXISTS play_cash_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, ref TEXT NOT NULL,
        user_id TEXT NOT NULL, operation TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>0),
        target TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS play_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, job_key TEXT NOT NULL,
        kind TEXT NOT NULL, payload_json TEXT NOT NULL,
        created_at REAL NOT NULL, due_at REAL NOT NULL, expires_at REAL NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
        lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(bot_id,job_key)
    );
    CREATE INDEX IF NOT EXISTS play_jobs_due ON play_jobs(state,due_at,lease_until);
    """)
