"""Session admission and shared entrance regression tests."""
from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from test_membership import _basic

from app.adapters.mock import MockEmby
from app.core.db import Database
from app.main import app
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.streams import StreamAdmission, occupied_keys, overflow_session_ids


def row(sid, device=None, *, playing=False, paused=False):
    result = {"Id": sid, "UserId": "u1", "DeviceId": device or sid}
    if playing:
        result.update(NowPlayingItem={"Id": "item42"}, PlayState={"IsPaused": paused})
    return result


def headers(device="a"):
    return {"X-Emby-Token": "tok:u1", "X-Emby-Device-Id": device}


@pytest.fixture()
def stack(tmp_path):
    db = Database(tmp_path / "test.db")
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    members.upsert("u1", "demo", {"group_id": "standard"})
    members.set_overrides("u1", {"max_streams": 2})
    emby = MockEmby()
    emby.set_sessions([row(s) for s in "abc"])
    yield members, emby, StreamAdmission(members, emby)
    db.close()


def test_two_starts_then_third_and_seek(stack):
    _, _, guard = stack
    async def check():
        for s in "ab":
            assert (await guard.inspect("u1", s)).allowed
        assert not (await guard.inspect("u1", "c")).allowed
        for _ in range(5):
            assert (await guard.inspect("u1", "a")).allowed
    asyncio.run(check())


def test_last_slot_race_and_restart(stack):
    members, emby, guard = stack
    async def check():
        assert (await guard.inspect("u1", "a")).allowed
        results = await asyncio.gather(guard.inspect("u1", "b"), guard.inspect("u1", "c"))
        assert sum(r.allowed for r in results) == 1
        restarted = StreamAdmission(members, emby)
        assert not (await restarted.inspect("u1", "c")).allowed
    asyncio.run(check())


def test_pending_expiry_requires_fresh_idle_snapshot(stack, monkeypatch):
    _, emby, guard = stack
    async def check():
        for s in "ab":
            assert (await guard.inspect("u1", s)).allowed
        monkeypatch.setattr("time.time", lambda: 9999999999)
        # Time alone is insufficient when Emby is unavailable.
        original = emby.active_sessions_raw
        emby.active_sessions_raw = AsyncMock(side_effect=RuntimeError("offline"))
        assert (await guard.inspect("u1", "c")).reason == "sessions-unavailable"
        assert len(guard._db.query("SELECT * FROM stream_leases")) == 2
        emby.active_sessions_raw = original
        assert (await guard.inspect("u1", "c")).allowed
    asyncio.run(check())


def test_pause_resume_retains_seat_stop_releases(stack):
    _, emby, guard = stack
    async def check():
        for s in "ab":
            assert (await guard.inspect("u1", s)).allowed
        emby.set_sessions([row("a", playing=True, paused=True), row("b", playing=True), row("c")])
        assert not (await guard.inspect("u1", "c")).allowed
        assert (await guard.inspect("u1", "a")).allowed
        emby.set_sessions([row("a"), row("b", playing=True), row("c")])
        assert (await guard.inspect("u1", "c")).allowed
    asyncio.run(check())


def test_same_device_sessions_are_not_merged(stack):
    _, emby, guard = stack
    emby.set_sessions([row(s, "shared", playing=s != "c") for s in "abc"])
    assert occupied_keys(emby._sessions, "u1") == {"sid:a", "sid:b"}
    assert not asyncio.run(guard.inspect("u1", "shared", session_id="c")).allowed
    assert asyncio.run(guard.inspect("u1", "shared", session_id="a")).allowed
    assert not asyncio.run(guard.inspect("u1", "shared")).allowed


def test_same_device_duplicate_is_resolved_only_by_exact_client_profile(stack):
    _, emby, guard = stack
    emby.set_sessions([
        {**row("hills-windows", "shared"), "Client": "Hills Windows",
         "ApplicationVersion": "1.5.3", "DeviceName": "PC-202309131322"},
        {**row("hills", "shared"), "Client": "Hills",
         "ApplicationVersion": "1.5.3", "DeviceName": "PC-202309131322"},
    ])
    profile = {"Client": "hills windows", "ApplicationVersion": "1.5.3",
               "DeviceName": "pc-202309131322"}
    result = asyncio.run(guard.inspect("u1", "shared", session_profile=profile))
    assert result.allowed and result.reason == "granted"
    leases = guard._db.query("SELECT session_id FROM stream_leases")
    assert [lease["session_id"] for lease in leases] == ["hills-windows"]


