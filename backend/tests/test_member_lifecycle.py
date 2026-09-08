"""Member lifecycle: expiry, remote results, delete safety, pagination."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.adapters.mock import MockEmby
from app.core.db import Database
from app.core.errors import ConflictError
from app.main import app
from app.modules import member_ops
from app.modules.enforcement import MANAGED_KEYS, EnforcementService, desired_policy, fingerprint
from app.modules.groups import GroupService
from app.modules.members import MemberService

ADMIN = ("admin", "change-me")


class FailingEmby:
    def __init__(self, inner: Any, *, fail_ids: set[str] | None = None,
                 missing_ids: set[str] | None = None) -> None:
        self._inner = inner
        self.fail_ids = fail_ids or set()
        self.missing_ids = missing_ids or set()
        self.deleted: list[str] = []

    async def list_users(self) -> list[dict[str, Any]]:
        return await self._inner.list_users()

    async def delete_user(self, user_id: str) -> bool:
        if user_id in self.missing_ids:
            raise RuntimeError("404 Not Found")
        if user_id in self.fail_ids:
            raise RuntimeError("Emby 500 password=hunter2")
        ok = await self._inner.delete_user(user_id)
        if ok:
            self.deleted.append(user_id)
        return ok

    async def apply_policy(self, user_id: str, policy: dict[str, Any]) -> bool:
        return await self._inner.apply_policy(user_id, policy)


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def stack(tmp_path):
    db = Database(tmp_path / "t.db")
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    emby = MockEmby()
    enforcement = EnforcementService(db, members, emby)
    return {"db": db, "groups": groups, "members": members,
            "emby": emby, "enforcement": enforcement}


def _create(client: TestClient, user_id: str, name: str, **extra: Any) -> dict:
    groups = client.get("/api/groups", auth=ADMIN).json()
    gid = extra.pop("group_id", groups[0]["id"])
    r = client.put(f"/api/members/{user_id}", auth=ADMIN,
                   json={"username": name, "group_id": gid, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_switching_timed_groups_keeps_existing_expiry(client) -> None:
    created = _create(client, "keep-1", "keep1", group_id="standard")
    original = created["expires_at"]
    assert original
    far = int(time.time()) + 180 * 86400
    client.put("/api/members/keep-1", auth=ADMIN,
               json={"username": "keep1", "expires_at": far})
    client.post('/api/groups', auth=ADMIN, json={'id':'timed2','name':'Timed2',
                'billing_mode':'both','duration_days':7,'traffic_quota_bytes':1024})
    switched = client.put("/api/members/keep-1", auth=ADMIN,
                          json={"username": "keep1", "group_id": "timed2"}).json()
    assert switched["expires_at"] == far
    assert switched["expires_at"] != original


def test_new_member_still_arms_group_duration(client) -> None:
    created = _create(client, "new-1", "new1", group_id="standard")
    assert created["expires_at"]
    assert created["days_remaining"] in (29, 30)


def test_permanent_to_timed_keep_does_not_guess_days(client) -> None:
    _create(client, "perm-1", "perm1", group_id="vip")
    preview = client.get("/api/members/perm-1/group-preview?group_id=standard",
                         auth=ADMIN).json()
    assert preview["decision_required"] is True
    assert preview["default_policy"] == "keep"
    kept = client.post("/api/members/perm-1/group", auth=ADMIN,
                       json={"group_id": "standard", "expiry_policy": "keep"}).json()
    assert kept["expires_at"] is None
    applied = client.post("/api/members/perm-1/group", auth=ADMIN,
                          json={"group_id": "standard",
                                "expiry_policy": "apply_group"}).json()
    assert applied["expires_at"]


def test_renew_writes_override_not_just_base(client) -> None:
    _create(client, "ov-1", "ov1", group_id="standard")
    past = int(time.time()) - 10
    client.put("/api/members/ov-1/overrides", auth=ADMIN,
               json={"expires_at_override": past})
    before = client.get("/api/members/ov-1", auth=ADMIN).json()["member"]
    assert before["state"] == "expired"
    renewed = client.post("/api/members/ov-1/renew", auth=ADMIN,
                          json={"days": 30}).json()
    assert renewed["state"] != "expired"
    assert renewed["overrides"]["expires_at_override"] > int(time.time())
    assert renewed["expires_at"] in (None, before.get("expires_at"))


def test_renew_refuses_permanent_accounts(client) -> None:
    _create(client, "perm-2", "perm2", group_id="vip")
    r = client.post("/api/members/perm-2/renew", auth=ADMIN, json={"days": 30})
    assert r.status_code == 422
    assert "永久" in r.json()["detail"]


def test_delete_default_is_self_only(client) -> None:
    _create(client, "casc-a", "casca")
    _create(client, "casc-b", "cascb", inviter_id="casc-a", register_via="invite")
    preview = client.get("/api/members/casc-b/delete-preview", auth=ADMIN).json()
    assert preview["cascade"] == []
    assert [c["emby_user_id"] for c in preview["available_cascade"]] == ["casc-a"]
    deleted = client.request("DELETE", "/api/members/casc-b", auth=ADMIN).json()
    assert deleted["ok"] is True
    assert deleted["removed"] == ["casc-b"]
    assert client.get("/api/members/casc-a", auth=ADMIN).status_code == 200


def test_cascade_requires_exact_confirm_ids(client) -> None:
    _create(client, "c2-a", "c2a")
    _create(client, "c2-b", "c2b", inviter_id="c2-a", register_via="invite")
    preview = client.get("/api/members/c2-b/delete-preview?cascade=true",
                         auth=ADMIN).json()
    ids = [o["emby_user_id"] for o in preview["objects"]]
    r = client.request("DELETE", "/api/members/c2-b?cascade=true", auth=ADMIN)
    assert r.status_code == 409
    r = client.request(
        "DELETE", "/api/members/c2-b?cascade=true", auth=ADMIN,
        json={"cascade": True, "confirm_ids": ["c2-b"]})
    assert r.status_code == 409
    r = client.request(
        "DELETE", "/api/members/c2-b?cascade=true", auth=ADMIN,
        json={"cascade": True, "confirm_ids": ids + ["stranger"]})
    assert r.status_code == 409
    r = client.request(
        "DELETE", "/api/members/c2-b?cascade=true", auth=ADMIN,
        json={"cascade": True, "confirm_ids": ids})
    assert r.status_code == 200
    body = r.json()
    assert set(body["removed"]) == {"c2-a", "c2-b"}
    assert body["ok"] is True


def test_emby_failure_keeps_local_row_and_redacts_password(client) -> None:
    _create(client, "fail-1", "fail1")
    inner = app.state.emby
    inner._users["fail-1"] = {
        "Id": "fail-1", "Name": "fail1", "Policy": {"IsDisabled": False}}
    failing = FailingEmby(inner, fail_ids={"fail-1"})
    app.state.emby = failing
    try:
        body = client.request("DELETE", "/api/members/fail-1", auth=ADMIN).json()
        assert body["ok"] is False
        assert body["deleted"] is False
        assert body["retained"] == ["fail-1"]
        assert "hunter2" not in str(body)
        assert client.get("/api/members/fail-1", auth=ADMIN).status_code == 200
        member = client.get("/api/members/fail-1", auth=ADMIN).json()["member"]
        assert member["retryable"] is True
        assert "hunter2" not in (member.get("last_remote_error") or "")
    finally:
        app.state.emby = failing._inner


def test_emby_already_gone_is_idempotent(client) -> None:
    _create(client, "gone-1", "gone1")
    missing = FailingEmby(app.state.emby, missing_ids={"gone-1"})
    app.state.emby = missing
    try:
        body = client.request("DELETE", "/api/members/gone-1", auth=ADMIN).json()
        assert body["ok"] is True
        assert "gone-1" in body["emby_already_gone"]
        assert client.get("/api/members/gone-1", auth=ADMIN).status_code == 404
    finally:
        app.state.emby = missing._inner


def test_pagination_unmanaged_uses_all_ids(client) -> None:
    for i in range(3):
        _create(client, f"page-{i}", f"page{i}")
    listing = client.get("/api/members?page=1&page_size=1&sort=username",
                         auth=ADMIN).json()
    assert listing["page"] == 1
    assert listing["page_size"] == 1
    assert listing["total"] >= 3
    assert listing["counts"]["total"] == listing["total"]
    assert len(listing["members"]) == 1
    page_ids = {m["emby_user_id"] for m in listing["members"]}
    unmanaged_ids = {u["emby_user_id"] for u in listing["unmanaged"]}
    assert not (page_ids & unmanaged_ids)
    enrolled = {m["emby_user_id"]
                for m in client.get("/api/members?limit=5000", auth=ADMIN).json()["members"]}
    assert not (enrolled & unmanaged_ids)


def test_list_separates_entitlement_and_emby_missing(stack) -> None:
    members = stack["members"]
    members.upsert("u-miss", "ghost", {"group_id": "standard"})
    observed = member_ops.attach_observation(
        [members.get("u-miss")], {}, emby_error=None)[0]
    assert observed["entitlement_state"] == "active"
    assert observed["emby_status"] == "missing"
    assert observed["sync_status"] == "emby_missing"


def test_fingerprint_hit_still_detects_managed_key_drift(stack) -> None:
    members, emby, enforcement = stack["members"], stack["emby"], stack["enforcement"]
    members.upsert("u1", "demo-user-1", {"group_id": "standard"})
    asyncio.run(enforcement.reconcile(apply=True))
    policy = emby._users["u1"]["Policy"]
    policy["EnableContentDownloading"] = not policy.get("EnableContentDownloading")
    result = asyncio.run(enforcement.reconcile(apply=False))
    assert any(c["user_id"] == "u1" for c in result["changes"])
    want = desired_policy(members.get("u1"))
    assert fingerprint(want) == members.get("u1")["applied_fingerprint"]
    assert "EnableContentDownloading" in MANAGED_KEYS


def test_enforce_now_skips_admin_and_unenrolled(stack) -> None:
    members, enforcement = stack["members"], stack["enforcement"]
    skipped = asyncio.run(enforcement.enforce_now("nobody"))
    assert skipped["skipped"] == "unenrolled"
    assert skipped["ok"] is True
    members.upsert("admin", "demo-admin", {"group_id": "standard"})
    admin = asyncio.run(enforcement.enforce_now("admin"))
    assert admin["skipped"] == "administrator"
    assert admin["remote_ok"] is None


def test_enforce_now_failure_is_not_ok(stack) -> None:
    members, emby, enforcement = stack["members"], stack["emby"], stack["enforcement"]
    members.upsert("u1", "demo-user-1", {"group_id": "standard"})

    async def boom(*_a, **_k):
        raise RuntimeError("policy write failed password=secret")

    emby.apply_policy = boom  # type: ignore[method-assign]
    result = asyncio.run(enforcement.enforce_now("u1", "test"))
    assert result["ok"] is False
    assert result["retryable"] is True
    row = members.get("u1")
    assert row["last_remote_ok"] is False
    assert "secret" not in (row.get("last_remote_error") or "")


def test_confirm_ids_helper_rejects_mismatch() -> None:
    preview = {"objects": [{"emby_user_id": "a"}, {"emby_user_id": "b"}]}
    with pytest.raises(ConflictError):
        member_ops.validate_confirm_ids(preview, None, cascade=True)
    with pytest.raises(ConflictError):
        member_ops.validate_confirm_ids(preview, ["a"], cascade=True)
    member_ops.validate_confirm_ids(preview, ["b", "a"], cascade=True)
    member_ops.validate_confirm_ids(preview, None, cascade=False)


def test_redact_strips_password() -> None:
    assert "hunter2" not in member_ops.redact("failed password=hunter2")
    assert member_ops.is_absent_error("HTTP 404")


def test_action_envelope_ok_false_on_remote_fail() -> None:
    r = member_ops.action_result(local_ok=True, remote_ok=False, error="boom",
                                 retryable=True)
    assert r["ok"] is False
    assert r["local_ok"] is True
    assert r["retryable"] is True
