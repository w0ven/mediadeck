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
    CREATE TABLE IF NOT EXISTS play_rounds (
        nonce TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('blackwhite','poker')),
        bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, thread_id INTEGER NOT NULL DEFAULT 0,
        command_message_id INTEGER NOT NULL, actor_tg_id TEXT NOT NULL, actor_user_id TEXT NOT NULL,
        actor_name TEXT NOT NULL, stake INTEGER NOT NULL CHECK(stake>0), config_json TEXT NOT NULL,
        secret TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'lobby', created_at REAL NOT NULL,
        expires_at REAL NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
        revision INTEGER NOT NULL DEFAULT 0, rendered_revision INTEGER NOT NULL DEFAULT -1,
        card_message_id INTEGER, card_send_state TEXT NOT NULL DEFAULT 'pending',
        publish_token TEXT NOT NULL DEFAULT '', publish_lease REAL NOT NULL DEFAULT 0,
        next_publish_at REAL NOT NULL DEFAULT 0, publish_error TEXT NOT NULL DEFAULT '',
        UNIQUE(kind,bot_id,chat_id,command_message_id)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS play_rounds_one_active
        ON play_rounds(kind,bot_id,chat_id,thread_id) WHERE state IN ('lobby','running');
    CREATE INDEX IF NOT EXISTS play_rounds_due ON play_rounds(kind,state,expires_at);
    CREATE TABLE IF NOT EXISTS play_players (
        id INTEGER PRIMARY KEY AUTOINCREMENT, nonce TEXT NOT NULL REFERENCES play_rounds(nonce),
        user_id TEXT NOT NULL, tg_id TEXT NOT NULL, display_name TEXT NOT NULL,
        choice TEXT NOT NULL DEFAULT '' CHECK(choice IN ('','black','white')),
        joined_at REAL NOT NULL, result_amount INTEGER NOT NULL DEFAULT 0 CHECK(result_amount>=0),
        UNIQUE(nonce,user_id), UNIQUE(nonce,tg_id)
    );
    CREATE TABLE IF NOT EXISTS poker_state (
        nonce TEXT PRIMARY KEY REFERENCES play_rounds(nonce),
        turn INTEGER NOT NULL DEFAULT 0 CHECK(turn>=0), current_seat INTEGER NOT NULL DEFAULT 0,
        blind INTEGER NOT NULL CHECK(blind>0), start_error TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS poker_hands (
        player_id INTEGER PRIMARY KEY REFERENCES play_players(id), seat INTEGER NOT NULL DEFAULT 0,
        cards_json TEXT NOT NULL DEFAULT '[]', seen INTEGER NOT NULL DEFAULT 0 CHECK(seen IN (0,1)),
        folded INTEGER NOT NULL DEFAULT 0 CHECK(folded IN (0,1)), invested INTEGER NOT NULL DEFAULT 0 CHECK(invested>=0)
    );
    CREATE TABLE IF NOT EXISTS poker_actions (
        nonce TEXT NOT NULL, turn INTEGER NOT NULL, player_id INTEGER NOT NULL,
        action TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>=0), created_at REAL NOT NULL,
        PRIMARY KEY(nonce,turn)
    );
    CREATE TABLE IF NOT EXISTS play_photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, nonce TEXT NOT NULL,
        recipient TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('private','result')),
        user_id TEXT NOT NULL DEFAULT '', thread_id INTEGER NOT NULL DEFAULT 0,
        content_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0, due_at REAL NOT NULL DEFAULT 0,
        lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '', message_id INTEGER,
        created_at REAL NOT NULL, UNIQUE(bot_id,nonce,recipient,mode)
    );
    CREATE INDEX IF NOT EXISTS play_photos_due ON play_photos(bot_id,state,due_at,lease_until);
    """)
    from app.modules.market_schema import migrate as migrate_market
    migrate_market(db)
