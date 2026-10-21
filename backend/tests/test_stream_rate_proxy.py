"""Real loopback HTTP transfer tests; never contact the running node."""
from __future__ import annotations

import concurrent.futures
import http.client
import importlib.util
import pathlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

AGENT = pathlib.Path(__file__).resolve().parents[2] / "agent" / "stream_rate_proxy.py"
spec = importlib.util.spec_from_file_location("stream_rate_proxy", AGENT)
rate_proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rate_proxy)
MiB = 1024 * 1024  # payload size only; limits are decimal MB/s
RATE_15 = 15_000_000
RATE_35 = 35_000_000
RATE_CARD = 40_000_000
PAYLOAD = bytes(range(256)) * (24 * MiB // 256)


class Reader(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_HEAD(self):
        self.respond()

    def do_GET(self):
        self.respond()

    def respond(self):
        if not self.path.startswith("/video?"):
            self.send_error(404)
            return
        start, end, status = 0, len(PAYLOAD) - 1, 200
        if "Range" in self.headers:
            span = self.headers["Range"]
            assert span.startswith("bytes=")
            start, end = [int(n) for n in span[6:].split("-")]
            status = 206
        body = PAYLOAD[start:end + 1]
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "video/mp4")
        self.send_header("ETag", '"reader"')
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(PAYLOAD)}")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except ConnectionError:
                pass


@pytest.fixture
def relay():
    readers = [ThreadingHTTPServer(("127.0.0.1", 0), Reader) for _ in range(2)]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in readers]
    for t in threads:
        t.start()
    server = rate_proxy.RateServer(("127.0.0.1", 0),
                                   {f"/{i}/": s.server_port for i, s in enumerate(readers)})
    relay_thread = threading.Thread(target=server.serve_forever, daemon=True)
    relay_thread.start()
    yield server
    server.shutdown()
    server.server_close()
    for reader in readers:
        reader.shutdown()
        reader.server_close()
    relay_thread.join(timeout=3)
    for thread in threads:
        thread.join(timeout=3)


def fetch(relay, pool=0, *, rate=RATE_15, user="abcdef0123", span=None, method="GET"):
    headers = {"X-Mediadeck-Rate-Key": user, "X-Mediadeck-Rate": str(rate)}
    if span is not None:
        headers["Range"] = f"bytes={span[0]}-{span[1]}"
    conn = http.client.HTTPConnection("127.0.0.1", relay.server_port, timeout=15)
    start = time.monotonic()
    try:
        conn.request(method, f"/{pool}/video?expires=123&md5=signed", headers=headers)
        response = conn.getresponse()
        body = response.read()
        elapsed = time.monotonic() - start
        return response, body, elapsed
    finally:
        conn.close()


def test_head_range_and_reader_headers_preserved(relay):
    response, body, _ = fetch(relay, span=(100, 199), method="HEAD")
    assert response.status == 206 and body == b""
    assert response.getheader("Content-Range") == f"bytes 100-199/{len(PAYLOAD)}"
    response, body, _ = fetch(relay, span=(100, 199))
    assert response.status == 206 and body == PAYLOAD[100:200]
    assert response.getheader("Content-Type") == "video/mp4"
    assert response.getheader("Content-Length") == "100"
    conn = http.client.HTTPConnection("127.0.0.1", relay.server_port)
    conn.request("GET", "/0/video?e=x")
    assert conn.getresponse().status == 403  # missing verified identity: fail closed
    conn.close()


def test_single_stream_can_use_entire_budget_and_two_pools_share_it(relay):
    # 24 MiB payload at decimal 15 MB/s: one stream must NOT use 15/4.
    response, body, single = fetch(relay)
    assert response.status == 200 and body == PAYLOAD
    assert len(PAYLOAD) / RATE_15 * .98 <= single < 3.5, single
    # Both full-size transfers run simultaneously through different readers.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        before = time.monotonic()
        futures = [executor.submit(fetch, relay, pool=i) for i in range(2)]
        result = [f.result(timeout=12) for f in futures]
        wall = time.monotonic() - before
    assert all(r.status == 200 and data == PAYLOAD for r, data, _ in result)
    assert 2 * len(PAYLOAD) / RATE_15 * .98 <= wall < 7, wall  # two streams share 15 MB/s
    # Neither active transfer is starved until the other has finished.
    assert abs(result[0][2] - result[1][2]) < 1.25, result


def test_four_parallel_ranges_are_fair_not_four_times_the_limit(relay):
    width = 6 * MiB
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        start = time.monotonic()
        jobs = [executor.submit(fetch, relay, pool=i % 2,
                                span=(i * width, (i + 1) * width - 1))
                for i in range(4)]
        results = [job.result(timeout=10) for job in jobs]
        elapsed = time.monotonic() - start
    assert all(r.status == 206 and body == PAYLOAD[i * width:(i + 1) * width]
               for i, (r, body, _) in enumerate(results))
    assert len(PAYLOAD) / RATE_15 * .98 <= elapsed < 3.5, elapsed  # four streams share 15 MB/s
    assert max(r[2] for r in results) - min(r[2] for r in results) < 1, results