def test_same_device_duplicate_stays_unresolved_when_profile_is_not_unique(stack):
    _, emby, guard = stack
    duplicate = {"Client": "Hills Windows", "ApplicationVersion": "1.5.3",
                 "DeviceName": "same-pc"}
    emby.set_sessions([{**row(sid, "shared"), **duplicate} for sid in ("a", "b")])
    result = asyncio.run(guard.inspect("u1", "shared", session_profile=duplicate))
    assert not result.allowed and result.reason == "session-unresolved"
    assert guard._db.query("SELECT * FROM stream_leases") == []


def test_explicit_session_id_remains_authoritative_over_profile(stack):
    _, emby, guard = stack
    emby.set_sessions([
        {**row("a", "shared"), "Client": "Hills"},
        {**row("b", "shared"), "Client": "Hills Windows"},
    ])
    result = asyncio.run(guard.inspect("u1", "shared", session_id="a",
                                      session_profile={"Client": "other"}))
    assert result.allowed
    assert guard._db.one("SELECT session_id FROM stream_leases")["session_id"] == "a"


def test_premature_playing_report_cannot_admit_third(stack):
    _, emby, guard = stack
    async def check():
        for s in "ab":
            assert (await guard.inspect("u1", s)).allowed
        emby.set_sessions([row(s, playing=True) for s in "abc"])
        assert not (await guard.inspect("u1", "c")).allowed
        assert (await guard.inspect("u1", "a")).allowed
    asyncio.run(check())


def test_unknown_identity_and_bad_snapshot_fail_closed(stack):
    _, emby, guard = stack
    assert not asyncio.run(guard.inspect(None, "a")).allowed
    emby.active_sessions_raw = AsyncMock(side_effect=RuntimeError("unavailable"))
    assert asyncio.run(guard.inspect("u1", "a")).reason == "sessions-unavailable"
    emby.active_sessions_raw = AsyncMock(return_value={})
    assert not asyncio.run(guard.inspect("u1", "a")).allowed
    emby.active_sessions_raw = AsyncMock(return_value=[{"NowPlayingItem": {"Id": "item"}}])
    assert asyncio.run(guard.inspect("u1", "a")).reason == "sessions-unavailable"


@pytest.mark.parametrize("accepted", [False, True])
def test_sampler_stop_acceptance_also_backs_off(stack, accepted):
    from app.modules.usage import UsageSampler
    members, emby, _ = stack
    emby.stop_session = AsyncMock(return_value=accepted)
    sampler = UsageSampler(members._db, members, emby, object())
    rows = [{**row(s, playing=True), "PlayStartTime": str(n)}
            for n, s in enumerate("abc")]
    sampler._collect_overflow_kicks(rows, time.time())
    asyncio.run(sampler._kick_overflow_streams())
    sampler._collect_overflow_kicks(rows, time.time())
    asyncio.run(sampler._kick_overflow_streams())
    emby.stop_session.assert_awaited_once_with("c", "同时播放路数已达上限")


def test_no_kick_order_from_last_activity():
    rows = [{**row(s, playing=True), "LastActivityDate": n} for n, s in enumerate("abc")]
    assert overflow_session_ids(rows, "u1", 2) == []


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.put("/api/settings/playback", headers=_basic(), json={"enabled": True})
        c.put("/api/members/u1", headers=_basic(), json={"group_id": "standard", "username": "demo"})
        c.put("/api/members/u1/overrides", headers=_basic(), json={"max_streams": 2})
        app.state.emby.set_sessions([row(s) for s in "abc"])
        yield c


@pytest.mark.parametrize("path", [
    "/emby/Items/item42/PlaybackInfo", "/Items/item42/PlaybackInfo",
    "/emby/Videos/item42/stream.mkv?Static=true", "/videos/item42/original",
    "/Videos/item42/master.m3u8?MediaSourceId=edition", "/Videos/item42/hls1/main/0.ts",
])
def test_shared_admission_refuses_before_all_link_entrances(client, path):
    for device in "abc":
        r = client.get("/api/playback/admit", headers={**headers(device), "X-Original-URI": path})
        assert r.status_code == (204 if device != "c" else 403)
        assert "location" not in r.headers


