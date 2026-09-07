"""Reconciling member rows against the accounts Emby actually has.

A member row outlives its Emby account: it carries the traffic ledger, plan,
expiry and operator notes. So the rules under test are about what must *not*
happen -- no silent deletion, no verdict from an unreadable Emby, no purge of a
member whose account is present right now.
"""
from __future__ import annotations

import base64
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.db import Database
from app.main import app
from app.modules.groups import GroupService
from app.modules.members import MemberService

ADMIN = ("admin", "change-me")


def _basic() -> dict[str, str]:
    token = base64.b64encode(b"admin:change-me").decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def members():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        groups = GroupService(db)
        groups.seed_defaults()
        service = MemberService(db, groups)
        default_group = groups.default_group_id()
        assert default_group
        for user_id, name in (("u1", "alice"), ("u2", "bob"), ("u3", "carol")):
            service.upsert(user_id, name, {"group_id": default_group}, actor="test")
        yield service


def emby_users(*pairs):
    return [{"Id": uid, "Name": name} for uid, name in pairs]


# -- preview is read-only ----------------------------------------------------

def test_preview_reports_differences_without_touching_anything(members):
    result = members.sync_emby(emby_users(("u1", "alice"), ("u9", "dave")), apply=False)
    assert result["applied"] is False
    assert {m["emby_user_id"] for m in result["missing"]} == {"u2", "u3"}
    assert [u["emby_user_id"] for u in result["unmanaged"]] == ["u9"]
    # Nothing was written: a preview must not flag anybody.
    for row in members.list():
        assert not row.get("emby_missing_since")


# -- flagging ----------------------------------------------------------------

def test_apply_flags_missing_members_but_never_deletes_them(members):
    before = {m["emby_user_id"] for m in members.list()}
    result = members.sync_emby(emby_users(("u1", "alice")), apply=True)
    assert result["applied"] is True
    after = {m["emby_user_id"] for m in members.list()}
    # The ledger survives: flagged, not removed.
    assert after == before
    flagged = {m["emby_user_id"] for m in members.list() if m.get("emby_missing_since")}
    assert flagged == {"u2", "u3"}


def test_flag_timestamp_is_stable_across_repeated_syncs(members):
    members.sync_emby(emby_users(("u1", "alice")), apply=True)
    first = members.get("u2")["emby_missing_since"]
    assert first
    members.sync_emby(emby_users(("u1", "alice")), apply=True)
    # "Missing since" must record the first observation, not the latest sync.
    assert members.get("u2")["emby_missing_since"] == first


def test_returning_account_clears_the_flag(members):
    members.sync_emby(emby_users(("u1", "alice")), apply=True)
    assert members.get("u2")["emby_missing_since"]
    result = members.sync_emby(
        emby_users(("u1", "alice"), ("u2", "bob"), ("u3", "carol")), apply=True)
    assert set(result["returned"]) == {"u2", "u3"}
    assert members.get("u2")["emby_missing_since"] is None


def test_rename_in_emby_is_followed(members):
    members.sync_emby(emby_users(("u1", "alice-renamed"), ("u2", "bob"), ("u3", "carol")),
                      apply=True)
    assert members.get("u1")["username"] == "alice-renamed"


# -- failure handling --------------------------------------------------------

def test_empty_user_list_is_treated_as_unreadable_not_as_no_users(members):
    """One failed Emby poll must not orphan the entire member base."""
    result = members.sync_emby([], apply=True)
    assert result["skipped"] == "emby-user-list-empty"
    assert result["applied"] is False
    assert not any(m.get("emby_missing_since") for m in members.list())


def test_users_without_ids_are_ignored(members):
    result = members.sync_emby([{"Name": "ghost"}, {"Id": "u1", "Name": "alice"}],
                               apply=True)
    assert result["emby_users"] == 1
    assert {m["emby_user_id"] for m in result["missing"]} == {"u2", "u3"}


# -- enrolment ---------------------------------------------------------------

def test_new_accounts_are_only_enrolled_when_asked(members):
    users = emby_users(("u1", "alice"), ("u2", "bob"), ("u3", "carol"), ("u9", "dave"))
    result = members.sync_emby(users, apply=True)
    assert result["enrolled"] == 0
    assert members.get("u9") is None

    result = members.sync_emby(users, apply=True, enroll_new=True)
    assert result["enrolled"] == 1
    assert members.get("u9")["username"] == "dave"


# -- purge -------------------------------------------------------------------

def test_purge_only_removes_already_flagged_rows(members):
    members.sync_emby(emby_users(("u1", "alice")), apply=True)
    # u1 is present in Emby, so naming it must be a no-op even if asked.
    removed = members.purge_orphans(["u1", "u2"], actor="test")
    assert removed == 1
    assert members.get("u1") is not None
    assert members.get("u2") is None


def test_purge_refuses_unflagged_and_unknown_ids(members):
    assert members.purge_orphans(["u1", "does-not-exist"], actor="test") == 0
    assert members.get("u1") is not None


def test_purge_after_account_returns_is_refused(members):
    members.sync_emby(emby_users(("u1", "alice")), apply=True)
    members.sync_emby(emby_users(("u1", "alice"), ("u2", "bob"), ("u3", "carol")),
                      apply=True)
    # The operator's page may still list u2 as an orphan; the account is back,
    # so the purge must refuse rather than delete a live member's ledger.
    assert members.purge_orphans(["u2"], actor="test") == 0
    assert members.get("u2") is not None


# -- API ---------------------------------------------------------------------

@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def test_sync_endpoints_require_auth(client):
    assert client.get("/api/members/emby-sync").status_code == 401
    assert client.post("/api/members/emby-sync", json={}).status_code == 401
    assert client.post("/api/members/purge-orphans",
                       json={"emby_user_ids": ["x"]}).status_code == 401


def test_sync_preview_endpoint_is_read_only(client):
    preview = client.get("/api/members/emby-sync", auth=ADMIN)
    assert preview.status_code == 200
    assert preview.json()["applied"] is False


def test_purge_endpoint_requires_explicit_ids(client):
    assert client.post("/api/members/purge-orphans", json={}, auth=ADMIN).status_code == 422
    assert client.post("/api/members/purge-orphans", json={"emby_user_ids": []},
                       auth=ADMIN).status_code == 422


def test_sync_endpoint_applies_and_reports(client):
    users = client.get("/api/emby/users", auth=ADMIN).json()
    assert users, "mock emby should expose users"
    body = client.post("/api/members/emby-sync", json={"enroll_new": True},
                       auth=ADMIN).json()
    assert body["applied"] is True
    assert body["emby_users"] == len(users)
