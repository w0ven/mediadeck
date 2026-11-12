"""End-to-end regression tests for metadata-only PlaybackInfo and over-limit notice media."""
# ruff: noqa: F811 - pytest injects fixtures using the imported names
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from test_stream_admission import (
    client,  # noqa: F401 - pytest fixture
    headers,
    row,
)

from app.main import app


def test_rex_detail_preload_while_phone_watching_never_blocks_or_sanctions(client, monkeypatch):
    monkeypatch.setattr("app.main.CONCURRENCY_GRACE_SECONDS", 0)
    app.state.members.set_overrides("u1", {"max_streams": 1})
    app.state.emby.set_sessions([
        {**row("phone-sid", "phone-dev", playing=True), "Client": "Filebar"},
        {**row("rex-sid", "rex-dev", playing=False), "Client": "Rex"},
    ])
    # Phone already holds the sole active seat.
    assert client.get(
        "/api/playback/admit",
        headers={**headers("phone-dev"), "X-Original-URI": "/Videos/item42/original.mkv"},
    ).status_code == 204

    # Computer adds account and opens detail page (Rex sends PlaybackInfo with IsPlayback=true).
    for _ in range(4):
        resp = client.post(
            "/api/playback/info/item42?IsPlayback=true&AutoOpenLiveStream=true",
            headers={**headers("rex-dev"), "User-Agent": "Rex-Standard/1.2"},
            json={},
        )
        assert resp.status_code == 200
        assert resp.json()["PlaySessionId"]

    # Zero concurrency sanctions, and phone's single lease remains untouched.
    assert app.state.restrictions.events() == []
    leases = app.state.db.query("SELECT * FROM stream_leases WHERE user_id='u1'")
    assert len(leases) == 1 and leases[0]["session_id"] == "phone-sid"


def test_over_limit_direct_stream_serves_notice_video_and_isolates_progress(
    client, monkeypatch, tmp_path
):
    monkeypatch.setattr("app.main.CONCURRENCY_GRACE_SECONDS", 0)
    monkeypatch.setenv("MEDIADECK_CONCURRENCY_NOTICE_URL", "/_concurrency_notice/concurrency-limit.mp4")
    hls_dir = tmp_path / "notice_hls"
    hls_dir.mkdir()
    (hls_dir / "master.m3u8").write_text("#EXTM3U\n/_concurrency_notice/main.m3u8\n", encoding="utf-8")
    (hls_dir / "main.m3u8").write_text(
        "#EXTM3U\n#EXTINF:4.0,\n/_concurrency_notice/seg-00.ts\n#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MEDIADECK_CONCURRENCY_NOTICE_HLS_DIR", str(hls_dir))

    app.state.members.set_overrides("u1", {"max_streams": 1})
    app.state.emby.set_sessions([
        {**row("phone-sid", "phone-dev", playing=True), "Client": "Filebar"},
        {**row("rex-sid", "rex-dev", playing=False), "Client": "Rex"},
    ])
    report_playback = AsyncMock(return_value=204)
    report_stopped = AsyncMock(return_value=204)
    monkeypatch.setattr(app.state.emby, "report_playback", report_playback)
    monkeypatch.setattr(app.state.emby, "report_stopped", report_stopped)

    # Phone acquires seat and starts playing.
    phone_info = client.post("/api/playback/info/item42", headers=headers("phone-dev"), json={})
    phone_play = phone_info.json()["PlaySessionId"]
    assert client.get(
        f"/Videos/item42/original.mkv?PlaySessionId={phone_play}",
        headers=headers("phone-dev"),
        follow_redirects=False,
    ).status_code == 302

    # Second device gets PlaybackInfo metadata, then actually clicks play (fetches stream).
    rex_info = client.post("/api/playback/info/item42", headers=headers("rex-dev"), json={})
    rex_play = rex_info.json()["PlaySessionId"]

    direct = client.get(
        f"/Videos/item42/original.mkv?PlaySessionId={rex_play}",
        headers=headers("rex-dev"),
        follow_redirects=False,
    )
    assert direct.status_code == 302
    assert direct.headers["location"] == "/_concurrency_notice/concurrency-limit.mp4"
    assert direct.headers["X-Mediadeck-Decision"] == "over-limit-notice"

    # HLS manifest path also serves the notice playlist instead of 403 or real movie.
    manifest = client.get(
        "/api/playback/manifest",
        headers={
            **headers("rex-dev"),
            "X-Original-URI": f"/Videos/item42/master.m3u8?DeviceId=rex-dev&PlaySessionId={rex_play}",
        },
    )
    assert manifest.status_code == 200
    assert "/_concurrency_notice/main.m3u8" in manifest.text
    assert manifest.headers["X-Mediadeck-Decision"] == "over-limit-notice"

    # Concurrency restriction event (group + private notice) is still recorded for real stream attempt.
    events = app.state.restrictions.events()
    assert len(events) == 1 and events[0]["rule"] == "concurrency"

    # Lifecycle reports from the notice clip are absorbed (204) and NEVER forwarded to Emby!
    for ep in ("started", "progress", "stopped"):
        r = client.post(
            f"/api/playback/{ep}",
            headers=headers("rex-dev"),
            json={"ItemId": "item42", "PlaySessionId": rex_play, "PositionTicks": 40_000_000},
        )
        assert r.status_code == 204
    report_playback.assert_not_awaited()
    report_stopped.assert_not_awaited()

    # Even a PlaySessionId-omitted report from that refused device is absorbed while phone still holds its seat.
    assert client.post(
        "/api/playback/progress",
        headers=headers("rex-dev"),
        json={"ItemId": "item42", "PositionTicks": 50_000_000},
    ).status_code == 204
    report_playback.assert_not_awaited()

    # Phone's legitimate progress still forwards to Emby normally.
    assert client.post(
        "/api/playback/progress",
        headers=headers("phone-dev"),
        json={"ItemId": "item42", "PlaySessionId": phone_play, "PositionTicks": 600_000_000},
    ).status_code == 204
    report_playback.assert_awaited_once()


