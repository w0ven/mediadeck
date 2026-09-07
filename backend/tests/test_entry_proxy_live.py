"""Real Caddy -> TLS mock Emby/panel -> signed TLS edge, on loopback only.

Skipped when Caddy/OpenSSL are absent. No production endpoint or credential is
used. These tests complement, and do not claim, a friend's deployment proof.
"""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
import socket
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest
from test_external_entries import (
    ADMIN,
    KEY_HEADER,
    SIGNING_KEY,
    client,  # noqa: F401 - pytest fixture
    entry_headers,
)

from app.main import app
from app.modules.signing import verify

pytestmark = pytest.mark.skipif(
    not shutil.which("caddy") or not shutil.which("openssl"),
    reason="Caddy and OpenSSL are needed for the loopback proxy test",
)


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def test_reference_and_origin_caddyfiles_parse(client, tmp_path):  # noqa: F811 - pytest fixture
    reference = Path(__file__).resolve().parents[2] / "docs/examples/friend.Caddyfile"
    origin = client.get("/api/integration/frontend?server=caddy", auth=ADMIN).json()["config"]
    for index, content in enumerate((reference.read_text(), origin)):
        path = tmp_path / f"example-{index}.Caddyfile"
        path.write_text(content)
        result = subprocess.run(["caddy", "adapt", "--config", str(path), "--adapter", "caddyfile"],
                                capture_output=True, check=True, timeout=10)
        assert json.loads(result.stdout)["apps"]["http"]["servers"]


