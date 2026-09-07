"""Pinned external entries: one stream hostname proxying exactly one node.

A CDN that can rewrite headers but cannot route by path gets two hostnames
instead of one. The redirect becomes ``https://<stream>/s/...`` with the signed
path untouched, and scheduling is restricted to the pinned node so the friend's
fixed upstream always matches. Path-routed entries must keep working unchanged.
"""
from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ConfigError
from app.main import app
from app.modules.entries import (
    ENTRY_HEADER,
    KEY_HEADER,
    PlaybackEntry,
    entry_target,
    validate_entries,
)
from app.modules.signing import verify

ADMIN = ("admin", "change-me")
SIGNING_KEY = "synthetic-node-key"
PATH = "/emby/videos/item42/original.mkv"
PINNED = {"id": "pinned-one", "origin": "https://friend-main.example.com",
          "stream_origin": "https://friend-stream.example.com", "node": "edge-b"}
ROUTED = {"id": "routed-one", "origin": "https://friend-routed.example.com"}


@pytest.fixture
def client():
    with TestClient(app) as client:
        for node in client.get("/api/nodes", auth=ADMIN).json():
            assert client.delete(f"/api/nodes/{node['name']}", auth=ADMIN).status_code == 200
        for name in ("edge-a", "edge-b"):
            assert client.post("/api/nodes", auth=ADMIN, json={
                "name": name, "base_url": f"https://{name}.example.com",
                "probe_url": "http://127.0.0.1:9800/load", "capacity": 40,
                "sign_secret": SIGNING_KEY, "sign_arg_digest": "k", "sign_arg_expires": "e",
                "pools": [{"name": "main", "emby_prefix": "/media", "url_prefix": "/s/main"}],
            }).status_code == 200
        assert client.put("/api/settings/integration", auth=ADMIN, json={
            "emby_public_url": "https://emby.example.com",
            "external_entries": [PINNED, ROUTED],
        }).status_code == 200
        assert client.put("/api/settings/playback", auth=ADMIN,
                          json={"enabled": True}).status_code == 200
        yield client


def headers(entry_id):
    entry = next(e for e in app.state.settings_service.integration_config()["external_entries"]
                 if e["id"] == entry_id)
    return {ENTRY_HEADER: entry_id, KEY_HEADER: entry["proxy_key"],
            "X-Emby-Token": "client-emby-token"}


# -- redirect shape ---------------------------------------------------------

@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_pinned_entry_redirects_to_stream_host_with_signature_intact(client, method):
    response = client.request(method, PATH, headers=headers("pinned-one"),
                              follow_redirects=False)
    assert response.status_code == 302
    parsed = urlsplit(response.headers["location"])
    # Stream hostname, plain /s/ path: the friend's CDN needs no path routing.
    assert parsed.netloc == "friend-stream.example.com"
    assert parsed.path.startswith("/s/main/")
    assert "/_n/" not in parsed.path
    # The node still verifies exactly what it signed.
    args = parse_qs(parsed.query)
    assert verify(unquote(parsed.path), args["k"][0], int(args["e"][0]), SIGNING_KEY,
                  rate_bps=int(args["r"][0]), utag=args["u"][0])
    # Pinned entries are served by their own node, never another one.
    assert response.headers["x-mediadeck-node"] == "edge-b"
    assert "no-store" in response.headers["cache-control"]
    assert KEY_HEADER.lower() not in response.headers


def test_path_routed_entry_is_unchanged_by_the_pinned_feature(client):
    """The existing /_n/<node>/ mode must behave exactly as before."""
    response = client.get(PATH, headers=headers("routed-one"), follow_redirects=False)
    assert response.status_code == 302
    parsed = urlsplit(response.headers["location"])
    assert parsed.netloc == "friend-routed.example.com"
    node = response.headers["x-mediadeck-node"]
    assert parsed.path.startswith(f"/_n/{node}/s/main/")


def test_unregistered_caller_still_gets_the_direct_node(client):
    response = client.get(PATH, headers={"X-Emby-Token": "client-emby-token"},
                          follow_redirects=False)
    assert response.status_code == 302
    assert urlsplit(response.headers["location"]).netloc in {
        "edge-a.example.com", "edge-b.example.com"}


def test_pinned_entry_survives_repeated_requests_on_its_own_node(client):
    """Affinity may prefer another node; the pin must win every time."""
    for _ in range(6):
        response = client.get(PATH, headers=headers("pinned-one"), follow_redirects=False)
        assert response.headers["x-mediadeck-node"] == "edge-b"
        assert urlsplit(response.headers["location"]).netloc == "friend-stream.example.com"


