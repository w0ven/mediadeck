from __future__ import annotations

import errno
import http.client
import importlib.util
import os
import threading
from pathlib import Path

import pytest

from app.main import _redact_playback_paths

AGENT = Path(__file__).resolve().parents[2] / "agent" / "union_file_reader.py"
SPEC = importlib.util.spec_from_file_location("union_file_reader", AGENT)
assert SPEC and SPEC.loader
ufr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ufr)


def test_virtual_path_redaction_keeps_chaichai_and_emby_theater_compatible(monkeypatch):
    """Regression test for chaichai AndroidTV (Kotlin non-null Path) & Emby Theater Windows."""
    raw = {
        "PlaySessionId": "ps-1",
        "MediaSources": [
            {
                "Id": "mediasource_22634",
                "Name": "S01E10 - 第 10 集",
                "Path": "/media/Anime/TV/遮天 (2023)/Season 1/遮天 - S01E10 - 金色苦海.mp4",
                "Protocol": "File",
                "Container": "mov,mp4,m4a",
                "SupportsDirectPlay": True,
                "DirectStreamUrl": None,
                "MediaStreams": [
                    {"Index": 0, "Type": "Video", "Codec": "hevc", "Path": None},
                    {
                        "Index": 3,
                        "Type": "Subtitle",
                        "IsExternal": True,
                        "Codec": "subrip",
                        "Path": "/config/cmcc-batches/44/sub/遮天 - S01E10 - 金色苦海.AI尊享翻译.zh.srt",
                        "DeliveryUrl": "/Videos/22634/mediasource_22634/Subtitles/3/Stream.srt",
                    },
                ],
            },
            # Duplicate basename across editions must disambiguate directory while keeping stem
            {
                "Id": "cmcc_edition_dup",
                "Name": "S01E10 - 移动版",
                "Path": "/config/cmcc-files/36/遮天 (2023)/Season 01/遮天 - S01E10 - 金色苦海.mp4",
                "Protocol": "File",
                "Container": "mp4",
                "MediaStreams": [
                    {
                        "Index": 2,
                        "Type": "Subtitle",
                        "IsExternal": True,
                        "Codec": "subrip",
                        "Path": "https://internal.invalid/sub?token=sub_leak",
                    }
                ],
            },
            {
                "Id": "url-fallback",
                "Name": "星际穿越 4K",
                "Path": "https://emby.example/cmcc-source/file_1?cap=secret",
                "Protocol": "Http",
                "Container": "mkv,webm",
                "MediaStreams": [],
            },
            {
                "Id": "http-null-path",
                "Name": "Live #1: Channel",
                "Path": None,
                "Protocol": "Http",
                "Container": "ts",
            },
            {
                "Id": "win-colon-file",
                "Name": "Mission Impossible",
                "Path": "/media/Movies/Season#1/Mission: Impossible #1.mkv",
                "Protocol": "File",
                "Container": "mkv",
            },
            {
                "Id": "fallback-url-or-path-name",
                "Name": "https://secret.invalid/x?token=leak2",
                "Path": None,
                "Protocol": "Http",
                "Container": "mp4",
            },
        ],
    }
    redacted = _redact_playback_paths(raw)
    s0, s1, s2, s3, s4, s5 = redacted["MediaSources"]
    assert s0["Path"] == "/stream/遮天 - S01E10 - 金色苦海.mp4"
    assert "Path" not in s0["MediaStreams"][0]
    assert s0["MediaStreams"][1]["Path"] == "/stream/遮天 - S01E10 - 金色苦海.AI尊享翻译.zh.srt"

    assert s1["Path"] == "/stream_2/遮天 - S01E10 - 金色苦海.mp4"
    assert s1["MediaStreams"][0]["Path"] == "/stream_2/subtitle.srt"

    assert s2["Path"] == "/stream/星际穿越 4K.mkv"
    assert s3["Path"] == "/stream/Live _1_ Channel.ts"
    assert s4["Path"] == "/stream/Mission_ Impossible _1.mkv"
    assert s5["Path"] == "/stream/media.mp4"
    all_paths = [s["Path"] for s in redacted["MediaSources"]]
    assert all("Season" not in p and "leak2" not in p for p in all_paths)

    # Original input dictionary must remain unmutated
    assert raw["MediaSources"][0]["Path"].startswith("/media/")
    assert raw["MediaSources"][0]["MediaStreams"][0]["Path"] is None

    dumped = repr(redacted)
    for forbidden in ("/media/", "/config/", "cmcc-", "cap=secret", "token=sub_leak"):
        assert forbidden not in dumped

    monkeypatch.setenv("MEDIADECK_REDACT_PLAYBACK_PATHS", "0")
    assert _redact_playback_paths(raw)["MediaSources"][0]["Path"].startswith("/media/")


