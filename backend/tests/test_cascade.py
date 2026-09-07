"""Cascade delete: whoever vouched for an account answers for it.

Deleting a member also deletes the member who invited them. That is a
deliberate, owner-set rule -- an invite is a warranty, not a favour -- but it
is also the most dangerous button in the panel, so the tests here pin the two
properties that keep it safe:

- **It stops after one level.** Walking the chain would let one bad account
  take out an unbounded line of members above it. Nobody clicking delete on a
  single row is asking for that, and the damage cannot be undone.
- **The preview is the truth.** What the confirmation dialog promises and what
  the delete actually removes are computed from the same place, because an
  operator who is told "this removes 1 account" and loses 2 will never trust
  the dialog again.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.db import Database
from app.main import app
from app.modules.groups import GroupService
from app.modules.members import MemberService

ADMIN = ("admin", "change-me")


def _svc(tmp_path):
    db = Database(tmp_path / "cascade.db")
    groups = GroupService(db)
    groups.seed_defaults()
    return MemberService(db, groups), groups, db


def _chain(members, groups, names):
    """a <- b <- c: each invited by the one before it."""
    made, previous = [], ""
    for name in names:
        row = members.upsert(f"emby-{name}", name, {
            "group_id": groups.default_group_id(),
            "register_via": "invite" if previous else "legacy",
            "inviter_id": previous,
        }, actor="test")
        made.append(row)
        previous = row["emby_user_id"]
    return made


def _actions_for(members, subject):
    return [r["action"] for r in members.audit_log(50, subject=subject)]


# -- one level, and only one -------------------------------------------------

def test_deleting_a_member_takes_their_inviter(tmp_path) -> None:
    members, groups, _db = _svc(tmp_path)
    _chain(members, groups, ["alice", "bob"])

    result = members.delete("emby-bob", actor="operator", cascade=True)

    assert set(result["deleted"]) == {"emby-bob", "emby-alice"}
    assert members.get("emby-bob") is None
    assert members.get("emby-alice") is None


def test_the_cascade_stops_at_the_direct_inviter(tmp_path) -> None:
    """The grandparent survives: one click, one level, bounded blast radius."""
    members, groups, _db = _svc(tmp_path)
    _chain(members, groups, ["gran", "parent", "child"])

    result = members.delete("emby-child", actor="operator", cascade=True)

    assert set(result["deleted"]) == {"emby-child", "emby-parent"}
    assert members.get("emby-gran") is not None


def test_a_long_chain_loses_exactly_two_accounts(tmp_path) -> None:
    members, groups, _db = _svc(tmp_path)
    _chain(members, groups, ["a", "b", "c", "d", "e"])

    members.delete("emby-e", actor="operator", cascade=True)

    survivors = {m["emby_user_id"] for m in members.list()}
    assert survivors == {"emby-a", "emby-b", "emby-c"}


def test_deleting_the_inviter_does_not_touch_their_invitees(tmp_path) -> None:
    """Cascade runs upward only. Downward would be a mass deletion."""
    members, groups, _db = _svc(tmp_path)
    _chain(members, groups, ["owner", "guest"])

    members.delete("emby-owner", actor="operator", cascade=True)

    assert members.get("emby-guest") is not None
    # The orphan keeps its provenance: blanking it would erase who brought
    # them in, which is the one fact the tree exists to record.
    assert members.get("emby-guest")["inviter_id"] == "emby-owner"
    assert members.get("emby-guest")["inviter_name"] == "(已删除)"


# -- the cases where nothing cascades ---------------------------------------

def test_a_legacy_account_has_no_inviter_to_take(tmp_path) -> None:
    """The several hundred pre-bot accounts must delete alone."""
    members, groups, _db = _svc(tmp_path)
    members.upsert("emby-old", "oldtimer",
                   {"group_id": groups.default_group_id()}, actor="test")

    result = members.delete("emby-old", actor="operator")

    assert result["deleted"] == ["emby-old"]
    assert members.get("emby-old") is None


def test_cascade_false_deletes_only_the_named_account(tmp_path) -> None:
    members, groups, _db = _svc(tmp_path)
    _chain(members, groups, ["alice", "bob"])

    result = members.delete("emby-bob", actor="operator", cascade=False)

    assert result["deleted"] == ["emby-bob"]
    assert members.get("emby-alice") is not None


def test_an_inviter_who_is_already_gone_is_not_an_error(tmp_path) -> None:
    members, groups, _db = _svc(tmp_path)
    _chain(members, groups, ["alice", "bob"])
    members.delete("emby-alice", actor="operator", cascade=False)

    result = members.delete("emby-bob", actor="operator")
    assert result["deleted"] == ["emby-bob"]


def test_a_self_referential_inviter_cannot_delete_twice(tmp_path) -> None:
    members, groups, _db = _svc(tmp_path)
    members.upsert("emby-loop", "loop", {
        "group_id": groups.default_group_id(),
        "inviter_id": "emby-loop",
    }, actor="test")

    result = members.delete("emby-loop", actor="operator")
    assert result["deleted"] == ["emby-loop"]


def test_deleting_an_unknown_member_raises(tmp_path) -> None:
    members, _groups, _db = _svc(tmp_path)
    with pytest.raises(KeyError):
        members.delete("nobody", actor="operator")


# -- preview matches reality -------------------------------------------------

def test_preview_names_the_inviter_that_delete_removes(tmp_path) -> None:
    members, groups, _db = _svc(tmp_path)
    _chain(members, groups, ["alice", "bob"])

    preview = members.delete_preview("emby-bob", cascade=True)
    assert preview["target"]["username"] == "bob"
    assert [c["emby_user_id"] for c in preview["cascade"]] == ["emby-alice"]

    result = members.delete("emby-bob", actor="operator", cascade=True)
    promised = {preview["target"]["emby_user_id"]} | {
        c["emby_user_id"] for c in preview["cascade"]}
    assert promised == set(result["deleted"])


def test_preview_of_a_legacy_account_promises_nothing_extra(tmp_path) -> None:
    members, groups, _db = _svc(tmp_path)
    members.upsert("emby-old", "oldtimer",
                   {"group_id": groups.default_group_id()}, actor="test")

    preview = members.delete_preview("emby-old")
    assert preview["cascade"] == []
    assert set(members.delete("emby-old", actor="operator")["deleted"]) == {"emby-old"}


def test_preview_does_not_delete_anything(tmp_path) -> None:
    members, groups, _db = _svc(tmp_path)
    _chain(members, groups, ["alice", "bob"])

    members.delete_preview("emby-bob")
    members.delete_preview("emby-bob")

    assert members.get("emby-bob") is not None
    assert members.get("emby-alice") is not None


def test_preview_of_an_unknown_member_raises(tmp_path) -> None:
    members, _groups, _db = _svc(tmp_path)
    with pytest.raises(KeyError):
        members.delete_preview("nobody")


# -- the audit trail has to explain the second deletion ----------------------

def test_both_deletions_are_audited_under_distinct_actions(tmp_path) -> None:
    members, groups, _db = _svc(tmp_path)
    _chain(members, groups, ["alice", "bob"])

    members.delete("emby-bob", actor="operator", cascade=True)

    assert "member.delete" in _actions_for(members, "emby-bob")
    # A distinct action: the operator never asked for alice's row to go.
    assert "member.delete.cascade" in _actions_for(members, "emby-alice")
    assert "member.delete" not in _actions_for(members, "emby-alice")


def test_the_cascade_entry_names_who_it_came_from(tmp_path) -> None:
    members, groups, _db = _svc(tmp_path)
    _chain(members, groups, ["alice", "bob"])

    members.delete("emby-bob", actor="operator", cascade=True)

    entries = [r for r in members.audit_log(50, subject="emby-alice")
               if r["action"] == "member.delete.cascade"]
    assert entries and "bob" in entries[0]["detail"]


def test_a_solo_delete_writes_no_cascade_entry(tmp_path) -> None:
    members, groups, _db = _svc(tmp_path)
    _chain(members, groups, ["alice", "bob"])

    members.delete("emby-bob", actor="operator", cascade=False)

    assert _actions_for(members, "emby-alice") == ["member.create"]


# -- tree bookkeeping --------------------------------------------------------

def test_the_list_carries_invitee_counts_without_a_query_per_row(tmp_path) -> None:
    members, groups, _db = _svc(tmp_path)
    _chain(members, groups, ["owner", "g1"])
    members.upsert("emby-g2", "g2", {
        "group_id": groups.default_group_id(),
        "register_via": "invite", "inviter_id": "emby-owner",
    }, actor="test")

    rows = {m["emby_user_id"]: m for m in members.list()}
    assert rows["emby-owner"]["invitee_count"] == 2
    assert rows["emby-g1"]["invitee_count"] == 0
    assert rows["emby-g1"]["inviter_name"] == "owner"
    assert rows["emby-owner"]["inviter_name"] == ""


def test_members_can_be_filtered_by_channel_and_inviter(tmp_path) -> None:
    members, groups, _db = _svc(tmp_path)
    _chain(members, groups, ["owner", "guest"])

    invited = members.list(register_via="invite")
    assert [m["emby_user_id"] for m in invited] == ["emby-guest"]

    legacy = members.list(register_via="legacy")
    assert [m["emby_user_id"] for m in legacy] == ["emby-owner"]

    downstream = members.list(inviter_id="emby-owner")
    assert [m["emby_user_id"] for m in downstream] == ["emby-guest"]
    assert members.list(inviter_id="emby-nobody") == []


def test_provenance_survives_a_later_edit(tmp_path) -> None:
    """An update must not be able to relabel where a member came from."""
    members, groups, _db = _svc(tmp_path)
    _chain(members, groups, ["owner", "guest"])

    members.upsert("emby-guest", "guest", {"note": "renamed"}, actor="test")

    after = members.get("emby-guest")
    assert after["register_via"] == "invite"
    assert after["inviter_id"] == "emby-owner"


# -- API ---------------------------------------------------------------------

def test_delete_preview_endpoint_matches_the_delete(tmp_path) -> None:
    with TestClient(app) as client:
        groups = client.get("/api/groups", auth=ADMIN).json()
        gid = groups[0]["id"]
        client.put("/api/members/casc-a", auth=ADMIN,
                   json={"username": "casca", "group_id": gid})
        client.put("/api/members/casc-b", auth=ADMIN,
                   json={"username": "cascb", "group_id": gid,
                         "inviter_id": "casc-a", "register_via": "invite"})

        preview = client.get("/api/members/casc-b/delete-preview?cascade=true",
                             auth=ADMIN).json()
        assert [c["emby_user_id"] for c in preview["cascade"]] == ["casc-a"]
        ids = [o["emby_user_id"] for o in preview["objects"]]

        removed = client.request(
            "DELETE", "/api/members/casc-b?cascade=true", auth=ADMIN,
            json={"cascade": True, "confirm_ids": ids}).json()
        assert set(removed["removed"]) == {"casc-b", "casc-a"}
        assert client.get("/api/members/casc-a", auth=ADMIN).status_code == 404


def test_delete_can_be_asked_not_to_cascade(tmp_path) -> None:
    with TestClient(app) as client:
        groups = client.get("/api/groups", auth=ADMIN).json()
        gid = groups[0]["id"]
        client.put("/api/members/solo-a", auth=ADMIN,
                   json={"username": "soloa", "group_id": gid})
        client.put("/api/members/solo-b", auth=ADMIN,
                   json={"username": "solob", "group_id": gid,
                         "inviter_id": "solo-a", "register_via": "invite"})

        removed = client.request(
            "DELETE", "/api/members/solo-b?cascade=false", auth=ADMIN).json()
        assert removed["removed"] == ["solo-b"]
        assert client.get("/api/members/solo-a", auth=ADMIN).status_code == 200


def test_bulk_still_refuses_to_delete() -> None:
    """Cascade makes a bulk delete unbounded. It stays unavailable."""
    with TestClient(app) as client:
        groups = client.get("/api/groups", auth=ADMIN).json()
        client.put("/api/members/bulk-x", auth=ADMIN,
                   json={"username": "bulkx", "group_id": groups[0]["id"]})
        r = client.post("/api/members/bulk", auth=ADMIN,
                        json={"action": "delete", "user_ids": ["bulk-x"]})
        assert r.status_code >= 400
        assert client.get("/api/members/bulk-x", auth=ADMIN).status_code == 200


def test_delete_preview_endpoint_requires_auth() -> None:
    with TestClient(app) as client:
        assert client.get("/api/members/x/delete-preview").status_code == 401


def test_delete_preview_of_an_unknown_member_is_404() -> None:
    with TestClient(app) as client:
        assert client.get("/api/members/ghost/delete-preview",
                          auth=ADMIN).status_code == 404


# -- upgrading a database that predates all of this --------------------------

def _legacy_db(path) -> None:
    """The members/redeem_codes shape a v0.13 install actually has on disk."""
    import sqlite3
    import time as _t
    now = int(_t.time())
    conn = sqlite3.connect(str(path))
    conn.executescript("""
    CREATE TABLE members (
        emby_user_id TEXT PRIMARY KEY,
        username TEXT NOT NULL DEFAULT '',
        plan_id TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        expires_at INTEGER,
        traffic_used_bytes INTEGER NOT NULL DEFAULT 0,
        traffic_period_start INTEGER NOT NULL DEFAULT 0,
        note TEXT NOT NULL DEFAULT '',
        contact TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        last_seen_at INTEGER,
        applied_fingerprint TEXT NOT NULL DEFAULT '',
        applied_at INTEGER
    );
    CREATE TABLE redeem_codes (
        id TEXT PRIMARY KEY,
        batch_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        plan_id TEXT,
        extend_days INTEGER NOT NULL DEFAULT 0,
        add_traffic_bytes INTEGER NOT NULL DEFAULT 0,
        max_uses INTEGER NOT NULL DEFAULT 1,
        used_count INTEGER NOT NULL DEFAULT 0,
        expires_at INTEGER,
        created_at INTEGER NOT NULL,
        created_by TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT ''
    );
    """)
    conn.execute(
        "INSERT INTO members(emby_user_id,username,created_at,updated_at)"
        " VALUES('old-1','oldtimer',?,?)", (now, now))
    conn.execute(
        "INSERT INTO redeem_codes(id,batch_id,kind,extend_days,created_at)"
        " VALUES('OLDCARD','b1','extend',30,?)", (now,))
    conn.commit()
    conn.close()


def test_accounts_that_predate_the_bot_upgrade_as_legacy(tmp_path) -> None:
    """The several hundred existing accounts must survive and be labelled."""
    path = tmp_path / "old.db"
    _legacy_db(path)

    db = Database(path)
    row = db.one("SELECT * FROM members WHERE emby_user_id='old-1'")

    assert row["username"] == "oldtimer"
    assert row["register_via"] == "legacy"
    assert row["inviter_id"] == ""
    assert row["register_at"] is None
    assert row["invite_quota"] == 0


def test_the_old_redeem_table_is_archived_not_dropped(tmp_path) -> None:
    """CREATE TABLE IF NOT EXISTS would leave the wrong shape in place.

    Renaming keeps cards an operator may have sold; dropping them cannot be
    undone, and a silent no-op would break every query written against the
    new columns.
    """
    path = tmp_path / "old.db"
    _legacy_db(path)

    db = Database(path)
    assert len(db.query("SELECT * FROM redeem_codes_v13")) == 1
    assert db.query("SELECT * FROM redeem_codes") == []
    columns = {r["name"] for r in db.query("PRAGMA table_info(redeem_codes)")}
    assert {"code", "group_id", "days", "status", "batch"} <= columns


def test_reopening_an_upgraded_database_archives_nothing_further(tmp_path) -> None:
    path = tmp_path / "old.db"
    _legacy_db(path)
    Database(path).close()

    db = Database(path)
    names = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "redeem_codes_v13" in names
    assert "redeem_codes_v13_2" not in names