def test_whitelist_35_and_independent_accounts(relay):
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        start = time.monotonic()
        tasks = [executor.submit(fetch, relay, pool=i, rate=RATE_35) for i in range(2)]
        result = [task.result(timeout=10) for task in tasks]
        elapsed = time.monotonic() - start
    assert all(r.status == 200 and body == PAYLOAD for r, body, _ in result)
    assert 2 * len(PAYLOAD) / RATE_35 * .98 <= elapsed < 3.0, elapsed  # 35 MB/s
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        start = time.monotonic()
        tasks = [executor.submit(fetch, relay, pool=i, user=f"abcdef012{i}") for i in range(2)]
        assert all(task.result(timeout=10)[0].status == 200 for task in tasks)
        elapsed = time.monotonic() - start
    assert elapsed < 2.9, elapsed  # different accounts do not share a budget


def test_bandwidth_card_40_mb_signed_rate_is_shared_by_two_connections(relay):
    # A card increases the effective signed entitlement; it does not give
    # each socket a separate 40 MB/s budget or hit a 35 MB/s absolute ceiling.
    assert rate_proxy.parse_rate(str(RATE_CARD)) == RATE_CARD
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        start = time.monotonic()
        transfers = [executor.submit(fetch, relay, pool=i, rate=RATE_CARD) for i in range(2)]
        results = [task.result(timeout=10) for task in transfers]
        elapsed = time.monotonic() - start
    assert all(reply.status == 200 and body == PAYLOAD for reply, body, _ in results)
    assert 2 * len(PAYLOAD) / RATE_CARD * .98 <= elapsed < 2, elapsed
    assert abs(results[0][2] - results[1][2]) < .7, results


def test_mixed_old_35_and_new_15_links_share_stricter_live_budget(relay):
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        start = time.monotonic()
        old = executor.submit(fetch, relay, pool=0, rate=RATE_35)
        new = executor.submit(fetch, relay, pool=1, rate=RATE_15)
        assert old.result(timeout=12)[1] == PAYLOAD
        assert new.result(timeout=12)[1] == PAYLOAD
        elapsed = time.monotonic() - start
    assert 2 * len(PAYLOAD) / RATE_15 * .95 <= elapsed < 7  # active minimum is 15 MB/s


def test_four_mixed_large_ranges_outlast_upstream_socket_timeout_without_disconnect(relay, monkeypatch):
    """A 50ms upstream *idle* timeout is not a 50ms response lifetime.

    Scale down the production 35s socket timeout: four concurrent, differently
    signed large Ranges spend >1s being paced, but no individual read stalls.
    The client side uses the unpatched HTTPConnection with its own 10s timeout.
    """
    original = http.client.HTTPConnection
    configured = []

    class ShortIdleTimeoutConnection(original):
        def __init__(self, host, port=None, timeout=35, **kwargs):
            configured.append(timeout)
            super().__init__(host, port, timeout=0.05, **kwargs)

    monkeypatch.setattr(http.client, "HTTPConnection", ShortIdleTimeoutConnection)

    def transfer(index):
        width = 6 * MiB
        headers = {"Range": f"bytes={index * width}-{(index + 1) * width - 1}",
                   "X-Mediadeck-Rate-Key": "abcdef0123",
                   "X-Mediadeck-Rate": str(RATE_15 if index % 2 else RATE_35)}
        connection = original("127.0.0.1", relay.server_port, timeout=10)
        try:
            connection.request("GET", f"/{index % 2}/video?expires=1", headers=headers)
            reply = connection.getresponse()
            return reply.status, reply.read()
        finally:
            connection.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        before = time.monotonic()
        result = list(executor.map(transfer, range(4)))
        elapsed = time.monotonic() - before
    assert configured == [35] * 4  # relay really used the short-idle-timeout subclass
    assert all(status == 206 and data == PAYLOAD[i * 6 * MiB:(i + 1) * 6 * MiB]
               for i, (status, data) in enumerate(result))
    assert len(PAYLOAD) / RATE_15 * .95 <= elapsed < 5, elapsed
    assert elapsed > 20 * 0.05  # wall time massively exceeds the upstream idle timeout


def test_public_location_example_preserves_access_safety_without_metering():
    snippet = (pathlib.Path(__file__).resolve().parents[2] /
               "deploy/nginx/stream-rate-locations.example.conf").read_text()
    for preserved in ("auth_request /__VERIFY_ENDPOINT__;", "limit_conn mediadeck_peruser 4;",
                      "include __ACCESS_POLICY_LOCATION_INCLUDE__;", "proxy_force_ranges on;",
                      "access_log off;"):
        assert snippet.count(preserved) == 2, preserved
    for retired in ('mirror /__ANNOUNCE_ENDPOINT__;', '__SPEED_LOG_PATH__', 'mediadeck_speed;'):
        assert retired not in snippet
    assert "limit_rate $mediadeck_rate;" not in snippet
    for index in (0, 1):
        assert snippet.count(f"proxy_pass http://__LOOPBACK_HOST__:__RELAY_PORT__/{index}/;") == 1
    assert snippet.count("proxy_set_header X-Mediadeck-Rate-Key $mediadeck_user_key;") == 2
    variables = (pathlib.Path(__file__).resolve().parents[2] /
                 "deploy/nginx/stream-rate-vars.example.conf").read_text()
    assert '""      15000000;' in variables and '"0"     15000000;' in variables


