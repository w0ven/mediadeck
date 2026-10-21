"""Bulk actions must survive a batch where only some ids are still valid.

Partial success is the normal case: a member can be deleted in another tab
while the operator is ticking boxes. Failing the whole batch for that would
make them redo work that already succeeded, so each id is attempted on its own
and the failures are named.

Deletion is deliberately absent from the allowed actions: a mis-click on a
checkbox column is easy, and bulk delete is the one action with no way back.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

ADMIN = ("admin", "change-me")


def _enrol(client: TestClient) -> list[str]:
    """Bring the mock Emby users under management and return their ids."""
    client.post("/api/members/enroll-defaults", auth=ADMIN, json={})
    listing = client.get("/api/members", auth=ADMIN).json()
    return [m["emby_user_id"] for m in listing.get("members", [])]


def test_bulk_renew_extends_every_named_member() -> None:
    with TestClient(app) as client:
        ids = _enrol(client)
        assert ids, "no members to act on"

        before = {m["emby_user_id"]: m.get("expires_at")
                  for m in client.get("/api/members", auth=ADMIN).json()["members"]}

        r = client.post("/api/members/bulk", auth=ADMIN,
                        json={"action": "renew", "user_ids": ids, "days": 30})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] == len(ids)
        assert body["failed"] == []

        after = {m["emby_user_id"]: m.get("expires_at")
                 for m in client.get("/api/members", auth=ADMIN).json()["members"]}
        for user_id in ids:
            if before[user_id] is not None:
                assert after[user_id] > before[user_id]


def test_unknown_ids_are_reported_without_losing_the_valid_ones() -> None:
    """One stale id must not cost the operator the rest of the batch."""
    with TestClient(app) as client:
        ids = _enrol(client)
        assert ids

        r = client.post("/api/members/bulk", auth=ADMIN,
                        json={"action": "renew",
                              "user_ids": [ids[0], "ghost-id"], "days": 7})
        body = r.json()
        assert body["ok"] == 1
        assert [f["user_id"] for f in body["failed"]] == ["ghost-id"]
        assert body["requested"] == 2


def test_bulk_suspend_then_activate_round_trips() -> None:
    with TestClient(app) as client:
        ids = _enrol(client)
        assert ids

        client.post("/api/members/bulk", auth=ADMIN,
                    json={"action": "suspend", "user_ids": ids})
        members = {m["emby_user_id"]: m for m in
                   client.get("/api/members", auth=ADMIN).json()["members"]}
        assert all(members[i]["status"] == "suspended" for i in ids)

        client.post("/api/members/bulk", auth=ADMIN,
                    json={"action": "activate", "user_ids": ids})
        members = {m["emby_user_id"]: m for m in
                   client.get("/api/members", auth=ADMIN).json()["members"]}
        assert all(members[i]["status"] == "active" for i in ids)


def test_delete_is_not_reachable_through_bulk() -> None:
    """The one irreversible action stays off the batch path."""
    with TestClient(app) as client:
        ids = _enrol(client)
        r = client.post("/api/members/bulk", auth=ADMIN,
                        json={"action": "delete", "user_ids": ids})
        assert r.status_code == 400
        # And the members are still there.
        assert client.get("/api/members", auth=ADMIN).json()["members"]


def test_empty_or_oversized_batches_are_refused() -> None:
    with TestClient(app) as client:
        assert client.post("/api/members/bulk", auth=ADMIN,
                           json={"action": "renew", "user_ids": []}).status_code == 400
        assert client.post("/api/members/bulk", auth=ADMIN,
                           json={"action": "renew",
                                 "user_ids": [f"x{i}" for i in range(501)]}
                           ).status_code == 400


def test_renew_days_are_bounded() -> None:
    """A typo of an extra zero must not hand out a decade of access."""
    with TestClient(app) as client:
        ids = _enrol(client)
        for bad in (0, -5, 99999):
            r = client.post("/api/members/bulk", auth=ADMIN,
                            json={"action": "renew", "user_ids": ids, "days": bad})
            assert r.status_code == 400, f"days={bad} should be rejected"


def test_bulk_requires_auth() -> None:
    with TestClient(app) as client:
        assert client.post("/api/members/bulk",
                           json={"action": "renew", "user_ids": ["x"]}).status_code == 401


def _audit_items(client: TestClient, limit: int = 200) -> list[dict]:
    return client.get(f"/api/audit?limit={limit}", auth=ADMIN).json()["items"]


def test_bulk_writes_one_summary_line_for_the_whole_batch() -> None:
    """A batch must be traceable as one operator action, not only as N rows."""
    with TestClient(app) as client:
        ids = _enrol(client)
        before = len(_audit_items(client))

        # reset-traffic used to be the example here; it is retired, so use a
        # live bulk action (docs/TRAFFIC_RETIREMENT.md).
        client.post("/api/members/bulk", auth=ADMIN,
                    json={"action": "renew", "user_ids": ids, "days": 7})

        entries = _audit_items(client)
        assert len(entries) > before
        summary = [e for e in entries
                   if e.get("action") == "member.bulk.renew"]
        assert len(summary) == 1, "exactly one summary line per batch"
        # The summary has to carry the outcome, otherwise it says nothing that
        # the per-member rows do not already say.
        assert f"ok={len(ids)}" in str(summary[0].get("detail", ""))


def test_bulk_reset_traffic_never_rewrites_archived_usage() -> None:
    """Retired traffic resets must not write to the archive through bulk either."""
    with TestClient(app) as client:
        ids = _enrol(client)
        db = app.state.db
        db.execute("UPDATE members SET traffic_used_bytes=4321")
        r = client.post("/api/members/bulk", auth=ADMIN,
                        json={"action": "reset-traffic", "user_ids": ids})
        assert r.json()["ok"] == 0
        rows = db.query("SELECT traffic_used_bytes FROM members")
        assert rows and all(row["traffic_used_bytes"] == 4321 for row in rows)
