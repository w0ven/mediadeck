"""Additive request-center migration; never emits notifications or touches quota."""


def migrate_requests(db):
    c = db._conn
    for name, ddl in (
        ("demand_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("demand_key", "TEXT NOT NULL DEFAULT ''"),
        ("revision", "INTEGER NOT NULL DEFAULT 1"),
        ("legacy_status", "TEXT NOT NULL DEFAULT ''"),
        ("create_key", "TEXT"),
        ("refund_at", "INTEGER"),
        ("original_title", "TEXT NOT NULL DEFAULT ''"),
        ("overview", "TEXT NOT NULL DEFAULT ''"),
    ):
        db._ensure_column("media_requests", name, ddl)
    c.execute("DROP INDEX IF EXISTS idx_mreq_open_title")
    c.execute(
        "UPDATE media_requests SET legacy_status=status, status='accepted', "
        "resolved_at=COALESCE(resolved_at,claimed_at,created_at) "
        "WHERE status IN ('claimed','done')"
    )
    # Legacy empty requirements mean a whole movie / unspecified series.
    import json

    from app.modules.requests import demand_key, normalize_demand

    for row in c.execute("SELECT id,media_type FROM media_requests WHERE demand_key=''").fetchall():
        demand = normalize_demand(row["media_type"], {}, legacy=True)
        c.execute(
            "UPDATE media_requests SET demand_json=?,demand_key=? WHERE id=?",
            (json.dumps(demand, ensure_ascii=False), demand_key(demand), row["id"]),
        )
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_mreq_open_demand "
        "ON media_requests(tmdb_id,media_type,demand_key) WHERE status='open'"
    )
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mreq_create_key ON media_requests(create_key)")
    # Executescript commits implicitly; individual statements keep migration atomic.
    statements = [
        """CREATE TABLE IF NOT EXISTS request_followers (
            request_id INTEGER NOT NULL, user_id TEXT NOT NULL, created_at INTEGER NOT NULL,
            PRIMARY KEY(request_id,user_id))""",
        """CREATE TABLE IF NOT EXISTS request_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL,
            actor TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
            internal INTEGER NOT NULL DEFAULT 0, event_key TEXT UNIQUE, created_at INTEGER NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS request_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL,
            user_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
            event_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, next_at INTEGER NOT NULL DEFAULT 0,
            lease_until INTEGER NOT NULL DEFAULT 0, message_id INTEGER, error TEXT NOT NULL DEFAULT '')""",
        """CREATE TABLE IF NOT EXISTS request_cards (
            token TEXT PRIMARY KEY, chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
            user_id TEXT NOT NULL, payload TEXT NOT NULL, actions TEXT NOT NULL,
            request_id INTEGER, revision INTEGER, photo INTEGER NOT NULL DEFAULT 0,
            expires_at INTEGER NOT NULL, UNIQUE(chat_id,message_id))""",
        """CREATE TABLE IF NOT EXISTS request_inputs (
            chat_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, token TEXT NOT NULL,
            kind TEXT NOT NULL, expires_at INTEGER NOT NULL)""",
    ]
    for sql in statements:
        c.execute(sql)
