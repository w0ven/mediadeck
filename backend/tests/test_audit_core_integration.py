"""Verify that module-level fixes are actually connected to public APIs."""
import time

from fastapi.testclient import TestClient

from app.main import app
from app.modules.signing import user_tag

ADMIN = ("admin", "change-me")


def test_http_log_retry_is_idempotent_and_bad_cursor_never_credits():
    with TestClient(app) as client:
        client.post("/api/members/enroll-defaults", auth=ADMIN, json={})
        token = client.get("/api/nodes/mock-a/report-token", auth=ADMIN).json()["report_token"]
        headers = {"Authorization": f"Bearer {token}"}
        payload = {"path": "/var/log/audit", "inode": 123, "offset": 99,
                   "previous_offset": 0,
                   "lines": [f"{time.time()} a=1.1.1.1 p=1 u={user_tag('u1')} r=0 9000 1.0"]}
        first = client.post("/api/edge/mock-a/report", headers=headers, json=payload)
        assert first.status_code == 200
        assert first.json()["bytes"] == 9000
        second = client.post("/api/edge/mock-a/report", headers=headers, json=payload)
        assert second.status_code == 200
        assert second.json()["bytes"] == 0
        payload["offset"] = "bad"
        assert client.post("/api/edge/mock-a/report", headers=headers,
                           json=payload).status_code == 422
        assert app.state.ledger.totals_for_users()["u1"] == 9000
        assert client.get("/api/edge/mock-a/cursors", headers=headers).json()["cursors"][0]["offset"] == 99


def test_stats_http_obeys_live_measured_cutover():
    with TestClient(app) as client:
        app.state.groups.update("vip", {"traffic_quota_bytes": 1})
        app.state.members.upsert("u1", "demo-user-1", {"group_id": "vip"})
        app.state.db.execute("UPDATE members SET traffic_used_bytes=2 WHERE emby_user_id='u1'")
        assert client.get("/api/stats/overview", auth=ADMIN).json()["members"]["exhausted"] == 1
        response = client.post("/api/metering/cutover", auth=ADMIN,
                               json={"cutover": True, "baseline_confirmed": True})
        assert response.status_code == 200
        assert client.get("/api/stats/overview", auth=ADMIN).json()["members"]["exhausted"] == 0
        month = app.state.stats.measured_month()["period"]
        app.state.db.execute("INSERT INTO measured_usage_monthly VALUES(?,?,?,?,?,?)",
                             (month, "mock-a", user_tag("u1"), "u1", 2, 0))
        assert client.get("/api/stats/overview", auth=ADMIN).json()["members"]["exhausted"] == 1
