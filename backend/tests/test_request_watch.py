"""Library-watch: reverse match, TV partial/complete, expiry, Emby skip."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.core.db import Database
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.plugins import PluginRegistry
from app.modules.plugins_builtin import (
    PluginContext,
    RequestLibraryWatchPlugin,
    ensure_request_library_watch,
    register_builtin,
)
from app.modules.request_schema import migrate_requests
from app.modules.request_watch import (
    FULL_SCAN_EVERY,
    WATCH_TTL,
    item_tmdb_id,
    library_stage,
    poll_library_watches,
    required_pairs,
)
from app.modules.requests import RequestService, normalize_demand


@pytest.fixture()
def stack(tmp_path):
    db = Database(tmp_path / "watch.db")
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    for uid, tg, roles in [
        ("u1", "900", []),
        ("u2", "901", []),
        ("up1", "801", ["uploader"]),
        ("admin1", "800", ["admin"]),
    ]:
        members.upsert(uid, uid, {"group_id": "standard"}, actor="test")
        members.bind_telegram(uid, tg, actor="test")
        members.set_roles(uid, roles, actor="test")
    service = RequestService(db, members, groups)
    yield db, members, service
    db.close()


def create(service, uid="u1", tmdb=550, kind="movie", **kw):
    return asyncio.run(service.create(uid, kind, tmdb, **kw))


def watch_row(db, rid):
    return db.one("SELECT * FROM request_library_watch WHERE request_id=?", (rid,))


def outbox_library(db, rid=None):
    sql = "SELECT * FROM request_outbox WHERE kind='library'"
    params = ()
    if rid is not None:
        sql += " AND request_id=?"
        params = (rid,)
    return db.query(sql, params)


class FakeLibrary:
    def __init__(self) -> None:
        self.latest: list[dict] = []
        self.by_tmdb: dict[int, list] = {}
        self.latest_calls: list[tuple] = []
        self.lookup_calls: list[tuple] = []
        self.fail_latest = False
        self.fail_lookup = False

    async def latest_items(self, limit: int = 12, *, watch: bool = False):
        self.latest_calls.append((limit, watch))
        if self.fail_latest:
            raise RuntimeError("emby down")
        return list(self.latest)[: max(1, limit)] if self.latest else []

    async def request_lookup(self, media_type, tmdb_id, user_id=None):
        self.lookup_calls.append((media_type, int(tmdb_id), user_id))
        if self.fail_lookup:
            raise RuntimeError("lookup failed")
        return list(self.by_tmdb.get(int(tmdb_id), []))


def _movie_item(tmdb_id=550):
    return [{"id": "m1", "name": "Demo", "episodes": []}]


def _tv_item(*pairs):
    return [
        {
            "id": "s1",
            "name": "Demo Show",
            "episodes": [{"season": s, "episode": e} for s, e in pairs],
        }
    ]


# -- pure matching ----------------------------------------------------------


def test_item_tmdb_id_reads_provider_ids_and_ignores_junk():
    assert item_tmdb_id({"tmdb_id": 12}) == 12
    assert item_tmdb_id({"ProviderIds": {"Tmdb": "99"}}) == 99
    assert item_tmdb_id({"provider_ids": {"tmdb": 7}}) == 7
    assert item_tmdb_id({"tmdb_id": "nope"}) is None
    assert item_tmdb_id({}) is None


def test_library_stage_movie_lands_once():
    demand = normalize_demand("movie", {"scope": "movie"})
    assert library_stage("movie", demand, []) is None
    assert library_stage("movie", demand, _movie_item()) == "complete"


def test_library_stage_tv_partial_then_complete_on_demanded_episodes():
    demand = normalize_demand(
        "tv", {"scope": "episodes", "seasons": [1], "episodes": [1, 2, 3]}
    )
    assert library_stage("tv", demand, []) is None
    assert library_stage("tv", demand, _tv_item((1, 1))) == "partial"
    assert library_stage("tv", demand, _tv_item((1, 1), (1, 2))) == "partial"
    assert library_stage("tv", demand, _tv_item((1, 1), (1, 2), (1, 3))) == "complete"
    # Unrelated season does not count.
    assert library_stage("tv", demand, _tv_item((2, 1))) is None


def test_library_stage_season_uses_catalog_for_complete():
    demand = normalize_demand("tv", {"scope": "season", "seasons": [1]})
    catalog = [{"season_number": 1, "episode_count": 2}]
    assert library_stage("tv", demand, _tv_item((1, 1)), catalog) == "partial"
    assert library_stage("tv", demand, _tv_item((1, 1), (1, 2)), catalog) == "complete"
    # Without a catalog, first content is partial and never complete.
    assert library_stage("tv", demand, _tv_item((1, 1), (1, 2))) == "partial"


def test_required_pairs_movie_is_none():
    assert required_pairs("movie", {"scope": "movie"}) is None


# -- lifecycle --------------------------------------------------------------


def test_accept_starts_watch_reject_does_not(stack):
    db, _, s = stack
    accepted = create(s, tmdb=550)
    s.finish(accepted["id"], "up1", "accepted")
    row = watch_row(db, accepted["id"])
    assert row["state"] == "pending"
    assert accepted["status"] == "open"
    assert s.get(accepted["id"])["status"] == "accepted"

    rejected = create(s, tmdb=551)
    s.finish(rejected["id"], "up1", "rejected")
    assert watch_row(db, rejected["id"]) is None
    cancelled = create(s, tmdb=552)
    s.finish(cancelled["id"], "u1", "cancelled")
    assert watch_row(db, cancelled["id"]) is None


def test_correction_stops_and_can_restart_watch(stack):
    db, _, s = stack
    row = create(s)
    s.finish(row["id"], "up1", "accepted")
    assert watch_row(db, row["id"])["state"] == "pending"
    s.correct(row["id"], "admin1", "open", "误点", 2)
    assert watch_row(db, row["id"])["state"] == "expired"
    assert s.get(row["id"])["status"] == "open"
    s.finish(row["id"], "up1", "accepted")
    assert watch_row(db, row["id"])["state"] == "pending"
    s.correct(row["id"], "admin1", "rejected", "不要了", 4)
    assert watch_row(db, row["id"])["state"] == "expired"


def test_migrate_requests_is_reentrant_and_backfills_accepted(stack):
    db, _, s = stack
    row = create(s)
    s.finish(row["id"], "up1", "accepted")
    migrate_requests(db)
    migrate_requests(db)
    watches = db.query("SELECT * FROM request_library_watch")
    assert len(watches) == 1
    assert watches[0]["request_id"] == row["id"]


# -- poller -----------------------------------------------------------------


def test_movie_arrival_notifies_owner_and_followers_once(stack):
    db, _, s = stack
    row = create(s, tmdb=550)
    s.follow(row["id"], "u2")
    s.finish(row["id"], "up1", "accepted")
    lib = FakeLibrary()
    lib.latest = [{"Id": "1", "tmdb_id": 550, "Type": "Movie"}]
    lib.by_tmdb[550] = _movie_item()
    summary = asyncio.run(poll_library_watches(s, lib, now=int(time.time())))
    assert summary["lookups"] == 1 and summary["notified"] == 1
    jobs = outbox_library(db, row["id"])
    assert {j["user_id"] for j in jobs} == {"u1", "u2"}
    assert all(j["kind"] == "library" for j in jobs)
    assert s.get(row["id"])["status"] == "accepted"
    assert watch_row(db, row["id"])["state"] == "done"
    # Replay must not enqueue again.
    lib.lookup_calls.clear()
    asyncio.run(poll_library_watches(s, lib, now=int(time.time()), full=True))
    assert lib.lookup_calls == []
    assert len(outbox_library(db, row["id"])) == 2


def test_tv_partial_then_complete_and_no_duplicate_stage(stack):
    db, _, s = stack
    demand = {"scope": "episodes", "seasons": [1], "episodes": [1, 2, 3]}
    row = create(s, tmdb=111, kind="tv", demand=demand)
    s.finish(row["id"], "up1", "accepted")
    lib = FakeLibrary()
    lib.latest = [{"ProviderIds": {"Tmdb": "111"}, "Type": "Series"}]
    lib.by_tmdb[111] = _tv_item((1, 1))
    asyncio.run(poll_library_watches(s, lib))
    assert watch_row(db, row["id"])["state"] == "partial"
    assert watch_row(db, row["id"])["notified_stage"] == "partial"
    keys = {j["event_key"] for j in outbox_library(db, row["id"])}
    assert keys == {"library:{}:partial:u1".format(row["id"])}

    lib.by_tmdb[111] = _tv_item((1, 1), (1, 2), (1, 3))
    asyncio.run(poll_library_watches(s, lib))
    assert watch_row(db, row["id"])["state"] == "done"
    stages = {j["event_key"] for j in outbox_library(db, row["id"])}
    assert "library:{}:complete:u1".format(row["id"]) in stages

    before = len(outbox_library(db, row["id"]))
    asyncio.run(poll_library_watches(s, lib, full=True))
    assert len(outbox_library(db, row["id"])) == before
    assert s.get(row["id"])["status"] == "accepted"


def test_reverse_match_only_looks_up_hits(stack):
    _, _, s = stack
    a = create(s, tmdb=1)
    b = create(s, tmdb=2)
    s.finish(a["id"], "up1", "accepted")
    s.finish(b["id"], "up1", "accepted")
    lib = FakeLibrary()
    lib.latest = [{"tmdb_id": 1}]
    lib.by_tmdb[1] = _movie_item()
    summary = asyncio.run(poll_library_watches(s, lib, latest_limit=100))
    assert lib.latest_calls == [(100, True)]
    assert summary["lookups"] == 1
    assert lib.lookup_calls == [("movie", 1, None)]
    assert watch_row(s._db, a["id"])["state"] == "done"
    assert watch_row(s._db, b["id"])["state"] == "pending"


def test_full_scan_looks_up_every_active_watch(stack):
    _, _, s = stack
    a = create(s, tmdb=1)
    b = create(s, tmdb=2)
    s.finish(a["id"], "up1", "accepted")
    s.finish(b["id"], "up1", "accepted")
    lib = FakeLibrary()
    lib.by_tmdb[2] = _movie_item()
    summary = asyncio.run(poll_library_watches(s, lib, full=True))
    assert lib.latest_calls == []
    assert summary["lookups"] == 2
    assert watch_row(s._db, b["id"])["state"] == "done"
    assert watch_row(s._db, a["id"])["state"] == "pending"


def test_ten_day_timeout_expires_without_notify(stack):
    db, _, s = stack
    row = create(s)
    s.finish(row["id"], "up1", "accepted")
    now = int(time.time())
    db.execute(
        "UPDATE request_library_watch SET created_at=? WHERE request_id=?",
        (now - WATCH_TTL - 1, row["id"]),
    )
    lib = FakeLibrary()
    lib.latest = [{"tmdb_id": 550}]
    lib.by_tmdb[550] = _movie_item()
    summary = asyncio.run(poll_library_watches(s, lib, now=now))
    assert summary["expired"] == 1
    assert watch_row(db, row["id"])["state"] == "expired"
    assert outbox_library(db, row["id"]) == []
    assert lib.lookup_calls == []
    assert s.get(row["id"])["status"] == "accepted"


def test_emby_unreachable_skips_round_without_touching_watches(stack):
    db, _, s = stack
    row = create(s)
    s.finish(row["id"], "up1", "accepted")
    before = dict(watch_row(db, row["id"]))
    lib = FakeLibrary()
    lib.fail_latest = True
    summary = asyncio.run(poll_library_watches(s, lib))
    assert summary["skipped"] is True
    assert summary["lookups"] == 0
    after = dict(watch_row(db, row["id"]))
    assert after["state"] == before["state"]
    assert after["last_checked_at"] == before["last_checked_at"]
    assert outbox_library(db) == []


def test_lookup_failure_skips_that_ticket(stack):
    db, _, s = stack
    row = create(s)
    s.finish(row["id"], "up1", "accepted")
    lib = FakeLibrary()
    lib.latest = [{"tmdb_id": 550}]
    lib.fail_lookup = True
    summary = asyncio.run(poll_library_watches(s, lib))
    assert summary["lookups"] == 1
    assert summary["notified"] == 0
    assert watch_row(db, row["id"])["state"] == "pending"
    assert outbox_library(db) == []


def test_unconfigured_library_skips(stack):
    _, _, s = stack
    row = create(s)
    s.finish(row["id"], "up1", "accepted")
    summary = asyncio.run(poll_library_watches(s, None))
    assert summary["skipped"] is True
    assert watch_row(s._db, row["id"])["state"] == "pending"


# -- plugin registration ----------------------------------------------------


class _Store:
    def __init__(self):
        self.data = {}

    def section(self, name):
        value = self.data.get(name, {})
        return value if isinstance(value, dict) else {}

    def set_section(self, name, value):
        self.data[name] = value


def test_plugin_is_registered_and_enabled_once():
    store = _Store()
    ensure_request_library_watch(store)
    ensure_request_library_watch(store)
    entry = store.section("plugins")["request_library_watch"]
    assert entry["enabled"] is True
    store.data["plugins"]["request_library_watch"]["enabled"] = False
    ensure_request_library_watch(store)
    assert store.section("plugins")["request_library_watch"]["enabled"] is False

    class _Db:
        def execute(self, *a, **k):
            return 1

        def query(self, *a, **k):
            return []

        def one(self, *a, **k):
            return None

    reg = register_builtin(PluginRegistry(store, _Db()), PluginContext())
    assert "request_library_watch" in reg.ids()
    plugin = reg.get("request_library_watch")
    assert isinstance(plugin, RequestLibraryWatchPlugin)
    assert plugin.spec.interval == 900
    assert plugin.spec.category == "request"


def test_plugin_run_skips_when_emby_is_down(stack):
    _, _, s = stack
    row = create(s)
    s.finish(row["id"], "up1", "accepted")
    lib = FakeLibrary()
    lib.fail_latest = True
    plugin = RequestLibraryWatchPlugin(PluginContext(requests=s, emby=lib))
    plugin.ctx.set_state(plugin.spec.id, {"last_full_scan": time.time()})
    result = asyncio.run(plugin.run({}))
    assert result["ok"] is True
    assert "跳过" in str(result.get("结果") or "")
    assert FULL_SCAN_EVERY == 6 * 3600