def test_shared_and_direct_do_not_double_reserve(client):
    for device in "ab":
        assert client.get("/api/playback/admit", headers={**headers(device),
            "X-Original-URI": "/Items/item42/PlaybackInfo"}).status_code == 204
        assert client.get("/Videos/item42/stream.mkv?Static=true", headers=headers(device),
                          follow_redirects=False).status_code == 302
    assert client.get("/Videos/item42/stream.mkv?Static=true", headers=headers("c"),
                      follow_redirects=False).status_code == 403


def test_auth_permissions_and_failure_never_return_link(client):
    url = "/Videos/item42/stream.mkv?Static=true"
    assert client.get(url, follow_redirects=False).status_code == 401
    assert client.get(url, headers={"X-Emby-Token": "invalid-token"}).status_code == 403
    app.state.emby.verify_item_access = AsyncMock(return_value=False)
    assert client.get(url, headers=headers()).status_code == 403
    app.state.emby.verify_item_access = AsyncMock(side_effect=RuntimeError())
    assert client.get(url, headers=headers()).status_code == 503


def test_stopped_report_releases_pending_only_on_success(client):
    for device in "ab":
        assert client.get("/api/playback/admit", headers={**headers(device),
            "X-Original-URI": "/Items/item42/PlaybackInfo"}).status_code == 204
    original = app.state.emby.report_stopped
    app.state.emby.report_stopped = AsyncMock(return_value=503)
    assert client.post("/api/playback/stopped", headers=headers(), json={}).status_code == 503
    url = "/api/playback/admit"
    third = {**headers("c"), "X-Original-URI": "/Items/item42/PlaybackInfo"}
    assert client.get(url, headers=third).status_code == 403
    app.state.emby.report_stopped = original
    assert client.post("/api/playback/stopped", headers=headers(), json={}).status_code == 204
    assert client.get(url, headers=third).status_code == 204


def test_query_only_playbackinfo_post_preserves_empty_body(client):
    original = app.state.emby.playback_info
    app.state.emby.playback_info = AsyncMock(wraps=original)
    response = client.post("/api/playback/info/item42?IsPlayback=true&StartTimeTicks=123",
                           headers=headers())
    assert response.status_code == 200
    args = app.state.emby.playback_info.await_args.args
    assert args[1] == "POST" and args[3]["StartTimeTicks"] == "123"
    assert args[4] is None
    assert client.post("/api/playback/info/item42", headers=headers("b"),
                       content="not-json").status_code == 400


def test_hills_playbackinfo_without_session_id_resolves_duplicate_device(client):
    app.state.emby.set_sessions([
        {**row("hills-windows", "shared"), "Client": "Hills Windows",
         "ApplicationVersion": "1.5.3", "DeviceName": "PC-202309131322"},
        {**row("hills", "shared"), "Client": "Hills",
         "ApplicationVersion": "1.5.3", "DeviceName": "PC-202309131322"},
    ])
    authorization = ('MediaBrowser Token="tok:u1", Client="Hills Windows", '
                     'Device="PC-202309131322", DeviceId="shared", Version="1.5.3"')
    response = client.post("/api/playback/info/item42", params={
        "UserId": "u1", "X-Emby-Authorization": authorization,
        "X-Emby-Client": "Hills Windows", "X-Emby-Client-Version": "1.5.3",
        "X-Emby-Device-Id": "shared", "X-Emby-Device-Name": "PC-202309131322",
    }, json={})
    assert response.status_code == 200
    lease = app.state.db.one("SELECT session_id,play_id FROM stream_leases")
    assert lease["session_id"] == "hills-windows"
    assert lease["play_id"] == response.json()["PlaySessionId"]