@pytest.mark.skipif(__import__("shutil").which("nginx") is None, reason="nginx unavailable")
def test_staged_nginx_real_proxy_preserves_ranges_and_overwrites_identity(relay, tmp_path):
    """Actual isolated nginx worker; never touches the running nginx config."""
    import shutil
    import socket
    import subprocess

    from app.core.config import NodePool, StreamNode
    from app.modules.provisioning import nginx_site

    node = StreamNode(name="rate-test", base_url="https://localhost", sign_secret="test",
                      probe_url="http://127.0.0.1:9800/load",
                      pools=[NodePool(name="main", url_prefix="/s/main",
                                      node_path="/tmp/media", emby_prefix="/media")])
    generated = nginx_site(node)
    begin = generated.index('    location "/s/main/" {')
    end = generated.index('    location = /_mediadeck/verify {', begin)
    location = generated[begin:end]
    location = location.replace("127.0.0.1:9820", f"127.0.0.1:{relay.server_port}")
    location = location.replace("access_log /var/log/nginx/mediadeck-speed.log mediadeck_speed;",
                                "access_log off;")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = tmp_path / "nginx.conf"
    config.write_text(f'''pid {tmp_path}/nginx.pid;
error_log {tmp_path}/error.log;
events {{}}
http {{
    map $arg_r $mediadeck_rate {{ "" 15000000; "0" 15000000; default $arg_r; }}
    map $arg_u $mediadeck_user_key {{ "" $remote_addr; default $arg_u; }}
    map $arg_u $md_denied {{ default 0; }}
    limit_conn_zone $mediadeck_user_key zone=mediadeck_peruser:1m;
    server {{
        listen 127.0.0.1:{port};
        {location}
        location = /_mediadeck/verify {{ internal; return 204; }}
        location = /_mediadeck/announce {{ internal; return 204; }}
    }}
}}
''')
    command = [shutil.which("nginx"), "-p", str(tmp_path), "-c", str(config)]
    checked = subprocess.run([*command, "-t"], capture_output=True, text=True,
                             timeout=5, check=False)
    assert checked.returncode == 0, checked.stderr
    process = subprocess.Popen([*command, "-g", "daemon off;"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.04)
        else:
            pytest.fail(f"staged nginx did not start: {(tmp_path / 'error.log').read_text()}")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/s/main/video?r=15000000&u=abcdef0123",
                     headers={"Range": "bytes=100-199", "X-Mediadeck-Rate-Key": "spoofed"})
        response = conn.getresponse()
        assert response.status == 206 and response.read() == PAYLOAD[100:200]
        assert response.getheader("Content-Range") == f"bytes 100-199/{len(PAYLOAD)}"
        conn.close()
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.skipif(__import__("shutil").which("rclone") is None, reason="rclone unavailable")
def test_real_rclone_reader_head_get_and_partial_file(tmp_path):
    """Disk bytes through real rclone serve http and relay; no FUSE/live mount."""
    import shutil
    import socket
    import subprocess

    (tmp_path / "video").write_bytes(PAYLOAD[:2 * MiB])
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([shutil.which("rclone"), "serve", "http", str(tmp_path),
                                "--addr", f"127.0.0.1:{port}", "--read-only",
                                "--vfs-cache-mode", "off", "--log-level", "ERROR"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("local rclone reader failed to start")
        server = rate_proxy.RateServer(("127.0.0.1", 0), {"/0/": port})
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for method in ("HEAD", "GET"):
                reply, data, _ = fetch(server, span=(100, 199), method=method)
                # rclone itself ignores Range on HEAD and returns 200/full
                # Content-Length; the relay must preserve that exact result.
                if method == "HEAD":
                    assert reply.status == 200 and reply.getheader("Content-Length") == str(2 * MiB)
                    assert data == b""
                else:
                    assert reply.status == 206
                    assert reply.getheader("Content-Range") == "bytes 100-199/2097152"
                    assert data == PAYLOAD[100:200]
            reply, data, _ = fetch(server, method="GET")
            assert reply.status == 200 and data == PAYLOAD[:2 * MiB]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_signed_zero_fallback_and_mixed_entitlements():
    assert rate_proxy.DEFAULT_RATE == RATE_15
    assert rate_proxy.parse_rate("0") == RATE_15
    assert rate_proxy.parse_rate(str(RATE_CARD)) == RATE_CARD
    assert rate_proxy.parse_rate("80000000") == 80_000_000
    limiter = rate_proxy.AggregateLimiter()
    first = limiter.join("same", RATE_35)
    second = limiter.join("same", RATE_15)
    assert limiter.quantum("same") == rate_proxy.CHUNK
    assert min(limiter.members["same"]["rates"].values()) == RATE_15
    limiter.leave("same", first)
    limiter.leave("same", second)
    assert not limiter.members
