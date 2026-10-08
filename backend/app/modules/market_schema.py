"""Additive fixed stock supply, ownership and matched trades, separate from cash."""
from app.modules.market_data import COMPANIES


def migrate(db):
    db._conn.executescript("""
    CREATE TABLE IF NOT EXISTS market_companies (
        code TEXT PRIMARY KEY, name TEXT NOT NULL, sector TEXT NOT NULL,
        supply INTEGER NOT NULL CHECK(supply=1000), issue_price INTEGER NOT NULL CHECK(issue_price IN (10,15,20,25,30)),
        inventory INTEGER NOT NULL CHECK(inventory>=0 AND inventory<=supply), halted INTEGER NOT NULL DEFAULT 0 CHECK(halted IN (0,1))
    );
    CREATE TRIGGER IF NOT EXISTS market_fixed_offer BEFORE UPDATE OF supply,issue_price ON market_companies
        WHEN NEW.supply<>OLD.supply OR NEW.issue_price<>OLD.issue_price
        BEGIN SELECT RAISE(ABORT,'fixed offering cannot change'); END;
    CREATE TRIGGER IF NOT EXISTS market_no_reissue BEFORE DELETE ON market_companies
        BEGIN SELECT RAISE(ABORT,'fixed offering cannot be deleted and reseeded'); END;
    CREATE TABLE IF NOT EXISTS market_positions (
        user_id TEXT NOT NULL, code TEXT NOT NULL REFERENCES market_companies(code),
        shares INTEGER NOT NULL DEFAULT 0 CHECK(shares>=0), locked INTEGER NOT NULL DEFAULT 0 CHECK(locked>=0 AND locked<=shares),
        cost INTEGER NOT NULL DEFAULT 0 CHECK(cost>=0), realized INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(user_id,code), CHECK(shares>0 OR cost=0)
    );
    CREATE TABLE IF NOT EXISTS market_intents (
        nonce TEXT PRIMARY KEY, bot_id TEXT NOT NULL, user_id TEXT NOT NULL, tg_id TEXT NOT NULL,
        operation TEXT NOT NULL CHECK(operation IN ('ipo','buy','sell')), code TEXT NOT NULL REFERENCES market_companies(code),
        quantity INTEGER NOT NULL CHECK(quantity>0), price INTEGER NOT NULL CHECK(price>0), config_json TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'preview' CHECK(state IN ('preview','done','expired')),
        created_at REAL NOT NULL, expires_at REAL NOT NULL, result_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS market_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, nonce TEXT NOT NULL UNIQUE, user_id TEXT NOT NULL,
        code TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>0), gross INTEGER NOT NULL CHECK(gross>0),
        fee INTEGER NOT NULL CHECK(fee>=0), created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS market_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, nonce TEXT NOT NULL UNIQUE, user_id TEXT NOT NULL, tg_id TEXT NOT NULL,
        code TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('buy','sell')), price INTEGER NOT NULL CHECK(price>0),
        quantity INTEGER NOT NULL CHECK(quantity>0), remaining INTEGER NOT NULL CHECK(remaining>=0 AND remaining<=quantity),
        gross INTEGER NOT NULL DEFAULT 0 CHECK(gross>=0), fee INTEGER NOT NULL DEFAULT 0 CHECK(fee>=0),
        fee_bps INTEGER NOT NULL CHECK(fee_bps BETWEEN 0 AND 100), config_json TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','filled','cancelled','expired','invalid','closed','halted')),
        created_at REAL NOT NULL, expires_at REAL NOT NULL, CHECK(state<>'filled' OR remaining=0)
    );
    CREATE INDEX IF NOT EXISTS market_book ON market_orders(code,side,state,price,id);
    CREATE INDEX IF NOT EXISTS market_orders_owner ON market_orders(user_id,state);
    CREATE TABLE IF NOT EXISTS market_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>0),
        price INTEGER NOT NULL CHECK(price>0), buy_order INTEGER NOT NULL REFERENCES market_orders(id),
        sell_order INTEGER NOT NULL REFERENCES market_orders(id), buyer TEXT NOT NULL, seller TEXT NOT NULL,
        fee INTEGER NOT NULL CHECK(fee>=0), seller_cost INTEGER NOT NULL CHECK(seller_cost>=0), created_at REAL NOT NULL,
        CHECK(buyer<>seller), CHECK(buy_order<>sell_order)
    );
    CREATE INDEX IF NOT EXISTS market_prices ON market_trades(code,id);
    CREATE TABLE IF NOT EXISTS market_stock_moves (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, user_id TEXT NOT NULL, ref TEXT NOT NULL,
        shares_delta INTEGER NOT NULL DEFAULT 0, lock_delta INTEGER NOT NULL DEFAULT 0,
        cost_delta INTEGER NOT NULL DEFAULT 0, realized_delta INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
    );
    """)
    db._conn.executescript("""
    CREATE TABLE IF NOT EXISTS market_watch (
        user_id TEXT NOT NULL, code TEXT NOT NULL, PRIMARY KEY(user_id,code)
    );
    CREATE TABLE IF NOT EXISTS market_news (
        slot INTEGER PRIMARY KEY, code TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS market_panels (
        nonce TEXT PRIMARY KEY, bot_id TEXT NOT NULL, user_id TEXT NOT NULL, tg_id TEXT NOT NULL,
        chat_id TEXT NOT NULL, thread_id INTEGER NOT NULL DEFAULT 0, message_id INTEGER,
        payload_json TEXT NOT NULL, created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS market_inputs (
        bot_id TEXT NOT NULL, tg_id TEXT NOT NULL, user_id TEXT NOT NULL, panel TEXT NOT NULL,
        operation TEXT NOT NULL, code TEXT NOT NULL, phase TEXT NOT NULL, quantity INTEGER NOT NULL DEFAULT 0,
        expires_at REAL NOT NULL, PRIMARY KEY(bot_id,tg_id)
    );
    CREATE TABLE IF NOT EXISTS market_input_messages (
        bot_id TEXT NOT NULL, tg_id TEXT NOT NULL, message_id INTEGER NOT NULL,
        PRIMARY KEY(bot_id,tg_id,message_id)
    );
    """)
    db._ensure_column('market_intents','request_key',"TEXT NOT NULL DEFAULT ''")
    db._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS market_intent_source ON market_intents(bot_id,user_id,request_key) WHERE request_key<>''")
    # INSERT OR IGNORE does not refill inventory. Immutable supply/price trigger guards reseeding.
    for c in COMPANIES:
        db._conn.execute('INSERT OR IGNORE INTO market_companies(code,name,sector,supply,issue_price,inventory) VALUES(?,?,?,?,?,?)',
                         (c['code'], c['name'], c['sector'], c['supply'], c['issue_price'], c['supply']))
        row = db._conn.execute('SELECT supply,issue_price FROM market_companies WHERE code=?', (c['code'],)).fetchone()
        if row['supply'] != c['supply'] or row['issue_price'] != c['issue_price']:
            raise RuntimeError('fixed market offering differs from authoritative seed')