def test_hills_direct_admission_uses_profile_from_original_query(client):
    app.state.emby.set_sessions([
        {**row("hills-windows", "shared"), "Client": "Hills Windows",
         "ApplicationVersion": "1.5.3", "DeviceName": "PC-202309131322"},
        {**row("hills", "shared"), "Client": "Hills",
         "ApplicationVersion": "1.5.3", "DeviceName": "PC-202309131322"},
    ])
    authorization = ('MediaBrowser Token="tok:u1", Client="Hills Windows", '
                     'Device="PC-202309131322", DeviceId="shared", Version="1.5.3"')
    query = urlencode({
        "UserId": "u1", "X-Emby-Authorization": authorization,
        "X-Emby-Client": "Hills Windows", "X-Emby-Client-Version": "1.5.3",
        "X-Emby-Device-Id": "shared", "X-Emby-Device-Name": "PC-202309131322",
    })
    response = client.get("/api/playback/admit", headers={
        "X-Original-URI": "/Videos/item42/stream.mkv?Static=true&" + query,
    })
    assert response.status_code == 204
    assert app.state.db.one("SELECT session_id FROM stream_leases")["session_id"] == "hills-windows"


def test_live_metadata_keeps_caller_profile_and_stop_does_not_hide_session(monkeypatch):
    from app.adapters.live import LiveEmby
    seen = []
    def upstream(request):
        seen.append(request)
        if request.url.path.endswith("PlaybackInfo"):
            assert request.headers["x-emby-token"] == "caller-test-token"
            assert request.url.params["MediaSourceId"] == "selected-edition"
            assert json.loads(request.content) == {"DeviceProfile": {"Name": "synthetic-client"}}
            return httpx.Response(200, json={"PlaySessionId": "play-new", "MediaSources": []})
        return httpx.Response(204)
    adapter = LiveEmby(lambda: {"enabled": True, "url": "https://emby.example.invalid",
                               "api_key": "admin-test-token", "verify_ssl": True})
    monkeypatch.setattr(adapter, "_client", lambda *_: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)))
    async def check():
        code, data = await adapter.playback_info("item", "POST",
            {"X-Emby-Token": "caller-test-token"}, {"MediaSourceId": "selected-edition"},
            {"DeviceProfile": {"Name": "synthetic-client"}})
        assert code == 200 and data["PlaySessionId"] == "play-new"
        assert await adapter.stop_session("session")
        assert not any(request.method == "DELETE" for request in seen)
    asyncio.run(check())


def test_delayed_old_stopped_cannot_release_new_play_and_hls_without_device(client):
    def info(device):
        r = client.post("/api/playback/info/item42", headers=headers(device), json={})
        assert r.status_code == 200
        return r.json()["PlaySessionId"]
    old = info("a")
    current = info("a")
    info("b")
    assert client.post("/api/playback/stopped", headers=headers("a"),
                       json={"PlaySessionId": old}).status_code == 204
    third = {**headers("c"), "X-Original-URI": "/Items/item42/PlaybackInfo"}
    assert client.get("/api/playback/admit", headers=third).status_code == 403
    assert client.get("/api/playback/admit", headers={"X-Emby-Token": "tok:u1",
        "X-Original-URI": "/Videos/item42/master.m3u8?PlaySessionId=" + current}).status_code == 204
    assert client.post("/api/playback/stopped", headers=headers("a"),
                       json={"PlaySessionId": current}).status_code == 204
    assert client.get("/api/playback/admit", headers=third).status_code == 204