@pytest.fixture
def proxy(client, tmp_path, request):  # noqa: F811 - imported pytest fixture
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-keyout", str(key), "-out", str(cert), "-subj", "/CN=emby.example.com",
        "-addext", "subjectAltName=DNS:emby.example.com,DNS:edge-a.example.com,DNS:edge-b.example.com",
    ], check=True, capture_output=True, timeout=15)
    requests, server_names = [], []
    payload = b"0123456789abcdef"

    class Upstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def send(self, status, body=b"", headers=None):
            self.send_response(status)
            for name, value in (headers or {}).items():
                if name.lower() not in {"content-length", "connection", "transfer-encoding"}:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            requests.append({"path": self.path, "headers": dict(self.headers)})
            if self.path == "/socket":
                accept = base64.b64encode(hashlib.sha1(
                    (self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
                    .encode()).digest()).decode()
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                opcode, size = self.rfile.read(2)
                mask = self.rfile.read(4)
                data = self.rfile.read(size & 127)
                decoded = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
                self.wfile.write(bytes([opcode, len(decoded)]) + decoded)
                self.wfile.flush()
                self.close_connection = True
                return
            if self.path.startswith("/s/"):
                parsed = urlsplit(self.path)
                args = parse_qs(parsed.query)
                valid = verify(unquote(parsed.path), args.get("k", [""])[0],
                               int(args.get("e", ["0"])[0]), SIGNING_KEY,
                               rate_bps=int(args.get("r", ["0"])[0]),
                               utag=args.get("u", [""])[0])
                if not valid:
                    self.send(403)
                elif self.headers.get("Range") == "bytes=2-5":
                    self.send(206, payload[2:6], {"Content-Range": "bytes 2-5/16"})
                elif self.headers.get("Range") == "bytes=-4":
                    self.send(206, payload[-4:], {"Content-Range": "bytes 12-15/16"})
                else:
                    self.send(200, payload)
            elif "/videos/" in self.path.lower():
                response = client.request(self.command, self.path, headers=dict(self.headers),
                                          follow_redirects=False)
                self.send(response.status_code, response.content, dict(response.headers))
            elif self.path.startswith("/auth"):
                self.send(401)
            else:
                self.send(200, b"Emby passthrough")

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    context.set_servername_callback(lambda sock, name, ctx: server_names.append(name))
    upstream.socket = context.wrap_socket(upstream.socket, server_side=True)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()

    def close_upstream():
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    request.addfinalizer(close_upstream)
    port = _port()
    config_parts = []
    for entry in ("friend-a", "friend-b"):
        response = client.get(f"/api/integration/frontend?entry={entry}", auth=ADMIN)
        assert response.status_code == 200
        config_parts.append(response.json()["config"].replace(
            "tls_server_name ", f"tls_trusted_ca_certs {cert}\n                tls_server_name "))
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("\n".join(config_parts))
    adapted = subprocess.run(["caddy", "adapt", "--config", str(caddyfile), "--adapter", "caddyfile"],
                             check=True, capture_output=True, timeout=15)
    config = json.loads(adapted.stdout)
    # Keep every generated route, Host and verified TLS SNI. Only replace the
    # listening/dial sockets, and trust this test's private certificate.
    config["admin"] = {"disabled": True}
    for server in config["apps"]["http"]["servers"].values():
        server["listen"] = [f"127.0.0.1:{port}"]
        server["automatic_https"] = {"disable": True}
        server.pop("tls_connection_policies", None)
    for handler in _walk(config):
        if handler.get("handler") == "reverse_proxy":
            for target in handler["upstreams"]:
                target["dial"] = f"127.0.0.1:{upstream.server_port}"
    config_file = tmp_path / "caddy.json"
    config_file.write_text(json.dumps(config))
    with (tmp_path / "caddy.log").open("wb") as log:
        process = subprocess.Popen(["caddy", "run", "--config", str(config_file)],
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                assert process.poll() is None, "loopback Caddy exited during startup"
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                pytest.fail("loopback Caddy did not listen within eight seconds")
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                              follow_redirects=False, timeout=5) as http:
                yield http, requests, server_names, port
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_caddy_real_redirect_range_query_sni_and_auth(proxy, monkeypatch):
    http, requests, names, _ = proxy
    # Include spaces, a literal percent and URL delimiters in the *filename*.
    async def media_paths(item_id):
        return {"source": "/media/Movies/Demo/测试 % # + &.mkv"}
    monkeypatch.setattr(app.state.emby, "item_media_paths", media_paths)
    for entry in ("friend-a", "friend-b", "friend-a"):
        headers = {"Host": f"{entry}.example.com", "X-Emby-Token": "client-emby-token",
                   "X-Mediadeck-Entry": "evil", KEY_HEADER: "forged",
                   "X-Forwarded-Host": "evil.example.com"}
        response = http.get("/emby/videos/item42/original.mkv?MediaSourceId=source", headers=headers)
        assert response.status_code == 302
        signed = urlsplit(response.headers["location"])
        assert signed.netloc == f"{entry}.example.com"
        assert "no-store" in response.headers["cache-control"]
        target = signed.path + "?" + signed.query
        byte_headers = {"Host": signed.netloc, "Range": "bytes=2-5",
                        "Authorization": "Bearer synthetic", "X-Emby-Token": "client-emby-token",
                        **entry_headers(entry)}
        result = http.get(target, headers=byte_headers)
        assert result.status_code == 206 and result.content == b"2345"
        assert result.headers["content-range"] == "bytes 2-5/16"
        edge_request = requests[-1]
        assert edge_request["path"].split("?", 1)[1] == signed.query
        edge_headers = {k.lower(): v for k, v in edge_request["headers"].items()}
        assert edge_headers["host"] in {"edge-a.example.com", "edge-b.example.com"}
        assert "authorization" not in edge_headers and "x-emby-token" not in edge_headers
        assert KEY_HEADER.lower() not in edge_headers
        suffix = http.get(target, headers={**byte_headers, "Range": "bytes=-4"})
        assert suffix.status_code == 206 and suffix.content == b"cdef"
        assert http.head(target, headers=byte_headers).status_code == 206
        assert http.get(target.replace("r=0", "r=99"), headers=byte_headers).status_code == 403
    assert "emby.example.com" in names
    assert {"edge-a.example.com", "edge-b.example.com"} & set(names)
    auth_start = len(requests)
    for _ in range(2):
        result = http.get("/auth", headers={"Host": "friend-a.example.com"})
        assert result.status_code == 401 and "no-store" in result.headers["cache-control"]
    assert len(requests) == auth_start + 2
    before = len(requests)
    for path in ("/_n/unknown/s/a", "/_n/edge-a/_mediadeck/load", "/_n/https://evil.example.com/s/a"):
        assert http.get(path, headers={"Host": "friend-a.example.com"}).status_code == 404
    assert len(requests) == before
    assert http.get("/web/index.html", headers={"Host": "friend-a.example.com"}).content == b"Emby passthrough"


def test_caddy_websocket_upgrade_and_frame(proxy):
    _, _, _, port = proxy
    with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
        sock.sendall(b"GET /socket HTTP/1.1\r\nHost: friend-a.example.com\r\n"
                     b"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                     b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n")
        stream = sock.makefile("rb")
        assert stream.readline().startswith(b"HTTP/1.1 101")
        while stream.readline() != b"\r\n":
            pass
        mask, message = b"test", b"hello"
        frame = bytes([0x81, 0x80 | len(message)]) + mask
        frame += bytes(c ^ mask[i % 4] for i, c in enumerate(message))
        sock.sendall(frame)
        assert stream.read(2) == bytes([0x81, len(message)])
        assert stream.read(len(message)) == message
        stream.close()
