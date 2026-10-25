"""Additive game-center persistence; all live timers and money survive process loss."""


def migrate(db):
    db._conn.executescript("""
    CREATE TABLE IF NOT EXISTS gc_tables (
        id TEXT PRIMARY KEY, game TEXT NOT NULL, tier TEXT NOT NULL,
        currency TEXT NOT NULL, phase TEXT NOT NULL, revision INTEGER NOT NULL,
        state_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS gc_tables_live ON gc_tables(phase,game,tier);
    CREATE INDEX IF NOT EXISTS gc_escrows_held_user ON play_escrows(user_id,scope,amount) WHERE amount>0;
    CREATE TABLE IF NOT EXISTS gc_seats (
        user_id TEXT NOT NULL, currency TEXT NOT NULL, table_id TEXT NOT NULL,
        PRIMARY KEY(user_id,currency), FOREIGN KEY(table_id) REFERENCES gc_tables(id)
    );
    CREATE INDEX IF NOT EXISTS gc_seats_table ON gc_seats(table_id);
    CREATE TABLE IF NOT EXISTS gc_hands (
        table_id TEXT NOT NULL, hand_no INTEGER NOT NULL, state_json TEXT NOT NULL,
        created_at REAL NOT NULL, settled_at REAL,
        PRIMARY KEY(table_id,hand_no), FOREIGN KEY(table_id) REFERENCES gc_tables(id)
    );
    CREATE TABLE IF NOT EXISTS gc_requests (
        user_id TEXT NOT NULL, request_id TEXT NOT NULL, request_json TEXT NOT NULL,
        result_json TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(user_id,request_id)
    );
    CREATE TABLE IF NOT EXISTS gc_beans (
        user_id TEXT PRIMARY KEY, balance INTEGER NOT NULL CHECK(balance>=0), day TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS gc_outbox (
        table_id TEXT NOT NULL, chat_id TEXT NOT NULL, bot_id TEXT NOT NULL,
        payload_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
        rendered_revision INTEGER NOT NULL DEFAULT -1, desired TEXT NOT NULL DEFAULT 'live',
        state TEXT NOT NULL DEFAULT 'pending', message_id INTEGER,
        lease_token TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
        due_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(table_id,chat_id), FOREIGN KEY(table_id) REFERENCES gc_tables(id)
    );
    CREATE INDEX IF NOT EXISTS gc_outbox_due ON gc_outbox(state,due_at,lease_until);
    CREATE TABLE IF NOT EXISTS play_cash_moves (
        scope TEXT NOT NULL, ref TEXT NOT NULL, request_id TEXT NOT NULL,
        intent_json TEXT NOT NULL, PRIMARY KEY(scope,ref,request_id)
    );
    """)