def test_real_nginx_auth_request_covers_gateway_playbackinfo_and_hls(client, tmp_path):
    nginx = shutil.which("nginx")
    if not nginx:
        pytest.skip("nginx executable required for entrance integration")
    calls = []

    class Backend(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.startswith("/api/playback/"):
                result = client.get(self.path, headers=dict(self.headers))
                self.send_response(result.status_code)
                self.end_headers()
                self.wfile.write(result.content)
                return
            calls.append(self.path)
            if "stream" in self.path or "original" in self.path or "cmcc-source" in self.path:
                self.send_response(302)
                self.send_header("Location", "https://media.example.invalid/direct")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"MediaSources":[{"DirectStreamUrl":"https://media.example.invalid/direct"}]}')

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path.startswith("/api/playback/"):
                result = client.post(self.path, headers=dict(self.headers), content=body)
                self.send_response(result.status_code)
                self.end_headers()
                self.wfile.write(result.content)
            else:
                self.do_GET()

    backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    address = f"127.0.0.1:{backend.server_port}"
    snippet = (Path(__file__).resolve().parents[2] / "deploy/nginx/playback-admission.conf").read_text()
    config = tmp_path / "nginx.conf"
    config.write_text(f'''daemon off; master_process off;
pid {tmp_path}/nginx.pid;
error_log {tmp_path}/error.log;
events {{ worker_connections 128; }}
http {{
 access_log off;
 upstream deck_admission_backend {{ server {address}; }}
 upstream emby_playback_origin {{ server {address}; }}
 server {{ listen 127.0.0.1:{port};
 location ~* ^/(emby/)?Videos/[^/]+/(stream|original)(\\.[A-Za-z0-9]+)?$ {{
  auth_request /_deck_admission;
  proxy_pass http://emby_playback_origin;
 }}
 {snippet}
 location ^~ /cmcc-source/ {{ proxy_pass http://emby_playback_origin; }}
 location / {{ proxy_pass http://emby_playback_origin; }}
 }}
}}
''')
    subprocess.run([nginx, "-t", "-p", str(tmp_path), "-c", str(config)], check=True,
                   capture_output=True)
    process = subprocess.Popen([nginx, "-p", str(tmp_path), "-c", str(config)])
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as edge:
            for _ in range(100):
                try:
                    edge.get("/health")
                    break
                except httpx.ConnectError:
                    time.sleep(0.02)
            # Auxiliary resources must not consume a seat, even on a third device.
            assert edge.get("/Videos/item42/Subtitles/0/Stream.srt",
                            headers=headers("c")).status_code == 200
            assert app.state.db.query("SELECT * FROM stream_leases") == []
            empty = edge.post("/emby/items/item42/playbackinfo?DeviceId=a&api_key=tok:u1")
            assert empty.status_code == 200  # query-only POST, case-insensitive native path
            play_ids = {}
            for device in "ab":
                info = edge.post("/Items/item42/PlaybackInfo", headers=headers(device),
                                 json={"IsPlayback": True})
                assert info.status_code == 200
                play_ids[device] = info.json()["PlaySessionId"]
                for path in ("/emby/sessions/playing", "/Sessions/Playing/Progress"):
                    assert edge.post(path, headers=headers(device), json={
                        "ItemId": "item42", "PlaySessionId": play_ids[device]}).status_code == 204
                assert app.state.db.one("SELECT observed FROM stream_leases WHERE session_id=?",
                                        (device,))["observed"] == 1
                for path in ("/Videos/item42/stream.mkv?Static=true&MediaSourceId=mobile",
                             "/emby/videos/item42/original.mkv?MediaSourceId=mobile"):
                    assert edge.get(path, headers=headers(device)).status_code == 302
                assert edge.get("/Videos/item42/master.m3u8", headers=headers(device)).status_code == 200
            before = len(calls)
            for method, path in (("POST", "/Items/item42/PlaybackInfo"),
                                 ("GET", "/Videos/item42/stream.mkv?Static=true&MediaSourceId=mobile"),
                                 ("GET", "/Videos/item42/master.m3u8"),
                                 ("GET", "/Videos/item42/hls1/main/0.ts")):
                result = edge.request(method, path, headers=headers("c"), json={})
                assert result.status_code == 403
                assert "location" not in result.headers
            assert len(calls) == before  # no URL issuer was contacted for the third play
            assert edge.get("/_deck_admission").status_code == 404
            assert edge.get("/cmcc-source/demo?cap=source-capability").status_code == 302
            assert edge.post("/Sessions/Playing/Stopped", headers=headers("a"),
                             json={"PlaySessionId": play_ids["a"]}).status_code == 204
            assert edge.post("/Items/item42/PlaybackInfo", headers=headers("c"), json={}).status_code == 200
            app.state.emby.active_sessions_raw = AsyncMock(side_effect=RuntimeError("offline"))
            before = len(calls)
            assert edge.get("/Videos/item42/master.m3u8", headers=headers("b")).status_code == 500
            assert len(calls) == before  # auth_request upstream errors never fall back
    finally:
        process.terminate()
        process.wait(timeout=10)
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=10)
