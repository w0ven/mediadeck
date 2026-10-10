"""Native API-prefix consumers need relative direct URLs, not prefixed HTTPS text."""

# ruff: noqa: F811 - imported pytest fixtures
import re
import shutil
import subprocess
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import AsyncMock
from urllib.parse import urlencode, urlsplit

import pytest
from test_external_entries import client  # noqa: F401
from test_whitelist_handlers import restricted  # noqa: F401
from test_whitelist_route import guard  # noqa: F401

from app.main import app


@pytest.mark.parametrize("already_issued_first", [False, True])
def test_two_complete_direct_episodes_stop_then_next(
    restricted, monkeypatch, tmp_path, already_issued_first
):
    if not shutil.which("ffmpeg"):
        pytest.skip("native FFmpeg required for complete direct media consumption")
    c, entry_headers = restricted
    app.state.members.upsert("u1", "fixture", {"group_id": "whitelist"})
    # A single seat makes Stop -> next binding and stale old Stop observable.
    app.state.members.set_overrides("u1", {"max_streams": 1})
    app.state.emby.set_sessions(
        [
            {"Id": "entry-session", "UserId": "u1", "DeviceId": "entry-client"},
            {"Id": "other-session", "UserId": "u1", "DeviceId": "other-client"},
        ]
    )
    monkeypatch.setattr(
        app.state.emby, "personal_user_for_token", AsyncMock(return_value="u1"), raising=False
    )
    registry = tmp_path / "registry.json"
    registry.write_text("{}")
    app.state.whitelist_route.registry_path = str(registry)
    caller = "tok:u1"
    h = {**entry_headers, "X-Emby-Token": caller, "X-Emby-Device-Id": "entry-client"}

    async def issue(item, method, headers, query, payload):
        play = "play-" + item
        sid = "src-" + item
        q = {
            "Static": "true",
            "MediaSourceId": sid,
            "PlaySessionId": play,
            "DeviceId": "entry-client",
            "api_key": caller,
        }
        return 200, {
            "PlaySessionId": play,
            "MediaSources": [
                {
                    "Id": sid,
                    "Path": "/media/Movies/Demo/" + item + ".mkv",
                    "DirectStreamUrl": "/videos/" + item + "/original.mkv?" + urlencode(q),
                }
            ],
        }

    monkeypatch.setattr(app.state.emby, "playback_info", issue)
    media = tmp_path / "short.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=15",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(media),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    blob = media.read_bytes()
    counts = {"issuer_302": 0, "node_fetches": 0, "bad_prefix_404": 0}

    class Server(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body=b"", headers=None):
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            p = urlsplit(self.path)
            if p.path.startswith("/_n/"):
                admission = c.get(
                    "/api/access/route-file-admit",
                    headers={
                        **entry_headers,
                        "X-Original-Method": "GET",
                        "X-Original-URI": self.path,
                    },
                )
                if admission.status_code != 204:
                    return self.send(admission.status_code)
                counts["node_fetches"] += 1
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
                if match:
                    lo = int(match[1])
                    hi = min(len(blob) - 1, int(match[2]) if match[2] else len(blob) - 1)
                    if lo > hi:
                        return self.send(416)
                    return self.send(
                        206,
                        blob[lo : hi + 1],
                        {
                            "Content-Type": "video/x-matroska",
                            "Accept-Ranges": "bytes",
                            "Content-Range": f"bytes {lo}-{hi}/{len(blob)}",
                        },
                    )
                return self.send(
                    200, blob, {"Content-Type": "video/x-matroska", "Accept-Ranges": "bytes"}
                )
            response = c.get(self.path, headers=entry_headers, follow_redirects=False)
            if response.status_code == 302:
                counts["issuer_302"] += 1
                target = urlsplit(response.headers["location"])
                return self.send(302, headers={"Location": root + target.path + "?" + target.query})
            if p.path.startswith("/embyhttps://"):
                counts["bad_prefix_404"] += 1
            return self.send(response.status_code, response.content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    root = "http://127.0.0.1:" + str(server.server_port)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    db = app.state.streams._db

    def seats():
        return db.query("SELECT * FROM stream_leases WHERE user_id=?", ("u1",))

    previous = None
    frames = []
    try:
        for i, item in enumerate(["episode1", "episode2"]):
            info = c.post("/api/playback/info/" + item, headers=h, json={})
            assert info.status_code == 200
            data = info.json()
            value = data["MediaSources"][0]["DirectStreamUrl"]
            play = data["PlaySessionId"]
            assert len(seats()) == 1 and seats()[0]["play_id"] == play
            if previous is not None:
                assert c.post("/api/playback/stopped", headers=h, json=previous).status_code == 204
                assert len(seats()) == 1 and seats()[0]["play_id"] == play
            # Simulate the immutable relative URL issued for the first episode
            # before the bad absolute-URL release; the next is current metadata.
            if already_issued_first and i == 0:
                parsed = urlsplit(value)
                value = parsed.path + "?" + parsed.query
            url = root + "/emby" + value
            event = {
                "ItemId": item,
                "MediaSourceId": "src-" + item,
                "PlaySessionId": play,
                "SessionId": "entry-session",
                "PositionTicks": 0,
                "IsPaused": False,
                "PlayMethod": "DirectPlay",
            }
            try:
                assert c.post("/api/playback/started", headers=h, json=event).status_code == 204
                blocked = c.post(
                    "/api/playback/info/third",
                    headers={**h, "X-Emby-Device-Id": "other-client"},
                    json={},
                )
                assert blocked.status_code == 403
                decoder = subprocess.run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-nostats",
                        "-progress",
                        "pipe:1",
                        "-i",
                        url,
                        "-map",
                        "0:v:0",
                        "-an",
                        "-f",
                        "null",
                        "-",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                count = max(
                    [int(v) for v in re.findall(r"^frame=(\d+)$", decoder.stdout, re.MULTILINE)], default=0
                )
                print(
                    {
                        "episode": i + 1,
                        "fully_consumed_frames": count,
                        "exit": decoder.returncode,
                        "bad_prefix": urlsplit(url).path.startswith("/embyhttps://"),
                    }
                )
                assert decoder.returncode == 0 and count == 45
                assert "progress=end" in decoder.stdout
                frames.append(count)
                event["PositionTicks"] = 30000000
                assert c.post("/api/playback/progress", headers=h, json=event).status_code == 204
            finally:
                assert c.post("/api/playback/stopped", headers=h, json=event).status_code == 204
            assert seats() == []
            previous = deepcopy(event)
        assert frames == [45, 45] and counts["issuer_302"] >= 2 and counts["node_fetches"] >= 2
        assert counts["bad_prefix_404"] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("prefix", ["/videos", "/Videos", "/emby/Videos"])
@pytest.mark.parametrize("absolute", [False, True])
def test_only_relative_direct_shape_is_preserved(guard, prefix, absolute):
    from urllib.parse import parse_qs

    from test_whitelist_route import GD, entry

    path = prefix + "/item42/original.mkv"
    value = (
        ("https://upstream.example.com" if absolute else "")
        + path
        + "?MediaSourceId=source-gd&Static=true"
    )
    before = {"PlaySessionId": "play42", "MediaSources": [{**GD, "DirectStreamUrl": value}]}
    out = guard.decorate(entry(), "u-vip", "item42", before, "synthetic-caller")
    parsed = urlsplit(out["MediaSources"][0]["DirectStreamUrl"])
    assert before["MediaSources"][0]["DirectStreamUrl"] == value
    assert parsed.path == path
    assert parsed.hostname == ("vip.example.com" if absolute else None)
    assert parse_qs(parsed.query) == {
        "MediaSourceId": ["source-gd"],
        "Static": ["true"],
        "api_key": ["synthetic-caller"],
    }
