"""Actual restricted handlers: identity gates do not rely on role or client IDs."""
# ruff: noqa: F811 - imported pytest fixture name
import importlib
from unittest.mock import AsyncMock

import pytest
from test_external_entries import ADMIN, client  # noqa: F401

from app.main import app


@pytest.fixture
def restricted(client):
    client.put("/api/settings/integration", auth=ADMIN, json={"external_entries": [
        {"id": "vip", "origin": "https://vip.example.com", "whitelist_only": True}]})
    entry = app.state.settings_service.integration_config()["external_entries"][0]
    return client, {"X-Mediadeck-Entry": "vip", "X-Mediadeck-Entry-Key": entry["proxy_key"]}


@pytest.mark.parametrize("group,state,expected", [("whitelist", "active", 200), ("standard", "active", 403),
    ("whitelist", "suspended", 403)])
def test_login_filters_authenticated_upstream_identity_before_token_exposure(restricted, monkeypatch, group, state, expected):
    client, headers = restricted
    app.state.members.upsert("u1", "fixture", {"group_id": group, "status": state, "roles": ["admin"]})
    reply = {"AccessToken": "synthetic-issued", "User": {"Id": "u1"}, "SessionInfo": {"UserId": "u1"}}
    monkeypatch.setattr(importlib.import_module("app.main"), "login_request", AsyncMock(return_value=(200, reply)))
    r = client.post("/api/access/route-login", headers=headers, json={"Username": "fixture", "Pw": "synthetic"})
    assert r.status_code == expected
    assert ("synthetic-issued" in r.text) == (expected == 200)


def test_login_missing_or_conflicting_upstream_uid_and_key_fail_closed(restricted, monkeypatch):
    client, headers = restricted
    app.state.members.upsert("u1", "fixture", {"group_id": "whitelist"})
    monkeypatch.setattr(importlib.import_module("app.main"), "login_request", AsyncMock(return_value=(200,
        {"AccessToken": "synthetic-issued", "User": {"Id": "u1"}, "SessionInfo": {"UserId": "u2"}})))
    r = client.post("/api/access/route-login", headers=headers, json={})
    assert r.status_code == 503 and "synthetic-issued" not in r.text
    assert client.post("/api/access/route-login", headers={**headers, "X-Mediadeck-Entry-Key": "forged"}, json={}).status_code == 403


def test_real_settings_get_save_retains_restriction_and_existing_fields(restricted):
    client, _headers = restricted
    before = app.state.settings_service.integration_config()
    public = client.get("/api/settings/integration", auth=ADMIN).json()
    assert public["external_entries"][0]["whitelist_only"] is True
    assert client.put("/api/settings/integration", auth=ADMIN, json=public).status_code == 200
    assert app.state.settings_service.integration_config()["external_entries"] == before["external_entries"]
    lines = [{"label": "original", "url": "https://emby.example.com"},
             {"label": "VIP", "url": "https://vip.example.com", "whitelist_only": True}]
    assert client.post("/api/settings/telegram", auth=ADMIN, json={"playback_lines": lines}).status_code == 200
    public = client.get("/api/settings/telegram", auth=ADMIN).json()
    assert public["playback_lines"] == lines
    assert client.post("/api/settings/telegram", auth=ADMIN, json=public).status_code == 200
    assert app.state.settings_service.telegram_config()["playback_lines"] == lines
