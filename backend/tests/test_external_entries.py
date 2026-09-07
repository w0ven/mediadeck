"""Registered entry routing on real Emby endpoints, including hostile input."""
from __future__ import annotations

from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from app.core.errors import ConfigError
from app.core.store import SettingsStore
from app.main import app
from app.modules.entries import (
    ENTRY_HEADER,
    KEY_HEADER,
    PlaybackEntry,
    entry_target,
    https_origin,
    identify_entry,
)
from app.modules.settings import SettingsService
from app.modules.signing import verify

ADMIN = ("admin", "change-me")
SIGNING_KEY = "synthetic-node-key"
ENTRIES = [
    {"id": "friend-a", "origin": "https://friend-a.example.com"},
    {"id": "friend-b", "origin": "https://friend-b.example.com"},
]
PATH = "/emby/videos/item42/original.mkv"


@pytest.fixture
def client():
    with TestClient(app) as client:
        for node in client.get("/api/nodes", auth=ADMIN).json():
            assert client.delete(f"/api/nodes/{node['name']}", auth=ADMIN).status_code == 200
        for name in ("edge-a", "edge-b"):
            result = client.post("/api/nodes", auth=ADMIN, json={
                "name": name, "base_url": f"https://{name}.example.com",
                "probe_url": "http://127.0.0.1:9800/load", "capacity": 40,
                "sign_secret": SIGNING_KEY, "sign_arg_digest": "k", "sign_arg_expires": "e",
                "pools": [
                    {"name": "main", "emby_prefix": "/media", "url_prefix": "/s/main"},
                    {"name": "archive", "emby_prefix": "/archive", "url_prefix": "/s/archive"},
                ],
            })
            assert result.status_code == 200
        result = client.put("/api/settings/integration", auth=ADMIN, json={
            "emby_public_url": "https://emby.example.com", "external_entries": ENTRIES,
        })
        assert result.status_code == 200
        assert client.put("/api/settings/playback", auth=ADMIN,
                          json={"enabled": True}).status_code == 200
        yield client


def entry_headers(entry_id="friend-a", token="client-emby-token"):
    entry = next(e for e in app.state.settings_service.integration_config()["external_entries"]
                 if e["id"] == entry_id)
    return {ENTRY_HEADER: entry_id, KEY_HEADER: entry["proxy_key"], "X-Emby-Token": token}


def request(client, headers, path=PATH, method="GET"):
    return client.request(method, path, headers=headers, follow_redirects=False)