def test_pinned_node_removed_falls_back_to_emby_rather_than_a_wrong_node(client):
    """Fail open, and never hand out a URL the friend's fixed upstream 404s."""
    assert client.delete("/api/nodes/edge-b", auth=ADMIN).status_code == 200
    response = client.get(PATH, headers=headers("pinned-one"), follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    # Falls back to the Emby origin exactly like any other unroutable request.
    assert "/emby/videos/item42/original.mkv" in location
    # What must never happen: a stream URL the friend's fixed upstream 404s,
    # or a different node leaking through the pin.
    assert "friend-stream.example.com" not in location
    assert "edge-a.example.com" not in location
    assert "/s/main/" not in location


# -- entry_target unit behaviour --------------------------------------------

def test_entry_target_pinned_and_mismatch():
    target = "https://edge-b.example.com/s/main/Show/E01.mkv?k=sig&e=1"
    pinned = PlaybackEntry("p", "https://main.example.com",
                           stream_origin="https://stream.example.com", node="edge-b")
    assert entry_target(target, "edge-b", pinned) == (
        "https://stream.example.com/s/main/Show/E01.mkv?k=sig&e=1")
    # A node other than the pinned one would 404 on the friend's fixed upstream.
    assert entry_target(target, "edge-a", pinned) == target
    # Non-pinned entries keep the /_n/ shape.
    routed = PlaybackEntry("r", "https://main.example.com")
    assert entry_target(target, "edge-b", routed) == (
        "https://main.example.com/_n/edge-b/s/main/Show/E01.mkv?k=sig&e=1")


def test_entry_target_pinned_preserves_encoded_path():
    target = "https://edge-b.example.com/s/main/%E5%89%A7%20S01E01.mkv?k=s&e=1"
    pinned = PlaybackEntry("p", "https://main.example.com",
                           stream_origin="https://stream.example.com", node="edge-b")
    assert entry_target(target, "edge-b", pinned).endswith(
        "/s/main/%E5%89%A7%20S01E01.mkv?k=s&e=1")


# -- validation --------------------------------------------------------------

@pytest.mark.parametrize("row", [
    {"id": "e", "origin": "https://a.example.com", "stream_origin": "https://s.example.com"},
    {"id": "e", "origin": "https://a.example.com", "node": "edge-a"},
])
def test_half_configured_pin_is_rejected(row):
    with pytest.raises(ConfigError):
        validate_entries([row], [], "", {"edge-a"})


def test_pin_requires_a_known_node():
    with pytest.raises(ConfigError):
        validate_entries([{"id": "e", "origin": "https://a.example.com",
                           "stream_origin": "https://s.example.com", "node": "ghost"}],
                         [], "", {"edge-a"})


@pytest.mark.parametrize("stream", [
    "https://a.example.com",       # same as this entry's own origin
    "https://emby.example.com",    # the official Emby origin
    "https://other.example.com",   # another entry's origin
])
def test_stream_origin_cannot_collide(stream):
    rows = [{"id": "other", "origin": "https://other.example.com"},
            {"id": "e", "origin": "https://a.example.com",
             "stream_origin": stream, "node": "edge-a"}]
    with pytest.raises(ConfigError):
        validate_entries(rows, [], "https://emby.example.com", {"edge-a"})


def test_pin_survives_a_read_edit_write_round_trip(client):
    """Editing an unrelated field must not silently drop the pin or the key."""
    before = client.get("/api/settings/integration", auth=ADMIN).json()
    pinned = next(e for e in before["external_entries"] if e["id"] == "pinned-one")
    assert pinned["stream_origin"] == "https://friend-stream.example.com"
    assert pinned["node"] == "edge-b"
    key = next(e for e in app.state.settings_service.integration_config()["external_entries"]
               if e["id"] == "pinned-one")["proxy_key"]
    assert client.put("/api/settings/integration", auth=ADMIN, json={
        "external_entries": [{k: v for k, v in e.items() if k != "proxy_key_set"}
                             for e in before["external_entries"]],
        "external_entries_revision": before["external_entries_revision"],
    }).status_code == 200
    after = app.state.settings_service.integration_config()["external_entries"]
    kept = next(e for e in after if e["id"] == "pinned-one")
    assert kept["stream_origin"] == "https://friend-stream.example.com"
    assert kept["node"] == "edge-b"
    assert kept["proxy_key"] == key


def test_public_settings_never_expose_the_key(client):
    body = client.get("/api/settings/integration", auth=ADMIN).json()
    assert all("proxy_key" not in e for e in body["external_entries"])
    assert all(e["proxy_key_set"] for e in body["external_entries"])


# -- exported friend configuration ------------------------------------------

@pytest.mark.parametrize("server", ["caddy", "nginx"])
def test_pinned_export_has_two_hosts_and_no_path_routing(client, server):
    response = client.get(
        f"/api/integration/frontend?server={server}&entry=pinned-one", auth=ADMIN)
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    config = response.json()["config"]
    key = next(e for e in app.state.settings_service.integration_config()["external_entries"]
               if e["id"] == "pinned-one")["proxy_key"]
    # Two fixed hostnames, no /_n/ anywhere: the CDN needs no path rules.
    assert "friend-main.example.com" in config and "friend-stream.example.com" in config
    assert "/_n/" not in config
    # Only the pinned node is reachable from this configuration.
    assert "edge-b.example.com" in config
    assert "edge-a.example.com" not in config
    # The credential rides on the Emby hop only, exactly once.
    assert config.count(key) == 1
    emby_part, stream_part = config.split("friend-stream.example.com", 1)
    assert key in emby_part and key not in stream_part
    # Viewer credentials are stripped before the node hop.
    for token in ("Authorization", "Cookie", "X-Emby-Token"):
        assert token in stream_part
    assert "no-store" in config


def test_pinned_export_refuses_after_its_node_disappears(client):
    assert client.delete("/api/nodes/edge-b", auth=ADMIN).status_code == 200
    response = client.get("/api/integration/frontend?server=caddy&entry=pinned-one",
                          auth=ADMIN)
    assert response.status_code == 422
