"""Cross-module API regressions found in the full source audit."""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

ADMIN = ("admin", "change-me")


def _role_admin(client):
    client.post("/api/members/enroll-defaults", auth=ADMIN, json={}).raise_for_status()
    client.post("/api/members/u1/roles", auth=ADMIN,
                json={"roles": ["admin"]}).raise_for_status()
    client.post("/api/members/u1/password", auth=ADMIN,
                json={"password": "initial-pass"}).raise_for_status()
    return ("demo-user-1", "initial-pass")


def test_bulk_repeated_ids_renew_once():
    with TestClient(app) as client:
        client.post("/api/members/enroll-defaults", auth=ADMIN, json={})
        before = app.state.members.get("u1")["expires_at"]
        result = client.post("/api/members/bulk", auth=ADMIN,
                             json={"action": "renew", "user_ids": ["u1", "u1"], "days": 7})
        assert result.status_code == 200
        assert result.json()["ok"] == 1
        assert result.json()["requested"] == 1
        assert app.state.members.get("u1")["expires_at"] == before + 7 * 86400


def test_whoami_is_authenticated_member_not_config_owner():
    with TestClient(app) as client:
        member_auth = _role_admin(client)
        assert client.get("/api/whoami", auth=member_auth).json() == {"user": member_auth[0]}


@pytest.mark.parametrize("endpoint", ["/api/members/u1/password",
                                      "/api/members/u1/actions/reset-password",
                                      "/api/emby/users/u1/password"])
def test_password_change_revokes_cached_panel_login(endpoint):
    with TestClient(app) as client:
        member_auth = _role_admin(client)
        assert client.get("/api/whoami", auth=member_auth).status_code == 200
        payload = {"new_password" if endpoint.startswith("/api/emby/") else "password":
                   "replacement-pass"}
        assert client.post(endpoint, auth=ADMIN, json=payload).status_code == 200
        assert client.get("/api/whoami", auth=member_auth).status_code == 401
        assert client.get("/api/whoami", auth=(member_auth[0], "replacement-pass")).status_code == 200


def test_emby_connection_change_invalidates_old_server_caches():
    with TestClient(app) as client:
        for key in ("emby:libraries", "emby:sessions", "rate:old", "panelauth:old"):
            app.state.cache.set(key, "old-server")
        with patch.object(app.state.playback, "invalidate") as invalidate:
            result = client.put("/api/settings/emby", auth=ADMIN,
                                json={"url": "http://replacement.invalid", "api_key": "local-test"})
            assert result.status_code == 200
            invalidate.assert_called_once()
        for key in ("emby:libraries", "emby:sessions", "rate:old", "panelauth:old"):
            assert app.state.cache.get(key) is None


def test_failed_emby_save_preserves_caches():
    with TestClient(app) as client:
        app.state.cache.set("emby:libraries", "keep")
        result = client.put("/api/settings/emby", auth=ADMIN, json={"url": "not-a-url"})
        assert result.status_code == 422
        assert app.state.cache.get("emby:libraries") == "keep"


def test_image_cache_is_scoped_to_emby_origin():
    with (TestClient(app) as client,
          patch.object(app.state.images, "fetch", new_callable=AsyncMock,
                       return_value=(b"local-image", "image/png", "test-etag")) as fetch):
        for host in ("first", "second"):
            client.put("/api/settings/emby", auth=ADMIN,
                       json={"url": f"http://{host}.invalid", "api_key": "test"}).raise_for_status()
            result = client.get("/emby/Items/shared-id/Images/Primary")
            assert result.status_code == 200
        assert fetch.call_args_list[0].args[0] != fetch.call_args_list[1].args[0]


def test_change_group_reissues_changed_rate_only():
    with TestClient(app) as client:
        client.post("/api/members/enroll-defaults", auth=ADMIN, json={})
        group = client.post("/api/groups", auth=ADMIN,
                            json={"id": "audit-cap", "name": "audit cap", "billing_mode": "none",
                                  "bandwidth_limit_kbps": 1234}).json()
        with patch("app.main._reissue_rate_caps", new_callable=AsyncMock) as reissue:
            result = client.post("/api/members/u1/group", auth=ADMIN,
                                 json={"group_id": group["id"]})
            assert result.status_code == 200
            reissue.assert_awaited_once()
            assert reissue.call_args.kwargs["user_id"] == "u1"
        with patch("app.main._reissue_rate_caps", new_callable=AsyncMock) as reissue:
            assert client.post("/api/members/u1/group", auth=ADMIN,
                               json={"group_id": group["id"]}).status_code == 200
            reissue.assert_not_awaited()