@pytest.fixture
def union_env(tmp_path):
    r1 = tmp_path / "gdrive"
    r2 = tmp_path / "gdrive3"
    r3 = tmp_path / "gdrive4"
    for d in (r1, r2, r3):
        d.mkdir()
    (r1 / "_health").mkdir()
    (r1 / "_health" / "probe.bin").write_bytes(b"HEALTH_OK_123456")
    (r1 / "Movies").mkdir()
    (r1 / "Movies" / "shared.mkv").write_bytes(b"FROM_PRIMARY_MOUNT_0123456789")
    (r2 / "Movies").mkdir()
    (r2 / "Movies" / "shared.mkv").write_bytes(b"FROM_SECONDARY_SHOULD_NOT_WIN")
    (r2 / "DuanjuStudio" / "hongguo" / "76999").mkdir(parents=True)
    (r2 / "DuanjuStudio" / "hongguo" / "76999" / "ep1.mp4").write_bytes(b"SHORT_DRAMA_PAYLOAD")

    raw_gd4 = r3 / "_gd4_raw_movie" / "2026"
    raw_gd4.mkdir(parents=True)
    target = raw_gd4 / "film.mkv"
    target.write_bytes(b"GD4_SYMLINKED_MOVIE_BYTES")
    gd4_dir = r3 / "GD4Movies" / "44"
    gd4_dir.mkdir(parents=True)
    (gd4_dir / "film.mkv").symlink_to(target)

    outside = tmp_path / "outside_secret.txt"
    outside.write_bytes(b"TOP_SECRET")
    (r1 / "escape.txt").symlink_to(outside)
    (gd4_dir / "escape_gd4.txt").symlink_to(outside)

    server = ufr.UnionServer(
        ("127.0.0.1", 0),
        [(str(r1), False), (str(r2), False), (str(r3), True)],
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, r1, r2, r3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _req(port: int, method: str, path: str, headers: dict[str, str] | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, dict(resp.getheaders()), body
    finally:
        conn.close()


def test_sequential_first_found_short_circuits_both_secondary_and_tertiary_mounts(
    union_env, monkeypatch
):
    server, r1, _r2, _r3 = union_env
    port = server.server_port
    touched_roots: list[str] = []
    real_strict = ufr.open_strict_beneath_path

    def spy_strict(root_path: str, relative: str) -> int:
        touched_roots.append(root_path)
        if root_path != str(r1):
            raise AssertionError(f"secondary mount {root_path} must not be touched on primary hit")
        return real_strict(root_path, relative)

    def explode_symlink(*_a, **_kw):
        raise AssertionError("symlink mount must not be touched on primary hit")

    monkeypatch.setattr(ufr, "open_strict_beneath_path", spy_strict)
    monkeypatch.setattr(ufr, "open_contained_symlinks", explode_symlink)

    status, hdrs, body = _req(port, "GET", "/_health/probe.bin")
    assert status == 200
    assert body == b"HEALTH_OK_123456"
    assert hdrs.get("Accept-Ranges") == "bytes"
    assert touched_roots == [str(r1)]

    touched_roots.clear()
    status, _hdrs, body = _req(port, "GET", "/Movies/shared.mkv")
    assert status == 200
    assert body == b"FROM_PRIMARY_MOUNT_0123456789"
    assert touched_roots == [str(r1)]


def test_secondary_and_contained_symlink_mounts_ranges_and_rfc9110_edge_cases(union_env):
    server, _r1, r2, _r3 = union_env
    port = server.server_port

    # Secondary strict mount with Range & HEAD
    status, hdrs, body = _req(
        port, "GET", "/DuanjuStudio/hongguo/76999/ep1.mp4", {"Range": "bytes=6-10"}
    )
    assert status == 206
    assert body == b"DRAMA"
    assert hdrs.get("Content-Range") == "bytes 6-10/19"
    assert hdrs.get("Content-Length") == "5"

    status, hdrs, body = _req(
        port, "HEAD", "/DuanjuStudio/hongguo/76999/ep1.mp4", {"Range": "bytes=0-3"}
    )
    assert status == 200
    assert body == b""
    assert hdrs.get("Content-Length") == "19"

    # Contained symlink inside r3 (GD4Movies -> _gd4_raw_movie)
    status, hdrs, body = _req(port, "GET", "/GD4Movies/44/film.mkv", {"Range": "bytes=-5"})
    assert status == 206
    assert body == b"BYTES"
    assert hdrs.get("Content-Range") == "bytes 20-24/25"

    # Unknown range unit & long multi-range (>128 chars) fall back to 200 per RFC 9110
    status, _hdrs, body = _req(
        port, "GET", "/GD4Movies/44/film.mkv", {"Range": "items=0-5"}
    )
    assert status == 200 and len(body) == 25
    long_multi = "bytes=" + ", ".join(f"{i}-{i}" for i in range(25))
    assert len(long_multi) > 128
    status, _hdrs, body = _req(
        port, "GET", "/GD4Movies/44/film.mkv", {"Range": long_multi}
    )
    assert status == 200 and len(body) == 25

    # Unsatisfiable range -> 416
    status, hdrs, _body = _req(port, "GET", "/GD4Movies/44/film.mkv", {"Range": "bytes=999-1000"})
    assert status == 416
    assert hdrs.get("Content-Range") == "bytes */25"

    # >4 GiB 64-bit sparse file offset read
    big = r2 / "big_4k_remux.mkv"
    with open(big, "wb") as f:
        f.seek(5_000_000_000)
        f.write(b"HIGH_OFFSET_64B!")
    status, hdrs, body = _req(
        port, "GET", "/big_4k_remux.mkv", {"Range": "bytes=5000000000-5000000015"}
    )
    assert status == 206
    assert body == b"HIGH_OFFSET_64B!"
    assert hdrs.get("Content-Range") == "bytes 5000000000-5000000015/5000000016"


def test_remount_survival_storage_fault_isolation_and_inflight_cap(union_env, monkeypatch):
    server, _r1, r2, _r3 = union_env
    port = server.server_port

    # 1. Replacing mount directory inode (simulating FUSE unmount + remount) works seamlessly
    old_r2 = r2.with_name("gdrive3_old")
    r2.rename(old_r2)
    r2.mkdir()
    (r2 / "remounted.mp4").write_bytes(b"NEW_MOUNT_DATA")
    status, _hdrs, body = _req(port, "GET", "/remounted.mp4")
    assert status == 200 and body == b"NEW_MOUNT_DATA"

    # 2. Storage EIO on r2 does NOT block r3 (GD4Movies), and returns 503 (not 404) when missing
    real_strict = ufr.open_strict_beneath_path

    def faulty_r2(root_path: str, relative: str) -> int:
        if root_path == str(r2):
            raise OSError(errno.EIO, "FUSE transport endpoint error")
        return real_strict(root_path, relative)

    monkeypatch.setattr(ufr, "open_strict_beneath_path", faulty_r2)
    status, _hdrs, body = _req(port, "GET", "/GD4Movies/44/film.mkv")
    assert status == 200 and body == b"GD4_SYMLINKED_MOVIE_BYTES"

    status, _hdrs, _body = _req(port, "GET", "/nonexistent_during_r2_fault.mkv")
    assert status == 503

    # 3. Pre-thread inflight gate rejects excess connections before spawning worker threads
    capped = ufr.UnionServer(("127.0.0.1", 0), [(str(_r1), False)], max_inflight=1)
    cap_thread = threading.Thread(target=capped.serve_forever, daemon=True)
    cap_thread.start()
    try:
        import socket as sock_mod

        slow = sock_mod.create_connection(("127.0.0.1", capped.server_port), timeout=5)
        try:
            # Hold 1st slot before finishing request headers
            slow.sendall(b"GET /_health/probe.bin HTTP/1.1\r\n")
            # Wait briefly for accept loop to dispatch worker #1
            for _ in range(50):
                if not capped.inflight.acquire(blocking=False):
                    break
                capped.inflight.release()
                threading.Event().wait(0.01)
            # 2nd connection must get immediate 503 without creating a worker thread
            status2, _hdrs2, _body2 = _req(capped.server_port, "GET", "/_health/probe.bin")
            assert status2 == 503
            slow.sendall(b"Host: 127.0.0.1\r\n\r\n")
            chunks = []
            while chunk := slow.recv(4096):
                chunks.append(chunk)
            assert b"HEALTH_OK_123456" in b"".join(chunks)
        finally:
            slow.close()
        # After slot #1 releases, subsequent request succeeds cleanly
        for _ in range(50):
            status3, _hdrs3, body3 = _req(capped.server_port, "GET", "/_health/probe.bin")
            if status3 == 200:
                break
            threading.Event().wait(0.01)
        assert status3 == 200 and body3 == b"HEALTH_OK_123456"
    finally:
        capped.shutdown()
        capped.server_close()
        cap_thread.join(timeout=3)


def test_traversal_authority_form_and_symlink_escapes_are_rejected(union_env):
    server, _r1, _r2, _r3 = union_env
    port = server.server_port

    with pytest.raises(ValueError, match="loopback only"):
        ufr.UnionServer(("0.0.0.0", 0), [(os.getcwd(), False)])

    # Leading // pointing to an EXISTING file must be rejected with 400 (including tab-delimited)
    assert _req(port, "GET", "//_health/probe.bin")[0] == 400
    assert _req(port, "GET", "http://127.0.0.1/_health/probe.bin")[0] == 400
    import socket as sock_mod

    with sock_mod.create_connection(("127.0.0.1", port), timeout=5) as raw_sock:
        raw_sock.sendall(b"GET\t//_health/probe.bin\tHTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        assert raw_sock.recv(1024).startswith(b"HTTP/1.1 400")

    for bad in (
        "/../etc/passwd",
        "/%2e%2e/outside_secret.txt",
        "/Movies//shared.mkv",
        "/%zz",
    ):
        status, _hdrs, _body = _req(port, "GET", bad)
        assert status == 400

    # Symlink on strict mount must never be followed
    status, _hdrs, body = _req(port, "GET", "/escape.txt")
    assert status == 404 and b"TOP_SECRET" not in body

    # Symlink on symlink-root escaping outside root_real must be rejected
    status, _hdrs, body = _req(port, "GET", "/GD4Movies/44/escape_gd4.txt")
    assert status == 404 and b"TOP_SECRET" not in body


def test_body_write_timeout_separation_restores_header_timeout_and_survives_pause(
    tmp_path: Path, monkeypatch
):
    import socket as sock_mod

    root = tmp_path / "pool"
    root.mkdir()
    # Explicitly cap both SO_SNDBUF and SO_RCVBUF to 64 KiB so a 4 MiB body deterministically
    # fills loopback buffers and blocks sendall() even on high-bandwidth tuned CI kernels.
    size = 4 * 1024 * 1024
    payload_file = root / "pause_test.mkv"
    with open(payload_file, "wb") as f:
        f.write(b"A" * size)

    class _TinyRcvBufConn(http.client.HTTPConnection):
        def connect(self):
            s = sock_mod.socket(sock_mod.AF_INET, sock_mod.SOCK_STREAM)
            s.setsockopt(sock_mod.SOL_SOCKET, sock_mod.SO_RCVBUF, 64 * 1024)
            s.settimeout(self.timeout)
            s.connect((self.host, self.port))
            self.sock = s

    observed_timeouts: list[float | None] = []
    orig_setup = ufr.UnionHandler.setup
    orig_serve = ufr.UnionHandler.serve

    def small_sndbuf_setup(self):
        self.request.setsockopt(sock_mod.SOL_SOCKET, sock_mod.SO_SNDBUF, 64 * 1024)
        orig_setup(self)

    def spy_serve(self):
        orig_serve(self)
        try:
            observed_timeouts.append(self.connection.gettimeout())
        except OSError:
            pass

    monkeypatch.setattr(ufr.UnionHandler, "timeout", 0.25)
    monkeypatch.setattr(ufr.UnionHandler, "setup", small_sndbuf_setup)
    monkeypatch.setattr(ufr.UnionHandler, "serve", spy_serve)

    # 1. Pause longer than header timeout (0.25s) but shorter than body_write_timeout (2.5s) succeeds intact
    srv = ufr.UnionServer(("127.0.0.1", 0), [(str(root), False)], body_write_timeout=2.5)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        conn = _TinyRcvBufConn("127.0.0.1", srv.server_port, timeout=10)
        conn.request("GET", "/pause_test.mkv", headers={"Range": f"bytes=0-{size - 1}"})
        resp = conn.getresponse()
        assert resp.status == 206
        first = resp.read(65536)
        assert len(first) == 65536
        # Pause longer than header timeout (0.25s) and verify server is actively blocked mid-stream
        threading.Event().wait(0.75)
        assert srv.inflight_current == 1
        assert not observed_timeouts
        total = len(first)
        while True:
            chunk = resp.read(262144)
            if not chunk:
                break
            total += len(chunk)
        conn.close()
        assert total == size
        for _ in range(50):
            if observed_timeouts and srv.inflight_current == 0:
                break
            threading.Event().wait(0.02)
        assert observed_timeouts[-1] == pytest.approx(0.25)
        assert srv.inflight_peak >= 1
        assert srv.inflight_current == 0
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=3)

    # 2. Stalling longer than body_write_timeout (0.2s) cuts the transfer and releases inflight slot
    short_srv = ufr.UnionServer(("127.0.0.1", 0), [(str(root), False)], body_write_timeout=0.2)
    short_th = threading.Thread(target=short_srv.serve_forever, daemon=True)
    short_th.start()
    try:
        conn = _TinyRcvBufConn("127.0.0.1", short_srv.server_port, timeout=5)
        conn.request("GET", "/pause_test.mkv", headers={"Range": f"bytes=0-{size - 1}"})
        resp = conn.getresponse()
        assert resp.status == 206
        head = resp.read(65536)
        threading.Event().wait(0.6)
        read_total = len(head)
        try:
            while True:
                b = resp.read(262144)
                if not b:
                    break
                read_total += len(b)
        except OSError:
            pass
        finally:
            conn.close()
        assert read_total < size
        for _ in range(50):
            if short_srv.inflight_current == 0:
                break
            threading.Event().wait(0.02)
        assert short_srv.inflight_current == 0
    finally:
        short_srv.shutdown()
        short_srv.server_close()
        short_th.join(timeout=3)


def test_body_write_timeout_cli_and_inflight_stats_validation(tmp_path: Path):
    root = tmp_path / "pool"
    root.mkdir()
    (root / "ok.bin").write_bytes(b"OK")

    args = ufr.parse_args(["--root", str(root), "--body-write-timeout", "420.5"])
    assert args.body_write_timeout == pytest.approx(420.5)

    for bad in (0.0, -5.0, float("nan"), float("inf"), 1e9):
        with pytest.raises(ValueError, match="body_write_timeout"):
            ufr.UnionServer(("127.0.0.1", 0), [(str(root), False)], body_write_timeout=bad)

    capped = ufr.UnionServer(("127.0.0.1", 0), [(str(root), False)], max_inflight=1)
    cap_thread = threading.Thread(target=capped.serve_forever, daemon=True)
    cap_thread.start()
    try:
        import socket as sock_mod

        slow = sock_mod.create_connection(("127.0.0.1", capped.server_port), timeout=5)
        try:
            slow.sendall(b"GET /ok.bin HTTP/1.1\r\n")
            for _ in range(50):
                if capped.inflight_current == 1:
                    break
                threading.Event().wait(0.01)
            assert capped.inflight_current == 1
            assert capped.inflight_peak == 1
            status2, _, _ = _req(capped.server_port, "GET", "/ok.bin")
            assert status2 == 503
            assert capped.rejected_503 == 1
            slow.sendall(b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n")
            while slow.recv(1024):
                pass
        finally:
            slow.close()
        for _ in range(50):
            if capped.inflight_current == 0:
                break
            threading.Event().wait(0.01)
        assert capped.inflight_current == 0
        assert capped.inflight_peak == 1
    finally:
        capped.shutdown()
        capped.server_close()
        cap_thread.join(timeout=3)

