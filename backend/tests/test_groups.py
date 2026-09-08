"""Group seeding, and the request allowance that now lives on a group.

The starter groups are seeded once and never resurrected: an operator who
deleted one meant it. The whitelist group is the deliberate exception. It is
the target of the /prouser admin command, so it is *ensured* on every boot --
a command that names a specific group must not fail on a database whose owner
tidied their group list. Ensuring is not the same as overwriting: a whitelist
group they renamed or loosened is left exactly as they left it.

The request quota is tested here rather than with the request service because
the failure being prevented is a migration one: a form posted by a UI that
predates the field must not read as "unlimited".
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.db import Database
from app.core.errors import ConfigError
from app.main import app
from app.modules.groups import (
    DEFAULT_GROUPS,
    DEFAULT_REQUEST_QUOTA,
    WHITELIST_GROUP_ID,
    GroupService,
)
from app.modules.members import MemberService

ADMIN = ("admin", "change-me")


@pytest.fixture()
def groups(tmp_path) -> GroupService:
    service = GroupService(Database(tmp_path / "groups.db"))
    service.seed_defaults()
    return service


# -- seeding -----------------------------------------------------------------

def test_a_fresh_database_gets_the_starter_groups_and_the_whitelist(tmp_path) -> None:
    service = GroupService(Database(tmp_path / "fresh.db"))
    added = service.seed_defaults()

    ids = {g["id"] for g in service.list()}
    assert ids == {spec["id"] for spec in DEFAULT_GROUPS} | {WHITELIST_GROUP_ID}
    assert added == len(DEFAULT_GROUPS) + 1


def test_seeding_is_idempotent_across_restarts(groups) -> None:
    """Boot happens more than once; the second one must change nothing."""
    before = groups.list()
    assert groups.seed_defaults() == 0
    assert groups.list() == before


def test_a_deleted_starter_group_stays_deleted(groups) -> None:
    """The operator meant it. Re-seeding would silently undo their decision."""
    groups.delete("vip")
    groups.seed_defaults()
    assert groups.get("vip") is None


# -- the whitelist group -----------------------------------------------------

def test_the_whitelist_group_never_expires_and_is_loosely_limited(groups) -> None:
    whitelist = groups.get(WHITELIST_GROUP_ID)

    assert whitelist is not None
    assert whitelist["duration_days"] == 0
    assert whitelist["billing_mode"] == "none"
    assert whitelist["traffic_quota_bytes"] == 0
    assert whitelist["max_streams"] == 10
    assert whitelist["max_devices"] == 10
    assert whitelist["allow_download"] == 1
    assert whitelist["allow_transcode"] == 1
    assert whitelist["request_quota"] == 0
    assert whitelist["is_default"] == 0


def test_ensuring_the_whitelist_twice_creates_one_group(groups) -> None:
    assert groups.ensure_whitelist() == 0
    assert groups.ensure_whitelist() == 0
    assert len([g for g in groups.list() if g["id"] == WHITELIST_GROUP_ID]) == 1


def test_a_deleted_whitelist_group_is_recreated(groups) -> None:
    """/prouser names this group, so it has to be there."""
    # Simulate an older/corrupted database; supported UI/API deletion is now blocked.
    groups._db.execute('DELETE FROM groups WHERE id=?', (WHITELIST_GROUP_ID,))
    assert groups.get(WHITELIST_GROUP_ID) is None

    assert groups.ensure_whitelist() == 1
    assert groups.get(WHITELIST_GROUP_ID) is not None


def test_an_edited_whitelist_group_is_not_overwritten(groups) -> None:
    """Ensuring is not the same as resetting: their edits outrank the seed."""
    groups.update(WHITELIST_GROUP_ID, {"name": "内部人员", "max_streams": 3})

    groups.ensure_whitelist()
    groups.seed_defaults()

    after = groups.get(WHITELIST_GROUP_ID)
    assert after["name"] == "内部人员"
    assert after["max_streams"] == 3


def test_the_whitelist_is_added_to_a_database_that_predates_it(tmp_path) -> None:
    """An upgrade must not require the operator to build the group by hand."""
    db = Database(tmp_path / "upgraded.db")
    service = GroupService(db)
    service.seed_defaults()
    db.execute('DELETE FROM groups WHERE id=?', (WHITELIST_GROUP_ID,))

    # Simulates the next boot of the upgraded panel.
    assert GroupService(db).seed_defaults() == 1
    assert GroupService(db).get(WHITELIST_GROUP_ID) is not None


def test_a_member_can_be_moved_into_the_whitelist(tmp_path) -> None:
    db = Database(tmp_path / "move.db")
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    members.upsert("u1", "alice", {"group_id": "standard"}, actor="test")

    members.upsert("u1", "alice", {"group_id": WHITELIST_GROUP_ID},
                   actor="test")

    member = members.get("u1")
    assert member["group_id"] == WHITELIST_GROUP_ID
    # billing_mode 'none' means nothing expires this account.
    assert member["group"]["duration_days"] == 0


# -- request quota -----------------------------------------------------------

def test_a_new_group_defaults_to_the_standard_allowance(groups) -> None:
    created = groups.create({"id": "plain", "name": "无参数组",
                             "billing_mode": "none"})
    assert created["request_quota"] == DEFAULT_REQUEST_QUOTA


def test_the_quota_round_trips_through_create_and_update(groups) -> None:
    groups.create({"id": "generous", "name": "宽松组",
                   "billing_mode": "none", "request_quota": 25})
    assert groups.get("generous")["request_quota"] == 25

    groups.update("generous", {"request_quota": 1})
    assert groups.get("generous")["request_quota"] == 1


def test_zero_means_unlimited_and_is_storable(groups) -> None:
    groups.update("standard", {"request_quota": 0})
    assert groups.get("standard")["request_quota"] == 0


def test_an_update_that_omits_the_quota_keeps_it(groups) -> None:
    """A UI predating the field must not silently uncap requests."""
    groups.update("standard", {"request_quota": 9})
    groups.update("standard", {"name": "普通用户"})
    assert groups.get("standard")["request_quota"] == 9


def test_a_negative_quota_is_refused(groups) -> None:
    with pytest.raises(ConfigError):
        groups.create({"id": "bad", "name": "负数组",
                       "billing_mode": "none", "request_quota": -1})


def test_an_absurd_quota_is_refused(groups) -> None:
    with pytest.raises(ConfigError):
        groups.create({"id": "huge", "name": "超大组",
                       "billing_mode": "none", "request_quota": 10 ** 9})


def test_a_non_numeric_quota_is_refused(groups) -> None:
    with pytest.raises(ConfigError):
        groups.create({"id": "texty", "name": "文本组",
                       "billing_mode": "none", "request_quota": "很多"})


# -- API ---------------------------------------------------------------------

def test_the_api_exposes_the_quota_on_every_group() -> None:
    with TestClient(app) as client:
        rows = client.get("/api/groups", auth=ADMIN).json()
        assert rows
        assert all("request_quota" in g for g in rows)


def test_the_whitelist_group_is_present_after_boot() -> None:
    with TestClient(app) as client:
        rows = client.get("/api/groups", auth=ADMIN).json()
        whitelist = [g for g in rows if g["id"] == WHITELIST_GROUP_ID]
        assert len(whitelist) == 1
        assert whitelist[0]["duration_days"] == 0