@pytest.mark.parametrize("action", ["disable", "delete"])
def test_destructive_concurrency_action_refuses_with_403_instead_of_notice_clip(
    client, monkeypatch, action
):
    monkeypatch.setattr("app.main.CONCURRENCY_GRACE_SECONDS", 0)
    monkeypatch.setenv("MEDIADECK_CONCURRENCY_NOTICE_URL", "/_concurrency_notice/concurrency-limit.mp4")
    app.state.restrictions.save({"concurrency": {"enabled": True, "action": action}})
    app.state.emby.list_users = AsyncMock(
        return_value=[{"Id": "u1", "Name": "demo", "Policy": {"IsAdministrator": False}}]
    )
    app.state.emby.apply_member_policy = AsyncMock(return_value={"status": "applied"})
    app.state.emby.delete_user = AsyncMock(return_value=True)
    app.state.members.set_overrides("u1", {"max_streams": 1})
    app.state.emby.set_sessions([row("a", playing=True), row("b")])

    r = client.get("/Videos/item42/original.mkv", headers=headers("b"), follow_redirects=False)
    assert r.status_code == 403
    assert "location" not in r.headers


def test_non_concurrency_denials_never_redirect_to_concurrency_notice(client, monkeypatch):
    monkeypatch.setenv("MEDIADECK_CONCURRENCY_NOTICE_URL", "/_concurrency_notice/concurrency-limit.mp4")
    # Unauthenticated -> 401
    assert client.get("/Videos/item42/original.mkv", follow_redirects=False).status_code == 401
    # Blocked device -> 403, not notice 302
    monkeypatch.setattr(app.state.members, "device_blocked", lambda uid, dev: dev == "b")
    r = client.get("/Videos/item42/original.mkv", headers=headers("b"), follow_redirects=False)
    assert r.status_code == 403 and "location" not in r.headers


def test_gateway_cmcc_over_limit_redirects_to_notice_video(client, monkeypatch, tmp_path):
    import http.client
    import importlib.util
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    from threading import Thread

    monkeypatch.setenv("MEDIADECK_CONCURRENCY_NOTICE_URL", "/_concurrency_notice/concurrency-limit.mp4")
    app.state.members.set_overrides("u1", {"max_streams": 1})
    app.state.emby.set_sessions([row("a", playing=True), row("b")])

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("gateway_notice_test", root / "deploy/gateway/gateway.py")
    assert spec and spec.loader
    gw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gw)

    reg = tmp_path / "sources.json"
    reg.write_text(json.dumps({"ms1": {"item_ids": ["item42"], "url": "https://openlist.example.com/d/a.mkv"}}))
    monkeypatch.setattr(gw, "REG", reg)
    monkeypatch.setattr(gw, "OPENLIST_PUBLIC_HOST", "openlist.example.com")
    gw.reset_registry_cache()

    class Bridge(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            res = client.get(self.path, headers=dict(self.headers), follow_redirects=False)
            self.send_response(res.status_code)
            for k, v in res.headers.items():
                if k.lower() not in ("content-length", "transfer-encoding", "connection"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(res.content)))
            self.end_headers()
            self.wfile.write(res.content)

    deck_srv = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
    Thread(target=deck_srv.serve_forever, daemon=True).start()
    gw_srv = ThreadingHTTPServer(("127.0.0.1", 0), gw.Handler)
    Thread(target=gw_srv.serve_forever, daemon=True).start()

    real_conn = http.client.HTTPConnection
    monkeypatch.setattr(
        gw.http.client,
        "HTTPConnection",
        lambda host, port, **kw: real_conn(host, deck_srv.server_port if port == 8300 else port, **kw),
    )
    try:
        conn = real_conn("127.0.0.1", gw_srv.server_port, timeout=5)
        conn.request(
            "GET",
            "/Videos/item42/original.mkv?MediaSourceId=ms1",
            headers=headers("b"),
        )
        resp = conn.getresponse()
        assert resp.status == 302
        assert resp.getheader("Location") == "/_concurrency_notice/concurrency-limit.mp4"
        assert resp.getheader("X-Mediadeck-Decision") == "over-limit-notice"
        conn.close()
    finally:
        gw_srv.shutdown()
        gw_srv.server_close()
        deck_srv.shutdown()
        deck_srv.server_close()
