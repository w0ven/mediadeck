"""Actual nginx -> gateway -> Deck handlers; all upstreams are loopback fixtures."""
import hashlib
import http.client
import importlib.util
import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_membership import _basic
from test_stream_admission import client as client  # noqa: PLC0414 - fixture re-export
from test_stream_admission import headers, row

from app.main import app
from app.modules.playback import Decision

ROOT = Path(__file__).resolve().parents[2]
PATHS = {
    "forward": "/emby/videos/item42/original.mkv?MediaSourceId=unknown&DeviceId=a",
    "cmcc": "/Videos/42/stream.mkv?Static=true&MediaSourceId=mobile&DeviceId=a",
    "local": "/emby/Videos/123/stream.mp4?Static=true&MediaSourceId=local&DeviceId=a",
}


@pytest.fixture()
def chain(client, tmp_path, monkeypatch):
    nginx = shutil.which("nginx")
    if not nginx:
        pytest.skip("nginx required for gateway admission integration")
    spec = importlib.util.spec_from_file_location("gateway_integration", ROOT / "deploy/gateway/gateway.py")
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    app.state.members.set_overrides("u1", {"max_streams": 1})
    for name in ("user_for_token", "verify_item_access", "active_sessions_raw"):
        monkeypatch.setattr(app.state.emby, name, AsyncMock(wraps=getattr(app.state.emby, name)))
    monkeypatch.setattr(app.state.streams, "inspect", AsyncMock(wraps=app.state.streams.inspect))
    monkeypatch.setattr(app.state.playback, "route", AsyncMock(return_value=Decision(
        True, "https://node.invalid/s/main/fixture?k=test", "direct", node="edge-a")))

    storage = tmp_path / "media-fixture"
    local_file = storage / "hg-12345/show/Season 01/show - S01E001.mp4"
    local_file.parent.mkdir(parents=True)
    local_file.write_bytes(b"x" * 2048)
    local_path = "/virtual/" + local_file.relative_to(storage).as_posix()
    local_reg = tmp_path / "local.json"
    local_reg.write_text(json.dumps({"123": {
        "itemId": 123, "ready": True, "decoded": True, "root": "/virtual/hg-12345",
        "path": local_path, "size": 2048, "mtimeNs": local_file.stat().st_mtime_ns,
        "sha256": "a" * 64, "sourceId": "local",
    }}))
    source_url = "https://openlist.invalid/d/fixture?sign=synthetic"
    registry = tmp_path / "sources.json"
    registry.write_text(json.dumps({"mobile": {"url": source_url, "item_ids": ["42"]}}))
    gateway.REG = registry
    gateway.OPENLIST_PUBLIC_HOST = "openlist.invalid"
    gateway.DUANJU_LOCAL_REGISTRY = local_reg
    gateway.DUANJU_EMBY_ROOT = "/virtual"
    gateway.DUANJU_STORAGE_ROOT = storage
    state = SimpleNamespace(deck=[], emby=[], cloud=[], override=None, gateway=gateway,
                            sources=[{"Id": "mobile"}, {"Id": "local", "Protocol": "File", "Path": local_path}],
                            cap=hashlib.sha256(source_url.encode()).hexdigest())
    servers, processes = [], []

    def serve(handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True).start()
        servers.append(server)
        return server.server_port

    class Backend(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            self.do_GET()

        def do_GET(self):
            state.deck.append((self.command, self.path, list(self.headers.items())))
            if state.override is not None and self.path == "/api/playback/admit":
                code, body, hs = state.override, b"", {}
            else:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                reply = client.request(self.command, self.path, headers=list(self.headers.items()),
                                       content=body, follow_redirects=False)
                code, body, hs = reply.status_code, reply.content, reply.headers
            self.send_response(code)
            for k, v in hs.items():
                if k.lower() not in ("content-length", "connection", "transfer-encoding"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

    class Emby(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            state.emby.append((self.command, self.path, dict(self.headers)))
            if self.path.endswith("/PlaybackInfo"):
                self.send_response(200)
                body = json.dumps({"MediaSources": state.sources}).encode()
            else:
                self.send_response(206 if self.headers.get("Range") else 200)
                body = b"synthetic bytes"
                self.send_header("Content-Range", "bytes 0-14/2048")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

    deck_port, emby_port = serve(Backend), serve(Emby)
    connection = http.client.HTTPConnection

    def local_connection(host, port, timeout):
        assert host == "127.0.0.1" and port in (8300, 8096), "no production/network calls"
        return connection(host, {8300: deck_port, 8096: emby_port}[port], timeout=timeout)

    monkeypatch.setattr(gateway, "http", SimpleNamespace(client=SimpleNamespace(HTTPConnection=local_connection)))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def metadata(req, timeout):
        assert req.full_url.startswith("http://127.0.0.1:8096/emby/Items/")
        req = urllib.request.Request(req.full_url.replace(":8096/", f":{emby_port}/", 1),
                                     headers=dict(req.header_items()), method=req.get_method())
        return opener.open(req, timeout=timeout)

    # Avoid patching the global urllib used by httpx/other tests.
    monkeypatch.setattr(gateway, "urllib", SimpleNamespace(parse=urllib.parse, error=urllib.error,
        request=SimpleNamespace(Request=urllib.request.Request, urlopen=metadata)))

    def cloud(req, timeout):
        assert req.full_url.startswith("http://127.0.0.1:5244/d/")
        state.cloud.append(req.full_url)
        return SimpleNamespace(code=302, headers={"Location": "https://fixture.cmecloud.cn/file"}, close=lambda: None)

    monkeypatch.setattr(gateway, "OPENER", SimpleNamespace(open=cloud))
    state.deck_url = f"http://127.0.0.1:{deck_port}"
    state.gateway_url = f"http://127.0.0.1:{serve(gateway.Handler)}"

    def edge(old):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        stream = (ROOT / "deploy/nginx/gateway-stream.conf").read_text()
        if old:
            stream = stream.replace('    proxy_pass http://deck_playback_gateway;',
                                    '    auth_request /_deck_admission;\n    proxy_pass http://deck_playback_gateway;')
        admission = (ROOT / "deploy/nginx/playback-admission.conf").read_text()
        config = tmp_path / ("old-nginx.conf" if old else "new-nginx.conf")
        config.write_text(f'''daemon off; master_process off;
pid {tmp_path}/nginx-{port}.pid; error_log {tmp_path}/nginx-{port}.log;
events {{ worker_connections 128; }}
http {{ access_log off;
 upstream deck_admission_backend {{ server 127.0.0.1:{deck_port}; }}
 upstream deck_playback_gateway {{ server {state.gateway_url.removeprefix('http://')}; }}
 upstream emby_playback_origin {{ server 127.0.0.1:{emby_port}; }}
 server {{ listen 127.0.0.1:{port};
 {stream}
 {admission}
 location ^~ /cmcc-source/ {{ proxy_pass {state.gateway_url}; }}
 location / {{ proxy_pass http://emby_playback_origin; }}
 }} }}''')
        subprocess.run([nginx, "-t", "-p", str(tmp_path), "-c", str(config)], check=True, capture_output=True)
        proc = subprocess.Popen([nginx, "-p", str(tmp_path), "-c", str(config)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(proc)
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), .1):
                    break
            except OSError:
                time.sleep(.01)
        return f"http://127.0.0.1:{port}"

    state.edge_url, state.oldedge_url = edge(False), edge(True)
    try:
        with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as net:
            state.net = net
            yield state
    finally:
        for proc in processes:
            proc.terminate()
            proc.wait(timeout=5)
        for server in reversed(servers):
            server.shutdown()
            server.server_close()


@pytest.mark.parametrize("branch", ["forward", "cmcc", "local"])
@pytest.mark.parametrize("entrance", ["gateway", "edge", "oldedge"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_selected_issuer_runs_one_full_gate_and_old_nginx_is_safe(chain, branch, entrance, method):
    path = PATHS[branch] + "&Marker=a%2Bb%26c"
    result = chain.net.request(method, getattr(chain, entrance + "_url") + path,
                               headers={**headers(), "Range": "bytes=0-14",
                                        "X-Original-URI": "/Videos/forged/original?api_key=other",
                                        "X-Original-Method": "POST"})
    assert result.status_code == (206 if branch == "local" else 302)
    expected = 2 if entrance == "oldedge" else 1
    assert app.state.streams.inspect.await_count == expected
    assert app.state.emby.active_sessions_raw.await_count == expected
    assert app.state.emby.user_for_token.await_count == expected
    assert app.state.emby.verify_item_access.await_count == expected
    gates = [r for r in chain.deck if r[1] == "/api/playback/admit"]
    assert len(gates) == (expected if branch != "forward" else expected - 1)
    if branch == "forward":
        assert chain.deck[-1][:2] == (method, path)
    else:
        sent = httpx.Headers(gates[-1][2])
        assert sent["X-Original-URI"] == path and sent["X-Original-Method"] == method
        assert sent["X-Emby-Token"] == "tok:u1" and sent["Range"] == "bytes=0-14"
    if method == "HEAD":
        assert result.content == b""
    if branch == "local":
        assert chain.emby[-1][:2] == (method, path)


@pytest.mark.parametrize("branch", ["forward", "cmcc", "local"])
@pytest.mark.parametrize("failure", ["token", "item", "device", "sessions", "cap", "web", "client"])
def test_all_issuers_keep_complete_rules_and_never_fall_back(chain, monkeypatch, branch, failure):
    if failure == "token":
        app.state.emby.user_for_token.side_effect = lambda *_: None
    elif failure == "item":
        app.state.emby.verify_item_access.side_effect = lambda *_: False
    elif failure == "device":
        monkeypatch.setattr(app.state.members, "device_blocked", lambda *_: True)
    elif failure == "sessions":
        app.state.emby.active_sessions_raw.side_effect = RuntimeError("offline")
    elif failure == "cap":
        # A pending seat must refuse but must never produce a punishment event.
        assert chain.net.get(chain.deck_url + PATHS["forward"].replace("DeviceId=a", "DeviceId=b"),
                             headers=headers("b")).status_code == 302
        app.state.playback.route.reset_mock()
    else:
        app.state.emby.set_sessions([{**row("a"), "Client": "Emby Web" if failure == "web" else "BlockedClient"}])
        if failure == "web":
            app.state.restrictions.save({"web_play": {"enabled": True, "action": "pause"}})
        else:
            app.state.restrictions.save_client_config({"enabled": True, "blocked_patterns": ["BlockedClient"], "exempt_groups": []})
    result = chain.net.get(chain.edge_url + PATHS[branch], headers={**headers(),
        "X-Original-URI": "/Videos/allowed/original?api_key=tok:other", "X-Original-Method": "HEAD",
        "X-Mediadeck-Admitted": "1", "X-Mediadeck-User-Id": "u1"})
    assert result.status_code == (503 if failure == "sessions" else 403)
    assert "location" not in result.headers
    assert not chain.cloud and not chain.emby
    app.state.playback.route.assert_not_awaited()
    if failure in ("sessions", "cap"):
        assert app.state.restrictions.events() == []


@pytest.mark.parametrize("branch", ["cmcc", "local"])
@pytest.mark.parametrize("status", [200, 302, 401, 403, 404, 500, 503])
def test_gateway_requires_exact_admission_success_and_never_follows_redirects(chain, branch, status):
    chain.override = status
    result = chain.net.get(chain.gateway_url + PATHS[branch], headers=headers())
    assert result.status_code == (status if status in (401, 403) else 503)
    assert not chain.cloud and not chain.emby and "location" not in result.headers


@pytest.mark.parametrize("branch", ["forward", "cmcc", "local"])
def test_repeat_requests_recheck_revocation_and_missing_credentials(chain, branch):
    url = chain.gateway_url + PATHS[branch]
    assert chain.net.get(url).status_code == 401
    assert chain.net.get(url, headers=headers()).status_code in (200, 302)
    app.state.emby.verify_item_access.side_effect = lambda *_: False
    before = (len(chain.cloud), len(chain.emby), app.state.playback.route.await_count)
    assert chain.net.get(url, headers=headers()).status_code == 403
    assert before == (len(chain.cloud), len(chain.emby), app.state.playback.route.await_count)


@pytest.mark.parametrize("branch", ["forward", "cmcc", "local"])
def test_unavailable_deck_never_falls_back_or_issues(chain, monkeypatch, branch):
    def offline(*args, **kwargs):
        raise TimeoutError("synthetic Deck timeout")
    monkeypatch.setattr(chain.gateway.Handler, "deck_request", offline)
    result = chain.net.get(chain.gateway_url + PATHS[branch], headers=headers())
    assert result.status_code == (502 if branch == "forward" else 503)
    assert not chain.cloud and not chain.emby and "location" not in result.headers


@pytest.mark.parametrize("branch", ["forward", "cmcc", "local"])
def test_old_gateway_gate_contract_is_safe_only_while_outer_gate_remains(chain, monkeypatch, branch):
    # Model the OLD gateway's absence of self-issuer admission; nginx must
    # remain on the old config until the new gateway is active everywhere.
    monkeypatch.setattr(chain.gateway.Handler, "admit", lambda self: True)
    url = chain.oldedge_url + PATHS[branch]
    assert chain.net.get(url, headers=headers()).status_code in (200, 302)
    assert app.state.streams.inspect.await_count == (2 if branch == "forward" else 1)
    app.state.emby.verify_item_access.side_effect = lambda *_: False
    before = (len(chain.cloud), len(chain.emby), len(chain.deck))
    assert chain.net.get(url, headers=headers()).status_code == 403
    assert before[:2] == (len(chain.cloud), len(chain.emby))
    assert len(chain.deck) == before[2] + 1  # only the outer rejection, no issuer hop


@pytest.mark.parametrize("branch", ["cmcc", "local"])
def test_exact_source_check_still_runs_after_admission(chain, branch):
    chain.sources = []
    result = chain.net.get(chain.gateway_url + PATHS[branch], headers=headers())
    assert result.status_code == 403 and not chain.cloud
    assert len(chain.emby) == 1 and chain.emby[0][1].endswith("/PlaybackInfo")
    assert app.state.streams.inspect.await_count == 1


def test_nonstatic_and_unknown_routes_forward_without_gateway_admission(chain):
    nonstatic = PATHS["cmcc"].replace("Static=true", "Static=false")
    result = chain.net.get(chain.gateway_url + nonstatic, headers=headers())
    assert result.status_code == 302  # test routing stub, not a real media URL
    assert app.state.streams.inspect.await_count == 1
    assert all(path != "/api/playback/admit" for _, path, _ in chain.deck)
    assert not chain.cloud and not chain.emby
    before = len(chain.deck)
    assert chain.net.get(chain.gateway_url + "/health").status_code == 200
    assert chain.net.post(chain.gateway_url + PATHS["cmcc"], headers=headers()).status_code == 501
    assert len(chain.deck) == before


def test_direct_deck_retains_auth_and_ignores_forged_original_uri(chain):
    url = chain.deck_url + PATHS["forward"]
    assert chain.net.get(url).status_code == 401
    assert chain.net.head(url, headers={**headers(), "X-Original-URI": "/different", "X-Mediadeck-Admitted": "1"}).status_code == 302
    assert app.state.streams.inspect.await_count == 1
    assert app.state.emby.verify_item_access.await_args.args[0] == "item42"


@pytest.mark.parametrize("branch", ["forward", "cmcc", "local"])
def test_native_authorization_and_query_identity_are_preserved(chain, branch):
    path = PATHS[branch]
    auth = {'Authorization': 'MediaBrowser Token="tok:u1", DeviceId="a", Client="Hills", Version="1"'}
    assert chain.net.head(chain.gateway_url + path, headers=auth).status_code in (200, 302)
    assert chain.net.get(chain.gateway_url + path + "&api_key=tok%3Au1").status_code in (200, 302)
    assert app.state.emby.user_for_token.await_args.args == ("tok:u1", "a")
    if branch != "forward":
        gate = httpx.Headers(chain.deck[0][2])
        assert gate["Authorization"] == auth["Authorization"]


@pytest.mark.parametrize("branch", ["forward", "cmcc", "local"])
def test_whitelist_membership_and_uri_checks_follow_issuer(chain, client, monkeypatch, branch):
    response = client.put("/api/settings/integration", headers=_basic(), json={"external_entries": [
        {"id": "vip", "origin": "https://vip.example.com", "whitelist_only": True}]})
    assert response.status_code == 200
    entry = app.state.settings_service.integration_config()["external_entries"][0]
    caller = {**headers(), "X-Mediadeck-Entry": "vip", "X-Mediadeck-Entry-Key": entry["proxy_key"]}
    monkeypatch.setattr(app.state.emby, "personal_user_for_token", AsyncMock(return_value="u1"), raising=False)
    # Ordinary membership must not enter a registered restricted route.
    assert chain.net.get(chain.gateway_url + PATHS[branch], headers=caller).status_code == 403
    app.state.members.upsert("u1", "fixture", {"group_id": "whitelist"})
    assert chain.net.get(chain.gateway_url + PATHS[branch], headers=caller).status_code in (200, 302)
    # Removing nginx's direct auth subrequest must not drop strict target checks.
    for suffix in ("&api_key=tok:u1&api_key=tok:u1", "&DeviceId=a", "&%61pi_key=tok:u1"):
        assert chain.net.get(chain.gateway_url + PATHS[branch] + suffix, headers=caller).status_code == 403
    duplicates = [*caller.items(), ("X-Emby-Token", "tok:u1")]
    assert chain.net.get(chain.gateway_url + PATHS[branch], headers=duplicates).status_code == 403
    app.state.members.upsert("u1", "fixture", {"group_id": "whitelist", "status": "suspended"})
    assert chain.net.get(chain.gateway_url + PATHS[branch], headers=caller).status_code == 403


def test_hls_native_metadata_and_capability_routes_keep_their_gates(chain, monkeypatch):
    # Non-issuer HLS remains nginx -> auth_request -> Emby, not gateway/Deck bytes.
    hls = "/Videos/item42/master.m3u8?DeviceId=a"
    assert chain.net.get(chain.edge_url + hls).status_code == 401
    assert chain.net.get(chain.edge_url + hls, headers=headers()).status_code == 200
    assert app.state.streams.inspect.await_count == 1
    assert len(chain.emby) == 1 and not chain.cloud
    info = chain.net.post(chain.edge_url + "/emby/Items/item42/PlaybackInfo", headers=headers(), json={})
    assert info.status_code == 200 and info.json()["PlaySessionId"]
    # Opaque source URLs retain their existing bearer-capability contract, not
    # an unauthenticated stream-start exception. Wrong capability never resolves.
    assert chain.net.get(chain.edge_url + "/cmcc-source/mobile?cap=wrong").status_code == 403
    before = app.state.streams.inspect.await_count
    assert chain.net.head(chain.edge_url + "/cmcc-source/mobile?cap=" + chain.cap).status_code == 302
    assert app.state.streams.inspect.await_count == before
    app.state.emby.active_sessions_raw.side_effect = RuntimeError("offline")
    count = len(chain.emby)
    assert chain.net.get(chain.edge_url + hls, headers=headers()).status_code == 500
    assert len(chain.emby) == count
    assert chain.net.post(chain.edge_url + PATHS["forward"], headers=headers()).status_code == 405
