"""Real nginx edge -> TLS origin -> actual Deck handlers, only loopback fixtures."""
import json
import shutil
import socket
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import httpx
import pytest
from test_external_entries import ADMIN, SIGNING_KEY, client  # noqa: F401

from app.main import app
from app.modules.signing import sign_url, user_tag, verify

pytestmark = pytest.mark.skipif(not shutil.which("nginx") or not shutil.which("openssl"), reason="nginx/openssl required")


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_real_edge_origin_phases_and_bytes(client, tmp_path):  # noqa: F811
    client.delete("/api/nodes/edge-b", auth=ADMIN)
    client.put("/api/members/u1", auth=ADMIN, json={"group_id": "whitelist"})
    client.put("/api/members/u2", auth=ADMIN, json={"group_id": "standard"})
    app.state.emby.set_sessions([{"Id": "entry-session", "UserId": "u1", "DeviceId": "entry-client"},
                                 {"Id": "normal-session", "UserId": "u2", "DeviceId": "normal-client"}])
    app.state.emby.personal_user_for_token = AsyncMock(side_effect=lambda token: {"vip-token": "u1", "normal-token": "u2"}.get(token))
    client.put("/api/settings/integration", auth=ADMIN, json={"external_entries": [
        {"id": "vip", "origin": "https://vip.example.com", "whitelist_only": True}]})
    entry = app.state.settings_service.integration_config()["external_entries"][0]
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"mobile-source": {"item_ids": ["item-mobile"]}}))
    app.state.whitelist_route.registry_path = str(registry)
    async def info(item, method, headers, query, payload):
        sid = "gd-source"
        q = {"MediaSourceId": sid, "PlaySessionId": "play42", "DeviceId": "entry-client", "api_key": "vip-token"}
        return 200, {"PlaySessionId": "play42", "MediaSources": [{"Id": sid, "Path": "/media/E01.mkv",
                      "TranscodingUrl": "/Videos/item42/master.m3u8?" + urlencode(q)}]}
    app.state.emby.playback_info = info
    app.state.emby.playback_manifest = AsyncMock(return_value=(200, "#EXTM3U\n#EXTINF:6,\nhls1/main/0.ts?PlaySessionId=play42\n"))
    app.state.emby.media_sources_for_token = AsyncMock(side_effect=lambda item, token: [
        {"Id": "mobile-source", "Path": "/cmcc/E01.mkv"} if item == "item-mobile" else {"Id": "gd-source", "Path": "/media/E01.mkv"}])
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-keyout", str(key),
        "-out", str(cert), "-subj", "/CN=vip.example.com", "-addext",
        "subjectAltName=DNS:vip.example.com,DNS:emby.example.com,DNS:edge-a.example.com,IP:127.0.0.1"],
        check=True, capture_output=True)
    records = []
    payload = b"0123456789abcdef"
    servers, threads = [], []
    def server(role, tls=False):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_HEAD(self):
                self.do_GET()
            def do_POST(self):
                self.do_GET()
            def do_GET(self):
                records.append((role, self.command, self.path))
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                headers, status, content = {}, 200, payload
                if role == "deck":
                    reply = client.request(self.command, self.path, headers=dict(self.headers), content=body, follow_redirects=False)
                    status, content, headers = reply.status_code, reply.content, dict(reply.headers)
                elif role == "gateway":
                    # Preserve full signed query, exactly as the real router does.
                    signed = sign_url("https://edge-a.example.com", "/s/main/E01.mkv", SIGNING_KEY, 300,
                        arg_digest="k", arg_expires="e", utag=user_tag("u1"), rate_bps=1000000)
                    headers["Location"] = "https://vip.example.com/_n/edge-a" + signed.removeprefix("https://edge-a.example.com")
                    status, content = 302, b""
                elif role == "node":
                    u = urlsplit(self.path)
                    q = parse_qs(u.query)
                    assert verify(unquote(u.path), q["k"][0], int(q["e"][0]), SIGNING_KEY,
                                  rate_bps=int(q["r"][0]), utag=q["u"][0])
                    assert "X-Mediadeck-Entry-Key" not in self.headers
                    if self.headers.get("Range"):
                        status, content, headers = 206, payload[4:8], {"Content-Range": "bytes 4-7/16"}
                self.send_response(status)
                for name, value in headers.items():
                    if name.lower() not in ("content-length", "connection", "transfer-encoding"):
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(content)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        servers.append(srv)
        threads.append(thread)
        return srv.server_port
    deck, emby, gateway, node = server("deck"), server("emby"), server("gateway"), server("node", True)
    origin, edge, plain = port(), port(), port()
    values = {"__ORIGIN_IP__": "127.0.0.1", "__ENTRY_IP__": "127.0.0.1", "__ENTRY_HOST__": "vip.example.com",
              "__OFFICIAL_HOST__": "emby.example.com", "__PANEL_HOST__": "deck.example.com", "__ENTRY_ID__": "vip",
              "__ENTRY_KEY__": entry["proxy_key"], "__NODE_NAME__": "edge-a", "__NODE_IP__": "127.0.0.1", "__NODE_HOST__": "edge-a.example.com"}
    def render(name):
        text = (Path(__file__).resolve().parents[2] / "deploy/nginx" / name).read_text()
        for src, dest in values.items():
            text = text.replace(src, dest)
        text = text.replace("127.0.0.1:8443", f"127.0.0.1:{origin}").replace("127.0.0.1:443", f"127.0.0.1:{node}")
        text = text.replace("127.0.0.1:8300", f"127.0.0.1:{deck}").replace("127.0.0.1:8096", f"127.0.0.1:{emby}").replace("127.0.0.1:8339", f"127.0.0.1:{gateway}")
        text = text.replace("listen 443", f"listen 127.0.0.1:{edge}").replace("listen 80", f"listen 127.0.0.1:{plain}")
        import re
        text = re.sub(r"/etc/letsencrypt/live/[^/]+/fullchain.pem", str(cert), text)
        text = re.sub(r"/etc/letsencrypt/live/[^/]+/privkey.pem", str(key), text)
        text = text.replace("/etc/ssl/certs/ca-certificates.crt", str(cert))
        text = text.replace("/var/log/nginx/mediadeck-whitelist-origin.log", str(tmp_path / "origin.log"))
        text = text.replace("/var/log/nginx/mediadeck-whitelist-edge.log", str(tmp_path / "edge.log"))
        return text
    config = tmp_path / "nginx.conf"
    config.write_text(f"daemon off; pid {tmp_path}/nginx.pid; error_log {tmp_path}/nginx-error.log; events {{}} http {{\n"
                      + render("whitelist-origin.conf.template") + render("whitelist-edge.conf.template") + "\n}")
    checked = subprocess.run(["nginx", "-t", "-p", str(tmp_path), "-c", str(config)], capture_output=True, check=False)
    assert checked.returncode == 0, checked.stderr.decode()
    proc = subprocess.Popen(["nginx", "-p", str(tmp_path), "-c", str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tls_ctx = ssl.create_default_context(cafile=str(cert))
    try:
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", edge), .1):
                    break
            except OSError:
                time.sleep(.05)
        with httpx.Client(base_url=f"https://127.0.0.1:{edge}", verify=tls_ctx, timeout=10, trust_env=False) as net:
            vip = {"Host": "vip.example.com", "X-Emby-Token": "vip-token", "X-Emby-Device-Id": "entry-client"}
            normal = {**vip, "X-Emby-Token": "normal-token", "X-Emby-Device-Id": "normal-client"}
            assert net.get("/System/Info", headers={"Host": "wrong.example"}).status_code == 403
            assert net.get("/System/Info", headers={"Host": "vip.example.com"}).status_code == 401
            assert net.get("/System/Info", headers=normal).status_code == 403
            assert net.get("/System/Info", headers=vip).status_code == 200
            assert net.get("/System/Info/Public", headers={"Host": "vip.example.com"}).status_code == 200
            assert net.get("/System/Info/Public", headers={"Host": "vip.example.com", "Upgrade": "websocket"}).status_code == 401
            reply = net.get("/Items/item42/PlaybackInfo", headers=vip)
            assert reply.status_code == 200
            media = reply.json()["MediaSources"][0]["TranscodingUrl"]
            parsed = urlsplit(media)
            playlist = net.get(parsed.path + "?" + parsed.query, headers=vip)
            assert playlist.status_code == 200
            child = urlsplit(playlist.text.splitlines()[-1])
            assert net.get(child.path + "?" + child.query, headers=vip).content == payload
            assert net.get(child.path + "?" + child.query.replace("gd-source", "mobile-source"), headers=vip).status_code == 403
            assert net.get("/Videos/item42/hls1/main/0.ts?PlaySessionId=play42", headers=vip).status_code == 403
            direct = net.get("/Videos/item42/original.mkv", headers=vip)
            assert direct.status_code == 302
            signed = urlsplit(direct.headers["location"])
            file_path = signed.path + "?" + signed.query
            byte = net.get(file_path, headers={**vip, "Range": "bytes=4-7"})
            assert byte.status_code == 206 and byte.content == payload[4:8]
            assert net.head(file_path, headers=vip).status_code == 200
            ordinary = sign_url("https://vip.example.com/_n/edge-a", "/s/main/E01.mkv", SIGNING_KEY, 300,
                arg_digest="k", arg_expires="e", utag=user_tag("u2"), rate_bps=1000000)
            u = urlsplit(ordinary)
            assert net.get(u.path + "?" + u.query, headers=vip).status_code == 403
            assert net.get("/_n/unknown/s/main/E01.mkv", headers=vip).status_code == 404
            for alias in ("/_%6e/edge-a/s/main/E01.mkv", "/_n%2Fedge-a/s/main/E01.mkv", "/_n/edge-a/s/main/%2e%2e/E01.mkv"):
                assert net.get(alias, headers=vip).status_code == 404
            assert net.get("/System/Info?api_key=vip-token&Api_Key=normal-token", headers=vip).status_code == 403
            assert net.get("/System/Info", headers={**normal, "X-Mediadeck-Entry-Key": "forged"}).status_code == 403
            assert net.post(file_path, headers=vip).status_code == 405
            assert net.get("/Items/item42/Download", headers=vip).content == payload
            mobile = net.get("/Items/item-mobile/Download", headers=vip)
            assert mobile.status_code == 307 and urlsplit(mobile.headers["location"]).hostname == "emby.example.com"
            assert net.get("/Videos/item42/original.mkv", headers=normal).status_code == 403
            with httpx.Client(verify=tls_ctx, trust_env=False) as probe:
                assert probe.get(f"https://127.0.0.1:{origin}/System/Info", headers={"Host": "emby.example.com"}).status_code == 403
            # Isolated fault injection, never stop a production service.
            app.state.emby.personal_user_for_token = AsyncMock(side_effect=RuntimeError("fixture offline"))
            assert net.get("/System/Info", headers=vip).status_code == 500  # auth_request non-2xx fails closed
        assert any(role == "node" and method == "GET" for role, method, _ in records)
        assert ("emby", "GET", "/Items/item42/Download") in records
        assert ("gateway", "GET", "/Videos/item42/original.mkv") in records
        assert "vip-token" not in (tmp_path / "edge.log").read_text()
        assert "vip-token" not in (tmp_path / "origin.log").read_text()
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        for srv in servers:
            srv.shutdown()
            srv.server_close()
