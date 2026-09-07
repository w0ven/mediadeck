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
from proxy_runtime import NGINX_AVAILABLE, nginx_command, stop_proxy
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


@pytest.fixture(params=["caddy", pytest.param("nginx", marks=pytest.mark.skipif(
    not NGINX_AVAILABLE, reason="nginx binary or MEDIADECK_NGINX_DOCKER is needed"))])
def proxy(client, tmp_path, request):  # noqa: F811 - imported pytest fixture
    engine = request.param
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
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append({"path": self.path, "headers": dict(self.headers),
                             "method": self.command, "body": body})
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

        do_POST = do_DELETE = do_PUT = do_PATCH = do_OPTIONS = do_GET

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
        response = client.get(f"/api/integration/frontend?server={engine}&entry={entry}", auth=ADMIN)
        assert response.status_code == 200
        config_parts.append(response.json()["config"].replace(
            "tls_server_name ", f"tls_trusted_ca_certs {cert}\n                tls_server_name "))
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("\n".join(config_parts) if engine == "caddy" else client.get(
        "/api/integration/frontend?entry=friend-a", auth=ADMIN).json()["config"])
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
    command = ["caddy", "run", "--config", str(config_file)]
    if engine == "nginx":
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        for index, text in enumerate(config_parts):
            for host in ("emby", "edge-a", "edge-b"):
                text = text.replace(f"https://{host}.example.com",
                                    f"https://127.0.0.1:{upstream.server_port}")
                text = text.replace(f"server {host}.example.com:443;",
                                    f"server 127.0.0.1:{upstream.server_port};")
            text = text.replace("listen 443 ssl;", f"listen 127.0.0.1:{port};")
            text = text.replace("listen [::]:443 ssl;", "")
            text = text.replace("http2 on;", "")
            text = text.replace("/etc/ssl/certs/ca-certificates.crt", str(cert))
            for entry in ("friend-a", "friend-b"):
                text = text.replace(f"/etc/letsencrypt/live/{entry}.example.com/fullchain.pem",
                                    str(cert))
                text = text.replace(f"/etc/letsencrypt/live/{entry}.example.com/privkey.pem",
                                    str(key))
            (conf_d / f"friend-{index}.conf").write_text(text)
        config_file = tmp_path / "nginx.conf"
        config_file.write_text(f"""pid {tmp_path}/nginx.pid;
error_log {tmp_path}/nginx-error.log error;
events {{}}
http {{
    access_log off;
    include {conf_d}/*.conf;
}}
""")
        checked = subprocess.run(nginx_command(tmp_path, "-t", "-c", str(config_file)),
                                 capture_output=True, timeout=20, check=False)
        assert checked.returncode == 0, "generated conf.d files must pass nginx -t"
        command = nginx_command(tmp_path, "-c", str(config_file), "-g", "daemon off;")
    with (tmp_path / "caddy.log").open("wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
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
            stop_proxy(process, command)


@pytest.mark.parametrize("path", [
    "/s/main/%E6%B5%8B%E8%AF%95%20%3f%23%25%2b.mkv",
    "/s/main/a//b.mkv", "/s/main/%61%2fb.mkv", "/s/main/a%252Fb.mkv",
    pytest.param("/s/main/" + "x" * 12000 + ".mkv", id="12k-path"),
])
def test_raw_node_path_and_query(proxy, path):
    http, requests, _, _ = proxy
    query = "k=a%2Bb&empty=&repeat=1&repeat=2&target=https://evil.example.com/"
    http.get("/_n/edge-a" + path + "?" + query,
             headers={"Host": "friend-a.example.com"})
    assert requests[-1]["path"] == path + "?" + query


@pytest.mark.parametrize("path", [
    "/_n", "/_n/", "/_n/unknown/s/a", "/_n/edge-a/s/..%2f..%2fapi",
    "/_n/unknown/..%2f..%2fapi", "/_n/edge-a/s/%2e%2e/api",
    "/_n/edge-a/s/main/%2E.%2Fapi", "/_%6e/edge-a/s/main/demo.mkv",
    "/_n%2funknown%2f..%2f..%2fapi", "/%2f_n/unknown/s/demo.mkv",
    "/%5f%6e/unknown/..%2F..%2Fapi",
])
def test_reserved_routes_never_escape_to_emby(proxy, path):
    http, requests, _, _ = proxy
    before = len(requests)
    result = http.get(path, headers={"Host": "friend-a.example.com"})
    assert result.status_code == 404
    assert len(requests) == before


@pytest.mark.parametrize("method", ["POST", "DELETE", "PUT", "PATCH", "OPTIONS"])
def test_friend_preserves_non_get_methods(proxy, method):
    http, requests, _, _ = proxy
    body = b'{"value":"example"}'
    path = "/Items/item/Subtitles?tag=a%2Bb&tag=2"
    result = http.request(method, path, content=body, headers={
        "Host": "friend-a.example.com", "Authorization": "Bearer synthetic",
        "Cookie": "session=synthetic", "X-Emby-Token": "synthetic"})
    assert result.status_code == 200
    assert requests[-1]["method"] == method
    assert requests[-1]["body"] == body
    assert requests[-1]["path"] == path
    assert requests[-1]["headers"][KEY_HEADER] == entry_headers()[KEY_HEADER]
    assert requests[-1]["headers"]["Authorization"] == "Bearer synthetic"
    assert requests[-1]["headers"]["Cookie"] == "session=synthetic"
    assert requests[-1]["headers"]["X-Emby-Token"] == "synthetic"
    path = "/_n/edge-a/s/main/demo.mkv?k=invalid&target=https://evil.example.com/"
    result = http.request(method, path, content=body, headers={
        "Host": "friend-a.example.com", "Authorization": "Bearer synthetic",
        "Cookie": "session=synthetic", "X-Emby-Token": "synthetic",
        "X-MediaBrowser-Token": "synthetic", "X-Emby-Authorization": "synthetic",
        "X-Mediadeck-Entry": "forged", KEY_HEADER: "forged",
        "X-Forwarded-Host": "evil.example.com", "Range": "bytes=2-5",
        "If-Range": '"example-etag"'})
    assert result.status_code == 403
    hit = requests[-1]
    assert hit["path"] == path.removeprefix("/_n/edge-a")
    assert hit["method"] == method and hit["body"] == body
    headers = {k.lower(): v for k, v in hit["headers"].items()}
    assert headers["host"] == "edge-a.example.com"
    assert headers["range"] == "bytes=2-5" and headers["if-range"] == '"example-etag"'
    assert not ({"authorization", "cookie", "x-emby-token", "x-mediabrowser-token",
                 "x-emby-authorization", "x-mediadeck-entry", KEY_HEADER.lower()} & headers.keys())


def test_proxy_request_line_limits_do_not_truncate_paths(proxy, request):
    http, requests, _, _ = proxy
    path = "/_n/edge-a/s/main/" + "x" * 40000
    result = http.get(path, headers={"Host": "friend-a.example.com"})
    if request.node.callspec.params["proxy"] == "nginx":
        assert result.status_code == 414 and not requests
    else:
        # Caddy's default header budget is larger. It forwards the full path;
        # the mock node correctly refuses its missing signature.
        assert result.status_code == 403
        assert requests[-1]["path"] == path.removeprefix("/_n/edge-a")


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