def assert_direct(response):
    assert response.status_code == 302
    assert urlsplit(response.headers["location"]).hostname in {
        "edge-a.example.com", "edge-b.example.com"}
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("prefix", ["/emby/Videos", "/emby/videos", "/Videos", "/videos"])
@pytest.mark.parametrize("tail", ["original.mkv", "stream.mkv?Static=true", "stream?Static=1"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_actual_video_routes_keep_node_signature_and_rate(client, prefix, tail, method):
    client.put("/api/members/u1", auth=ADMIN, json={"group_id": "standard"})
    client.put("/api/members/u1/overrides", auth=ADMIN, json={"bandwidth_limit_kbps": 8000})
    response = request(client, entry_headers(), f"{prefix}/item42/{tail}", method)
    assert response.status_code == 302
    parsed = urlsplit(response.headers["location"])
    node = response.headers["x-mediadeck-node"]
    assert parsed.netloc == "friend-a.example.com"
    assert parsed.path.startswith(f"/_n/{node}/s/main/")
    args = parse_qs(parsed.query)
    assert args["r"] == ["1000000"] and args["u"][0]
    decoded_path = unquote(parsed.path.removeprefix(f"/_n/{node}"))
    assert verify(decoded_path, args["k"][0], int(args["e"][0]), SIGNING_KEY,
                  rate_bps=int(args["r"][0]), utag=args["u"][0])
    assert not verify(decoded_path, args["k"][0], int(args["e"][0]), SIGNING_KEY,
                      rate_bps=0, utag=args["u"][0])
    assert "no-store" in response.headers["cache-control"]
    assert KEY_HEADER.lower() not in response.headers


@pytest.mark.parametrize("headers", [
    {}, {"Host": "friend-a.example.com"}, {"X-Forwarded-Host": "friend-a.example.com"},
    {"Forwarded": 'host="friend-a.example.com";proto=https'},
    {ENTRY_HEADER: "friend-a"}, {ENTRY_HEADER: "friend-a", KEY_HEADER: "wrong"},
    {ENTRY_HEADER: "unknown", KEY_HEADER: "wrong"},
    {ENTRY_HEADER: "friend-a", KEY_HEADER: "x" * 200},
    {"Host": "evil.example.com", "X-Forwarded-For": "127.0.0.1"},
    {"X-Forwarded-Host": "friend-a.example.com,evil.example.com"},
])
def test_unknown_official_and_forged_assertions_keep_direct_targets(client, headers):
    assert_direct(request(client, {**headers, "X-Emby-Token": "client-emby-token"}))


def test_trusted_assertion_uses_registry_even_with_hostile_hosts(client):
    response = request(client, {**entry_headers(), "Host": "evil.example.com",
                               "X-Forwarded-Host": "other.example.com"})
    assert urlsplit(response.headers["location"]).netloc == "friend-a.example.com"


@pytest.mark.parametrize("header", [ENTRY_HEADER, KEY_HEADER])
def test_duplicate_assertion_headers_do_not_select_entry(client, header):
    headers = list(entry_headers().items())
    headers.append((header, dict(headers)[header]))
    assert_direct(request(client, headers))


def test_non_ascii_assertion_is_not_an_exception():
    headers = Headers(raw=[(ENTRY_HEADER.lower().encode(), b"friend-a"),
                           (KEY_HEADER.lower().encode(), b"\xff")])
    assert identify_entry(headers, [{"id": "friend-a", "proxy_key": "x" * 43,
                                     "origin": ENTRIES[0]["origin"]}]) is None


@pytest.mark.parametrize("key", ["", "invalid-token"])
def test_entry_credential_never_bypasses_emby_auth(client, key):
    response = request(client, {**entry_headers(token=key), "X-Mediadeck-Proxy": "1"})
    assert response.status_code == 204
    assert response.headers["x-mediadeck-fallback"] == "unauthorised"
    assert "location" not in response.headers


@pytest.mark.parametrize("path", [
    "/emby/videos/item42/master.m3u8", "/Videos/item42/stream.mkv",
    "/videos/item42/hls/segment1.ts", "/emby/Videos/item42/manifest.mpd",
])
@pytest.mark.parametrize(("proxy", "status"), [("1", 204), ("nginx", 418)])
def test_transcode_fallback_never_gets_entry_wrapper(client, path, proxy, status):
    response = request(client, {**entry_headers(), "X-Mediadeck-Proxy": proxy}, path)
    assert response.status_code == status
    assert response.headers["x-mediadeck-fallback"] == "transcode"
    assert "location" not in response.headers
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("condition", ["disabled", "unresolved", "no-node"])
def test_other_fallbacks_preserved(client, condition):
    if condition == "disabled":
        client.put("/api/settings/playback", auth=ADMIN, json={"enabled": False})
    elif condition == "no-node":
        for node in ("edge-a", "edge-b"):
            client.post(f"/api/nodes/{node}/disable", auth=ADMIN)
    path = PATH.replace("item42", "unknown") if condition == "unresolved" else PATH
    response = request(client, {**entry_headers(), "X-Mediadeck-Proxy": "nginx"}, path)
    assert response.status_code == 418 and "location" not in response.headers


def test_playback_caches_and_signed_urls_are_isolated_by_entry(client, monkeypatch):
    emby = app.state.emby
    access = AsyncMock(wraps=emby.verify_item_access)
    paths = AsyncMock(wraps=emby.item_media_paths)
    users = AsyncMock(wraps=emby.user_for_token)
    monkeypatch.setattr(emby, "verify_item_access", access)
    monkeypatch.setattr(emby, "item_media_paths", paths)
    monkeypatch.setattr(emby, "user_for_token", users)
    selected = set()
    for name in ("friend-a", "friend-a", "friend-b", "direct", "friend-b", "direct", "friend-a"):
        headers = entry_headers(name) if name != "direct" else {"X-Emby-Token": "client-emby-token"}
        response = request(client, headers)
        selected.add(response.headers["x-mediadeck-node"])
        if name == "direct":
            assert_direct(response)
        else:
            assert urlsplit(response.headers["location"]).hostname == f"{name}.example.com"
    # Separate metadata, positive auth and per-user rate caches; affinity
    # still uses the same media path, so entries do not scatter one file.
    assert access.await_count == paths.await_count == users.await_count == 3
    assert len(selected) == 1


def test_warm_entry_auth_does_not_authorise_another_entry(client, monkeypatch):
    assert request(client, entry_headers()).status_code == 302
    monkeypatch.setattr(app.state.emby, "verify_item_access", AsyncMock(return_value=False))
    for entry in ("friend-b", None):
        headers = entry_headers(entry) if entry else {"X-Emby-Token": "client-emby-token"}
        response = request(client, {**headers, "X-Mediadeck-Proxy": "1"})
        assert response.status_code == 204 and "location" not in response.headers


def test_entry_cannot_bypass_access_rules(client, monkeypatch):
    monkeypatch.setattr(app.state.access, "evaluate", Mock(return_value={
        "allowed": False, "reason": "test-block", "rule_id": "test-rule",
    }))
    pick = Mock(wraps=app.state.scheduler.pick)
    monkeypatch.setattr(app.state.scheduler, "pick", pick)
    response = request(client, entry_headers())
    assert response.status_code == 403 and "location" not in response.headers
    assert "no-store" in response.headers["cache-control"]
    pick.assert_not_called()


def test_version_pool_and_encoded_path_are_preserved(client, monkeypatch):
    path = "/archive/Movies/Demo/测试 % # + &.mkv"
    monkeypatch.setattr(app.state.emby, "item_media_paths", AsyncMock(return_value={"alt": path}))
    response = request(client, entry_headers(), PATH + "?MediaSourceId=alt")
    parsed = urlsplit(response.headers["location"])
    args = parse_qs(parsed.query)
    node = response.headers["x-mediadeck-node"]
    decoded = unquote(parsed.path.removeprefix(f"/_n/{node}"))
    assert decoded == path.replace("/archive", "/s/archive", 1)
    assert verify(decoded, args["k"][0], int(args["e"][0]), SIGNING_KEY,
                  rate_bps=int(args["r"][0]), utag=args["u"][0])


def test_wrapper_preserves_query_byte_for_byte():
    target = "https://edge.example.com/s/main/a%20b%25.mkv?r=7&u=x&e=123&k=a%2Bb&extra=&extra=2"
    wrapped = entry_target(target, "edge-a", PlaybackEntry("friend-a", ENTRIES[0]["origin"]))
    assert wrapped == ENTRIES[0]["origin"] + "/_n/edge-a" + target.removeprefix("https://edge.example.com")
    assert entry_target(target, "../evil", PlaybackEntry("a", ENTRIES[0]["origin"])) == target


@pytest.mark.parametrize("origin", [
    "http://friend.example.com", "https://friend.example.com/path", "https://x@y.example.com",
    "https://friend.example.com?x=1", "https://friend.example.com#frag",
    "https://*.example.com", "https://friend.example.com\\evil", "https://friend.example.com\n",
    "https://friend.example.com:", "https://friend.example.com:0", "https://friend.example.com:65536",
    "https://friend.example.com.", "https://bad_host.example.com", "https://example.com%2f.evil.com",
    "https://friend.example.com?", "https://friend.example.com#", "https://localhost", None,
])
def test_invalid_origin_is_rejected_atomically(client, origin):
    before = app.state.settings_service.integration_config()
    response = client.put("/api/settings/integration", auth=ADMIN, json={
        "tmdb_language": "en-US", "external_entries": [{"id": "bad", "origin": origin}],
    })
    assert response.status_code == 422
    assert app.state.settings_service.integration_config() == before


@pytest.mark.parametrize("entries", [
    "wrong", None, [{"id": "bad/id", "origin": ENTRIES[0]["origin"]}],
    [ENTRIES[0], ENTRIES[0]], [ENTRIES[0], {**ENTRIES[0], "id": "duplicate"}],
    [{"id": "official", "origin": "https://emby.example.com:443/"}],
    [{**ENTRIES[0], "rotate_proxy_key": "false"}], ENTRIES * 33,
])
def test_invalid_registry_is_rejected(client, entries):
    assert client.put("/api/settings/integration", auth=ADMIN,
                      json={"external_entries": entries}).status_code == 422


def test_canonical_origin():
    assert https_origin("https://FRIEND.example.com:443/") == "https://friend.example.com"
    assert https_origin("https://friend.example.com:8443") == "https://friend.example.com:8443"
    with pytest.raises(ConfigError):
        https_origin("https://friend.example.com:bad")


def test_settings_keep_keys_on_partial_save_and_roundtrip_and_reload(client):
    service = app.state.settings_service
    before = service.integration_config()
    public = client.get("/api/settings/integration", auth=ADMIN).json()
    for entry in before["external_entries"]:
        assert entry["proxy_key"] not in str(public)
    assert client.put("/api/settings/integration", auth=ADMIN, json=public).status_code == 200
    assert client.put("/api/settings/integration", auth=ADMIN,
                      json={"tmdb_language": "en-US"}).status_code == 200
    assert service.integration_config()["external_entries"] == before["external_entries"]
    reloaded = SettingsService(SettingsStore(service._store.path))
    assert reloaded.integration_config()["external_entries"] == before["external_entries"]
    assert service._store.path.stat().st_mode & 0o777 == 0o600


def test_rotation_removal_and_origin_change_take_effect_immediately(client):
    old_headers = entry_headers()
    assert request(client, old_headers).status_code == 302
    changed = [{**ENTRIES[0], "origin": "https://new-friend.example.com", "rotate_proxy_key": True},
               ENTRIES[1]]
    assert client.put("/api/settings/integration", auth=ADMIN,
                      json={"external_entries": changed}).status_code == 200
    assert_direct(request(client, old_headers))
    response = request(client, entry_headers())
    assert urlsplit(response.headers["location"]).netloc == "new-friend.example.com"
    current_headers = entry_headers()
    client.put("/api/settings/integration", auth=ADMIN, json={"external_entries": [ENTRIES[1]]})
    assert_direct(request(client, current_headers))


def test_entry_exports_require_admin_and_do_not_expose_keys_in_settings(client):
    assert client.put("/api/settings/integration", json={"external_entries": []}).status_code == 401
    url = "/api/integration/frontend?server=caddy&entry=friend-a"
    assert client.get(url).status_code == 401
    response = client.get(url, auth=ADMIN)
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    config = response.json()["config"]
    assert entry_headers()[KEY_HEADER] in config
    assert "handle /_n/edge-a/s/*" in config and "handle /_n/edge-b/s/*" in config
    assert "uri strip_prefix /_n/edge-a" in config
    assert "header_up Host edge-a.example.com" in config
    assert "tls_server_name edge-a.example.com" in config
    assert "reverse_proxy https://emby.example.com" in config
    assert "handle /_n/*" in config and "respond 404" in config
    assert "header_up -X-Emby-Token" in config
    assert "{host}" not in config and "{query" not in config
    assert client.get(url.replace("friend-a", "missing"), auth=ADMIN).status_code == 404
    assert client.get(url.replace("caddy", "nginx"), auth=ADMIN).status_code == 400


def test_nginx_origin_template_covers_routes_trust_and_cache(client):
    config = client.get("/api/integration/frontend?server=nginx", auth=ADMIN).json()["config"]
    assert r"location ~ ^/(emby/)?[Vv]ideos/[^/]+/(?i:stream|original)(\.[A-Za-z0-9]+)?$" in config
    assert 'if ($request_method !~ "^(GET|HEAD)$") { return 418; }' in config
    assert "proxy_set_header X-Mediadeck-Entry-Key $http_x_mediadeck_entry_key;" in config
    assert "proxy_set_header X-Mediadeck-Proxy nginx;" in config
    assert "error_page 418 500 502 503 504 = @mediadeck_emby_origin;" in config
    assert "proxy_cache off;" in config
    assert "proxy_ssl_verify on;" in config
    # 403 access denials must not be converted into a successful fallback.
    assert "error_page 403" not in config
