"""Retired online-short integration must not return during a normal deployment."""
# ruff: noqa: F811 - fixture imported from the existing admission contract
import importlib.util
from unittest.mock import AsyncMock

from test_stream_admission import client, headers  # noqa: F401

from app.main import app


def test_retired_short_module_and_capability_route_are_absent(client):
    assert importlib.util.find_spec("app.modules.short_drama") is None
    assert not hasattr(app.state, "short_drama")
    assert "/api/playback/short-session" not in {
        getattr(route, "path", "") for route in app.routes
    }


def test_normal_playback_info_keeps_original_upstream_source(client, monkeypatch):
    original = {
        "PlaySessionId": "ordinary-play",
        "MediaSources": [{
            "Id": "ordinary-source", "Protocol": "File", "Path": "/media/example.mkv",
            "SupportsTranscoding": True, "Container": "mkv",
        }],
    }
    upstream = AsyncMock(return_value=(200, original))
    monkeypatch.setattr(app.state.emby, "playback_info", upstream)
    response = client.post("/api/playback/info/item42", headers=headers(), json={})
    assert response.status_code == 200, response.text
    assert response.json() == original
    assert upstream.await_count == 1
    assert app.state.db.query("SELECT * FROM stream_leases WHERE user_id='u1'") == []
    bound = app.state.db.one("SELECT * FROM watch_playbacks WHERE user_id='u1'")
    assert bound["play_id"] == "ordinary-play"
    assert bound["session_id"] == "a"


def test_retired_short_registry_setting_cannot_change_normal_admission(client, monkeypatch):
    monkeypatch.setenv("MEDIADECK_SHORT_REGISTRY", "/nonexistent/retired-registry.json")
    for device in "ab":
        response = client.get("/api/playback/admit", headers={
            **headers(device), "X-Original-URI": "/Videos/item42/stream.mkv?Static=true",
        })
        assert response.status_code == 204
    blocked = client.get("/api/playback/admit", headers={
        **headers("c"), "X-Original-URI": "/Videos/item42/stream.mkv?Static=true",
    })
    assert blocked.status_code == 403
