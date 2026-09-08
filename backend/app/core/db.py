"""SQLite storage for operational data.

The settings store (a JSON document) is right for configuration: small, read
often, written rarely, and worth having as a human-readable file.  It is wrong
for usage data.  Traffic accounting appends thousands of rows a day and has to
answer aggregate questions ("how much did this user watch in August"), and a
JSON blob rewritten on every sample would both lose concurrent writes and grow
without bound.

So: configuration stays in JSON, usage and membership live here.

WAL is enabled because the panel reads (dashboard, stats) while a background
sampler writes; without it those readers would block the writer and the UI
would stall behind accounting.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
-- A group is the operator's billing/limit preset for a class of accounts.
-- Not a product: nothing is bought. billing_mode arms the meters (time /
-- traffic / both / none); limits are defaults every member inherits and may
-- override field-by-field.
CREATE TABLE IF NOT EXISTS groups (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    description          TEXT NOT NULL DEFAULT '',
    billing_mode         TEXT NOT NULL DEFAULT 'both',
    duration_days        INTEGER NOT NULL DEFAULT 30,
    traffic_quota_bytes  INTEGER NOT NULL DEFAULT 0,
    bandwidth_limit_kbps INTEGER NOT NULL DEFAULT 0,
    max_streams          INTEGER NOT NULL DEFAULT 2,
    max_devices          INTEGER NOT NULL DEFAULT 0,
    allow_download       INTEGER NOT NULL DEFAULT 0,
    allow_transcode      INTEGER NOT NULL DEFAULT 1,
    is_default           INTEGER NOT NULL DEFAULT 0,
    -- Media requests allowed per calendar month. 0 = unlimited.
    request_quota        INTEGER NOT NULL DEFAULT 3,
    created_at           INTEGER NOT NULL,
    updated_at           INTEGER NOT NULL
);

-- A plan is a template: the limits and billing rules a member inherits.
-- DEPRECATED (v0.14): superseded by groups; kept so existing rows survive a
-- downgrade and so old audit references stay resolvable.
CREATE TABLE IF NOT EXISTS plans (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    billing_type        TEXT NOT NULL DEFAULT 'unlimited',
    traffic_quota_bytes INTEGER NOT NULL DEFAULT 0,
    traffic_period      TEXT NOT NULL DEFAULT 'monthly',
    duration_days       INTEGER NOT NULL DEFAULT 0,
    max_streams         INTEGER NOT NULL DEFAULT 1,
    max_bitrate_kbps    INTEGER NOT NULL DEFAULT 0,
    max_devices         INTEGER NOT NULL DEFAULT 0,
    allow_transcode     INTEGER NOT NULL DEFAULT 0,
    allow_download      INTEGER NOT NULL DEFAULT 0,
    allow_sync          INTEGER NOT NULL DEFAULT 0,
    libraries_json      TEXT NOT NULL DEFAULT '[]',
    price_cents         INTEGER NOT NULL DEFAULT 0,
    currency            TEXT NOT NULL DEFAULT 'CNY',
    priority            INTEGER NOT NULL DEFAULT 0,
    is_default          INTEGER NOT NULL DEFAULT 0,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL
);

-- A member links an Emby user to a plan. Emby users with no row here are
-- deliberately invisible to enforcement: this server has hundreds of accounts
-- that predate the panel, and a bug must never be able to disable them.
CREATE TABLE IF NOT EXISTS members (
    emby_user_id        TEXT PRIMARY KEY,
    username            TEXT NOT NULL DEFAULT '',
    plan_id             TEXT,
    status              TEXT NOT NULL DEFAULT 'active',
    expires_at          INTEGER,
    traffic_used_bytes  INTEGER NOT NULL DEFAULT 0,
    traffic_period_start INTEGER NOT NULL DEFAULT 0,
    note                TEXT NOT NULL DEFAULT '',
    contact             TEXT NOT NULL DEFAULT '',
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    last_seen_at        INTEGER,
    -- Snapshot of what was last pushed into Emby, so the reconciler can tell
    -- "nothing changed" from "never applied" without re-writing every policy
    -- on every pass.
    applied_fingerprint TEXT NOT NULL DEFAULT '',
    applied_at          INTEGER,
    -- Per-member permission overlay. Missing keys inherit the group; '{}'
    -- means fully inherited. Validated in members.py, never trusted as raw
    -- input.
    overrides_json      TEXT NOT NULL DEFAULT '{}',
    -- v0.14: group replaces plan; roles are additive job functions
    -- (comma-separated: admin, uploader).
    group_id            TEXT,
    roles               TEXT NOT NULL DEFAULT ''
);
-- idx_members_group is created in _migrate() *after* _ensure_column: on an
-- upgraded database the members table predates group_id, and executescript
-- would fail here before the column migration ever ran.
CREATE INDEX IF NOT EXISTS idx_members_plan ON members(plan_id);
CREATE INDEX IF NOT EXISTS idx_members_status ON members(status);

-- One row per device per member. Emby has a device list but no notion of
-- "this account may use at most N devices", so the limit is enforced here.
CREATE TABLE IF NOT EXISTS devices (
    emby_user_id    TEXT NOT NULL,
    device_id       TEXT NOT NULL,
    device_name     TEXT NOT NULL DEFAULT '',
    client          TEXT NOT NULL DEFAULT '',
    app_version     TEXT NOT NULL DEFAULT '',
    last_ip         TEXT NOT NULL DEFAULT '',
    first_seen_at   INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    blocked         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (emby_user_id, device_id)
);
CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(emby_user_id);
CREATE INDEX IF NOT EXISTS idx_devices_seen ON devices(last_seen_at);

-- Account-sharing findings. Recorded for a person to judge, never acted on
-- automatically: the cost of being wrong is locking out a paying member over a
-- VPN reconnect, and that call is not the panel's to make.
CREATE TABLE IF NOT EXISTS sharing_findings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    emby_user_id  TEXT NOT NULL,
    username      TEXT NOT NULL DEFAULT '',
    networks      TEXT NOT NULL DEFAULT '',
    network_count INTEGER NOT NULL DEFAULT 0,
    detected_at   INTEGER NOT NULL,
    reviewed      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sharing_detected ON sharing_findings(detected_at);
CREATE INDEX IF NOT EXISTS idx_sharing_user ON sharing_findings(emby_user_id);

-- Access rules for the playback edge. Country-level rules are deliberately
-- absent: they need a GeoIP database this host does not carry, and a rule that
-- silently matches nothing is worse than no rule at all.
CREATE TABLE IF NOT EXISTS access_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    pattern     TEXT NOT NULL,
    action      TEXT NOT NULL DEFAULT 'deny',
    note        TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER NOT NULL
);

-- A refused request that leaves no trace is indistinguishable from a broken
-- node, and the operator ends up debugging the wrong thing.
CREATE TABLE IF NOT EXISTS access_blocks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL DEFAULT '',
    user_agent  TEXT NOT NULL DEFAULT '',
    remote_ip   TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    rule_id     INTEGER,
    item_id     TEXT NOT NULL DEFAULT '',
    blocked_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocks_time ON access_blocks(blocked_at);

-- Claim and rebind requests from the bot. Registration does not come through
-- here: the chat already proves who is asking, so a new account needs no
-- review. These two do -- both are attempts to take control of an account the
-- requester cannot otherwise prove they own.
CREATE TABLE IF NOT EXISTS tg_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL DEFAULT 'bind',
    tg_user_id      TEXT NOT NULL,
    tg_username     TEXT NOT NULL DEFAULT '',
    wanted_username TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    note            TEXT NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,
    reviewed_at     INTEGER,
    reviewed_by     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tgreq_status ON tg_requests(status, created_at);

-- Plugin run history. Append-heavy, so it lives here rather than in the
-- settings document, which is rewritten in full on every save.
CREATE TABLE IF NOT EXISTS plugin_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plugin_id   TEXT NOT NULL,
    ok          INTEGER NOT NULL DEFAULT 1,
    summary     TEXT NOT NULL DEFAULT '{}',
    started_at  INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    trigger     TEXT NOT NULL DEFAULT 'schedule'
);
CREATE INDEX IF NOT EXISTS idx_plugin_runs ON plugin_runs(plugin_id, started_at);

-- Media requests from members. One row per request; the uploader who claims
-- it is recorded so every other uploader sees it as taken rather than open.
CREATE TABLE IF NOT EXISTS media_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    emby_user_id  TEXT NOT NULL,
    username      TEXT NOT NULL DEFAULT '',
    tg_user_id    TEXT NOT NULL DEFAULT '',
    tmdb_id       INTEGER NOT NULL,
    media_type    TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    year          INTEGER,
    poster_path   TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'open',
    claimed_by    TEXT NOT NULL DEFAULT '',
    claimed_at    INTEGER,
    resolved_at   INTEGER,
    result_note   TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mreq_status ON media_requests(status, created_at);
CREATE INDEX IF NOT EXISTS idx_mreq_user ON media_requests(emby_user_id, created_at);

-- One row per uploader who was told about a request, so the fan-out can be
-- taken back. When somebody claims, every *other* uploader's message has to
-- lose its button: without the message ids there is no way to reach them, and
-- four people would each start hunting for the same title.
CREATE TABLE IF NOT EXISTS request_notices (
    request_id    INTEGER NOT NULL,
    tg_user_id    TEXT NOT NULL,
    message_id    INTEGER NOT NULL,
    created_at    INTEGER NOT NULL,
    PRIMARY KEY (request_id, tg_user_id)
);
CREATE INDEX IF NOT EXISTS idx_mreq_notice ON request_notices(request_id);
-- The same title requested twice while still open is one request, not two.
CREATE UNIQUE INDEX IF NOT EXISTS idx_mreq_open_title
    ON media_requests(tmdb_id, media_type) WHERE status IN ('open', 'claimed');

-- Rolled up per user per day. Sampling writes here continuously, so it is kept
-- narrow and indexed for the two questions actually asked: one user's history,
-- and one day across all users.
CREATE TABLE IF NOT EXISTS usage_daily (
    day             TEXT NOT NULL,
    emby_user_id    TEXT NOT NULL,
    bytes           INTEGER NOT NULL DEFAULT 0,
    seconds         INTEGER NOT NULL DEFAULT 0,
    plays           INTEGER NOT NULL DEFAULT 0,
    transcodes      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, emby_user_id)
);
CREATE INDEX IF NOT EXISTS idx_usage_day ON usage_daily(day);

-- Finished playback sessions: the source for "top titles", "what did this user
-- watch", and per-node traffic attribution.
CREATE TABLE IF NOT EXISTS play_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    emby_user_id    TEXT NOT NULL,
    username        TEXT NOT NULL DEFAULT '',
    item_id         TEXT NOT NULL DEFAULT '',
    item_name       TEXT NOT NULL DEFAULT '',
    item_type       TEXT NOT NULL DEFAULT '',
    series_name     TEXT NOT NULL DEFAULT '',
    device_id       TEXT NOT NULL DEFAULT '',
    client          TEXT NOT NULL DEFAULT '',
    play_method     TEXT NOT NULL DEFAULT '',
    node            TEXT NOT NULL DEFAULT '',
    remote_ip       TEXT NOT NULL DEFAULT '',
    bytes           INTEGER NOT NULL DEFAULT 0,
    seconds         INTEGER NOT NULL DEFAULT 0,
    started_at      INTEGER NOT NULL,
    ended_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_play_user ON play_events(emby_user_id);
CREATE INDEX IF NOT EXISTS idx_play_started ON play_events(started_at);
CREATE INDEX IF NOT EXISTS idx_play_item ON play_events(item_id);

-- Every enforcement action, so "why was this account disabled" always has an
-- answer. Billing disputes are unanswerable without this.
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    actor       TEXT NOT NULL DEFAULT 'system',
    action      TEXT NOT NULL,
    subject     TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '',
    ok          INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_subject ON audit_log(subject);

-- v0.19: registration is gated again, but by *who vouched for you* rather
-- than by a global switch. Three channels, three tables.

-- An invite is a member vouching for someone. owner_user_id is who spends
-- their quota; the new account records that id as its inviter, which is what
-- makes cascade delete possible later.
CREATE TABLE IF NOT EXISTS invite_codes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT NOT NULL UNIQUE,
    owner_user_id TEXT NOT NULL DEFAULT '',
    uses_left     INTEGER NOT NULL DEFAULT 1,
    expires_at    INTEGER,
    created_at    INTEGER NOT NULL,
    revoked       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_invite_owner ON invite_codes(owner_user_id);

-- A redeem code is sold or handed out by the operator: it carries its own
-- group and duration, so a card can be worth more than the default plan.
-- Single-use by construction (status flips to 'used'), because a card that
-- can be redeemed twice is a card that will be.
CREATE TABLE IF NOT EXISTS redeem_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    group_id    TEXT NOT NULL DEFAULT '',
    days        INTEGER NOT NULL DEFAULT 30,
    status      TEXT NOT NULL DEFAULT 'unused',
    used_by     TEXT NOT NULL DEFAULT '',
    used_at     INTEGER,
    batch       TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_redeem_status ON redeem_codes(status, batch);

-- Pre-authorisation: the operator names a Telegram id that may register with
-- no credential at all. Keyed by tg_user_id because that is the identity the
-- bot can actually verify, and UNIQUE so granting twice is not two slots.
CREATE TABLE IF NOT EXISTS admin_grants (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id  TEXT NOT NULL UNIQUE,
    granted_by  TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    used_at     INTEGER,
    gift_code   TEXT,
    gift_group_id TEXT,
    gift_days   INTEGER
);

CREATE TABLE IF NOT EXISTS redeem_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,
    user_id     TEXT NOT NULL DEFAULT '',
    ts          INTEGER NOT NULL,
    actor       TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_redeem_log_code ON redeem_log(code);
CREATE INDEX IF NOT EXISTS idx_redeem_log_user ON redeem_log(user_id);
CREATE INDEX IF NOT EXISTS idx_redeem_log_ts ON redeem_log(ts);

-- v0.20: points. The balance is deliberately absent as a column: it is the
-- SUM of these rows and nothing else, so a wrong balance is always a wrong
-- row that can be found and reversed. balance_after is a witness for audits,
-- never the answer to "how many points does this member have".
CREATE TABLE IF NOT EXISTS points_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    emby_user_id  TEXT NOT NULL,
    delta         INTEGER NOT NULL,
    balance_after INTEGER NOT NULL DEFAULT 0,
    reason        TEXT NOT NULL DEFAULT '',
    ref           TEXT NOT NULL DEFAULT '',
    actor         TEXT NOT NULL DEFAULT 'system',
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_points_user
    ON points_ledger(emby_user_id, created_at);

-- One row per member per day. The primary key is what makes a second
-- check-in on the same day impossible rather than merely discouraged: two
-- taps on a slow connection must not pay twice.
CREATE TABLE IF NOT EXISTS checkins (
    emby_user_id TEXT NOT NULL,
    day          TEXT NOT NULL,
    streak       INTEGER NOT NULL DEFAULT 1,
    points       INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (emby_user_id, day)
);
CREATE INDEX IF NOT EXISTS idx_checkins_day ON checkins(day);

-- What points can be spent on. Kept as data rather than code so the operator
-- can price and retire items without a release.
CREATE TABLE IF NOT EXISTS shop_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL,
    name           TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    cost           INTEGER NOT NULL DEFAULT 0,
    amount         INTEGER NOT NULL DEFAULT 0,
    enabled        INTEGER NOT NULL DEFAULT 1,
    per_user_limit INTEGER NOT NULL DEFAULT 0,
    sort           INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shop_items_sort ON shop_items(sort, id);

-- What was actually handed out. Kept even when the item is deleted, because
-- "why does this member have 500GB extra" must stay answerable.
CREATE TABLE IF NOT EXISTS shop_orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    emby_user_id TEXT NOT NULL,
    item_id      INTEGER NOT NULL,
    item_name    TEXT NOT NULL DEFAULT '',
    cost         INTEGER NOT NULL DEFAULT 0,
    kind         TEXT NOT NULL DEFAULT '',
    amount       INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shop_orders_user
    ON shop_orders(emby_user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_shop_orders_item ON shop_orders(item_id);

-- v0.23: bytes measured on the edge, per user per node per day.
--
-- Deliberately NOT merged into usage_daily. That table holds an estimate
-- derived from playback sampling (playing time x bitrate) and therefore only
-- sees what the media server observes -- which excludes signed direct links,
-- i.e. nearly all real traffic. This table holds bytes nginx actually put on
-- the wire. Keeping them apart is what makes it possible to say which figure
-- came from where; conflating them is how an estimate came to be trusted as
-- a measurement.
--
-- Keyed on (day, node, utag) rather than user id: the log carries the
-- anonymised tag, and a line whose tag is not yet linked to a member must
-- still be counted. emby_user_id is filled in when known and backfilled
-- later, so history becomes attributable retroactively.
CREATE TABLE IF NOT EXISTS edge_usage_daily (
    day          TEXT NOT NULL,
    node         TEXT NOT NULL,
    utag         TEXT NOT NULL,
    emby_user_id TEXT NOT NULL DEFAULT '',
    bytes        INTEGER NOT NULL DEFAULT 0,
    requests     INTEGER NOT NULL DEFAULT 0,
    seconds      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, node, utag)
);
CREATE INDEX IF NOT EXISTS idx_edge_usage_user
    ON edge_usage_daily(emby_user_id, day);
CREATE INDEX IF NOT EXISTS idx_edge_usage_day ON edge_usage_daily(day);

-- How far each node's log has been consumed. (inode, offset) rather than a
-- timestamp: re-reading a window would double-count permanently, and inode
-- is what distinguishes a rotated file from the same file grown longer.
CREATE TABLE IF NOT EXISTS edge_cursors (
    node       TEXT NOT NULL,
    path       TEXT NOT NULL,
    inode      INTEGER NOT NULL DEFAULT 0,
    offset     INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (node, path)
);

-- Measured kernel outbound IP bytes (nft per registered 4-tuple).
-- Separate from edge_usage_daily (completed HTTP logs) and from
-- members.traffic_used_bytes (bitrate estimate). Neither of those is
-- imported here as a billing baseline.
CREATE TABLE IF NOT EXISTS measured_usage_monthly (
    month         TEXT NOT NULL,
    node          TEXT NOT NULL,
    utag          TEXT NOT NULL,
    emby_user_id  TEXT NOT NULL DEFAULT '',
    bytes         INTEGER NOT NULL DEFAULT 0,
    unattributed  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (month, node, utag)
);
CREATE INDEX IF NOT EXISTS idx_measured_user_month
    ON measured_usage_monthly(emby_user_id, month);

CREATE TABLE IF NOT EXISTS measured_watermarks (
    node          TEXT NOT NULL,
    conn_id       TEXT NOT NULL,
    generation    INTEGER NOT NULL,
    utag          TEXT NOT NULL DEFAULT '',
    emby_user_id  TEXT NOT NULL DEFAULT '',
    counter_bytes INTEGER NOT NULL DEFAULT 0,
    observed_at   REAL NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (node, conn_id, generation)
);

-- One row per accepted envelope. Dedup is (node, boot_id, seq), not
-- "highest seq wins": an older unique sample must still credit.
CREATE TABLE IF NOT EXISTS measured_envelopes (
    node        TEXT NOT NULL,
    boot_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    nft_ok      INTEGER NOT NULL DEFAULT 0,
    observed_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (node, boot_id, seq)
);

CREATE TABLE IF NOT EXISTS measured_node_seq (
    node        TEXT NOT NULL,
    boot_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL DEFAULT 0,
    nft_ok      INTEGER NOT NULL DEFAULT 0,
    observed_at REAL NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL DEFAULT 0,
    acked       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (node, boot_id)
);

-- Quota credit / offset. Does not delete measured_usage_monthly history.
-- quota_used = max(0, SUM(bytes) - credit_bytes).
CREATE TABLE IF NOT EXISTS measured_credits (
    month         TEXT NOT NULL,
    emby_user_id  TEXT NOT NULL,
    credit_bytes  INTEGER NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (month, emby_user_id)
);

-- Authoritative deny snapshot. Nodes ack a revision; empty list is a
-- real snapshot (nobody blocked), not "keep last".
CREATE TABLE IF NOT EXISTS meter_policy (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    rev         INTEGER NOT NULL DEFAULT 0,
    blocked_json TEXT NOT NULL DEFAULT '[]',
    updated_at  REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meter_policy_ack (
    node        TEXT PRIMARY KEY,
    sent_rev    INTEGER NOT NULL DEFAULT 0,
    applied_rev INTEGER NOT NULL DEFAULT 0,
    sent_at     REAL NOT NULL DEFAULT 0,
    applied_at  REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # One connection guarded by a lock rather than a pool: writes are
        # low-volume and serialising them avoids SQLITE_BUSY entirely, which is
        # far easier to reason about than retry loops.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False,
                                     timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    @property
    def path(self) -> Path:
        return self._path

    def _migrate(self) -> None:
        with self._lock:
            # Must run before executescript: CREATE TABLE IF NOT EXISTS is a
            # no-op against a table that already exists under an incompatible
            # shape, so the v0.13 redeem_codes would survive and every query
            # written against the new columns would fail at runtime.
            self._retire_legacy_redeem_codes()
            self._conn.executescript(SCHEMA)
            cur = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'")
            row = cur.fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),),
                )
            self._ensure_column(
                "members", "overrides_json", "TEXT NOT NULL DEFAULT '{}'")
            # v0.14: groups replace plans; roles are additive job functions.
            # Upgraded databases created the members table long before these
            # columns existed, so they must be added here, not in SCHEMA.
            self._ensure_column("members", "group_id", "TEXT")
            self._ensure_column("members", "roles", "TEXT NOT NULL DEFAULT ''")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_members_group ON members(group_id)")
            # v0.18: Telegram linkage. Stored on the member rather than in a
            # side table because it is strictly one chat per member: the unique
            # index is what stops a second account from claiming a chat that
            # already answers for someone else.
            self._ensure_column("members", "tg_user_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("members", "tg_username", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("members", "tg_bound_at", "INTEGER")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_members_tg "
                "ON members(tg_user_id) WHERE tg_user_id <> ''")
            # v0.19: how each member got here, and who vouched for them.
            # legacy is the honest label for the several hundred accounts that
            # predate the bot -- inventing a channel for them would make the
            # registration-source report a lie.
            self._ensure_column("members", "inviter_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(
                "members", "register_via", "TEXT NOT NULL DEFAULT 'legacy'")
            self._ensure_column("members", "register_at", "INTEGER")
            self._ensure_column(
                "members", "invite_quota", "INTEGER NOT NULL DEFAULT 0")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_members_inviter "
                "ON members(inviter_id)")
            # v0.21: monthly media-request allowance. Counted on the member
            # rather than derived from media_requests because a request that
            # was rejected still spent the slot -- deriving it would refund
            # every refusal and make the cap unenforceable.
            self._ensure_column(
                "members", "request_used", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(
                "members", "request_period", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(
                "groups", "request_quota", "INTEGER NOT NULL DEFAULT 3")
            # v0.28: when the Emby account behind a member row disappeared.
            # A separate column rather than a status: the billing state machine
            # describes what the operator owes this member, while this records
            # a fact about Emby. Overloading status would let an orphan silently
            # clear itself the next time enforcement recomputed the state.
            self._ensure_column("members", "emby_missing_since", "INTEGER")
            self._ensure_column(
                "members", "last_remote_action", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("members", "last_remote_ok", "INTEGER")
            self._ensure_column(
                "members", "last_remote_error", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("members", "last_remote_at", "INTEGER")
            self._ensure_column("admin_grants", "gift_code", "TEXT")
            self._ensure_column("admin_grants", "gift_group_id", "TEXT")
            self._ensure_column("admin_grants", "gift_days", "INTEGER")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_gift_code "
                "ON admin_grants(gift_code) WHERE gift_code IS NOT NULL")
            self._reshape_measured_watermarks()
            self._reshape_meter_policy_ack()
            self._conn.commit()

    def _retire_legacy_redeem_codes(self) -> None:
        """Move a v0.13-shaped redeem_codes aside instead of dropping it.

        The old table keyed codes by a TEXT id and described them with
        batch_id/kind/plan_id/extend_days. Nothing reads it any more, but it
        may hold codes an operator sold, so it is renamed rather than deleted:
        a wrong migration that destroys data cannot be undone, one that keeps
        it can.
        """
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(redeem_codes)")}
        if not cols or "code" in cols:
            return
        for suffix in ("", *[f"_{n}" for n in range(2, 20)]):
            archive = f"redeem_codes_v13{suffix}"
            exists = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (archive,)).fetchone()
            if not exists:
                self._conn.execute(
                    f"ALTER TABLE redeem_codes RENAME TO {archive}")
                self._conn.commit()
                return

    def _reshape_measured_watermarks(self) -> None:
        """Drop boot_id from the watermark key if an older table still has it.

        Stream identity is (node, conn_id, generation). Keeping boot_id in
        the PK double-counted a live connection after every agent restart.
        """
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(measured_watermarks)")}
        if not cols or "boot_id" not in cols:
            self._ensure_column(
                "measured_watermarks", "observed_at", "REAL NOT NULL DEFAULT 0")
            return
        self._conn.execute(
            "ALTER TABLE measured_watermarks RENAME TO measured_watermarks_boot")
        self._conn.execute(
            "CREATE TABLE measured_watermarks ("
            "node TEXT NOT NULL, conn_id TEXT NOT NULL, generation INTEGER NOT NULL, "
            "utag TEXT NOT NULL DEFAULT '', emby_user_id TEXT NOT NULL DEFAULT '', "
            "counter_bytes INTEGER NOT NULL DEFAULT 0, "
            "observed_at REAL NOT NULL DEFAULT 0, "
            "updated_at REAL NOT NULL DEFAULT 0, "
            "PRIMARY KEY (node, conn_id, generation))")
        self._conn.execute(
            "INSERT INTO measured_watermarks"
            "(node,conn_id,generation,utag,emby_user_id,counter_bytes,observed_at,updated_at) "
            "SELECT node, conn_id, generation, utag, emby_user_id, "
            "MAX(counter_bytes), MAX(updated_at), MAX(updated_at) "
            "FROM measured_watermarks_boot "
            "GROUP BY node, conn_id, generation")
        self._conn.execute("DROP TABLE measured_watermarks_boot")

    def _reshape_meter_policy_ack(self) -> None:
        """sent_rev is what we shipped; applied_rev is what the node confirmed."""
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(meter_policy_ack)")}
        if not cols:
            return
        if "sent_rev" in cols and "applied_rev" in cols:
            return
        if "rev" in cols:
            self._conn.execute(
                "ALTER TABLE meter_policy_ack RENAME TO meter_policy_ack_old")
            self._conn.execute(
                "CREATE TABLE meter_policy_ack ("
                "node TEXT PRIMARY KEY, "
                "sent_rev INTEGER NOT NULL DEFAULT 0, "
                "applied_rev INTEGER NOT NULL DEFAULT 0, "
                "sent_at REAL NOT NULL DEFAULT 0, "
                "applied_at REAL NOT NULL DEFAULT 0)")
            self._conn.execute(
                "INSERT INTO meter_policy_ack(node,sent_rev,applied_rev,sent_at,applied_at) "
                "SELECT node, rev, 0, acked_at, 0 FROM meter_policy_ack_old")
            self._conn.execute("DROP TABLE meter_policy_ack_old")
            return
        self._ensure_column("meter_policy_ack", "sent_rev", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("meter_policy_ack", "applied_rev", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("meter_policy_ack", "sent_at", "REAL NOT NULL DEFAULT 0")
        self._ensure_column("meter_policy_ack", "applied_at", "REAL NOT NULL DEFAULT 0")

    def _ensure_column(self, table: str, name: str, ddl: str) -> None:
        """Idempotent ADD COLUMN for databases created before the column existed.

        CREATE TABLE IF NOT EXISTS never adds columns to an existing table, so
        a fresh install gets the column from SCHEMA while an upgraded install
        reaches here. PRAGMA table_info is the sqlite-portable check.
        """
        cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
        if name not in cols:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Transactional write; rolls back on error so a failed enforcement
        pass cannot leave half-applied state."""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self.write() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
