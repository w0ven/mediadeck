# ruff: noqa: F811 - imported pytest fixture name
"""Restricted lifecycle must use admission's personal-owner proof.

All upstream responses are synthetic MockTransport data; app/DB/settings are
isolated by conftest. The actual LiveEmby identity and report methods run.
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_external_entries import ADMIN, client  # noqa: F401

from app.adapters.live import LiveEmby
from app.main import app

PERSONAL = "synthetic-personal"
MANAGEMENT = "synthetic-management"
UID = "u1"
DEVICE = "synthetic-device"
SESSION = "synthetic-session"
PLAY = "synthetic-play"
EVENTS = ("started", "progress", "stopped")
PAYLOAD = {"ItemId": "synthetic-item", "SessionId": SESSION,
           "PlaySessionId": PLAY, "PositionTicks": 0, "IsPaused": False}


@pytest.fixture
def pipeline(client, monkeypatch):
    client.put("/api/settings/integration", auth=ADMIN, json={"external_entries": [
        {"id": "vip", "origin": "https://vip.example.com", "whitelist_only": True},
        {"id": "friend", "origin": "https://friend.example.com"}]})
    app.state.members.upsert(UID, "fixture", {"group_id": "whitelist", "status": "active"})
    entries = app.state.settings_service.integration_config()["external_entries"]
    clock = round(time.time(), 3)
    state = SimpleNamespace(rows=[{"Id": SESSION, "UserId": UID, "DeviceId": DEVICE}],
                            posts=[], clock=clock, authority_down=False)

    def transport(request):
        token = request.headers.get("X-Emby-Token") or request.url.params.get("api_key")
        if request.url.path == "/emby/Auth/Keys":
            if state.authority_down:
                raise httpx.ReadTimeout("synthetic authority failure", request=request)
            if token == MANAGEMENT:
                return httpx.Response(200, json={"Items": [{"AccessToken": MANAGEMENT}]})
            return httpx.Response(403 if token == PERSONAL else 401)
        if request.url.path == "/emby/Sessions":
            return httpx.Response(200, json=copy.deepcopy(state.rows))
        if request.method == "POST" and request.url.path.startswith("/emby/Sessions/Playing"):
            body = json.loads(request.content)
            state.posts.append((request.url.path, dict(request.headers), body))
            if not request.url.path.endswith("/Stopped"):
                owned = next(row for row in state.rows if row.get("UserId") == UID)
                owned.update(PlaySessionId=PLAY,
                             LastActivityDate=datetime.fromtimestamp(state.clock, UTC).isoformat(),
                             NowPlayingItem={"Id": "synthetic-item", "Name": "fixture",
                                             "RunTimeTicks": 600 * 10_000_000,
                                             "Bitrate": 4_000_000},
                             PlayState={"PositionTicks": body.get("PositionTicks", 0),
                                        "IsPaused": body.get("IsPaused", False), "PlaybackRate": 1.0})
            # Stop intentionally leaves a stale /Sessions row for sampler regression.
            return httpx.Response(204)
        raise AssertionError(f"Unexpected synthetic request: {request.method} {request.url.path}")

    live = LiveEmby(lambda: {"enabled": True, "url": "https://emby.invalid",
                            "api_key": MANAGEMENT, "timeout_seconds": 1, "verify_ssl": True})
    monkeypatch.setattr(live, "_client", lambda *args: httpx.AsyncClient(
        transport=httpx.MockTransport(transport), trust_env=False))
    monkeypatch.setattr(app.state, "emby", live)
    monkeypatch.setattr(app.state.streams, "_emby", live)
    monkeypatch.setattr(app.state.usage, "_emby", live)
    usage_report = AsyncMock(wraps=app.state.usage.playback_report)
    monkeypatch.setattr(app.state.usage, "playback_report", usage_report)

    def headers(entry="vip"):
        row = next(v for v in entries if v["id"] == entry)
        return {"X-Mediadeck-Entry": row["id"], "X-Mediadeck-Entry-Key": row["proxy_key"],
                "X-Emby-Token": PERSONAL, "X-Emby-Device-Id": DEVICE}

    def lease():
        app.state.db.execute("INSERT INTO stream_leases "
                             "(user_id,session_id,observed,pending_at,play_id) VALUES(?,?,0,?,?)",
                             (UID, SESSION, time.time(), PLAY))

    return SimpleNamespace(client=client, state=state, live=live, headers=headers,
                           usage_report=usage_report, lease=lease)


@pytest.mark.parametrize("event", EVENTS)
@pytest.mark.parametrize("discovery_device", [None, "other-device", DEVICE])
def test_discovery_row_keeps_admission_and_lifecycle_identity_consistent(pipeline, event, discovery_device):
    p = pipeline
    if discovery_device:
        p.state.rows.append({"Id": "discovery", "DeviceId": discovery_device})
    # This exact real legacy lookup was the old handler's first failing branch.
    legacy = asyncio.run(p.live.user_for_token(PERSONAL, DEVICE))
    assert (legacy is None) == (discovery_device == DEVICE)
    original_path = "/Sessions/Playing" + {"started": "", "progress": "/Progress", "stopped": "/Stopped"}[event]
    admission = p.client.get("/api/access/route-admit", headers={**p.headers(),
                             "X-Original-URI": original_path, "X-Original-Method": "POST"})
    assert admission.status_code == 204
    response = p.client.post(f"/api/playback/{event}", headers=p.headers(), json=PAYLOAD)
    assert response.status_code == 204
    assert len(p.state.posts) == 1
    assert p.state.posts[0][0] == "/emby" + original_path
    assert not any(key.startswith("x-mediadeck-entry") for key in p.state.posts[0][1])
    assert p.usage_report.await_count == (0 if event == "progress" else 1)


@pytest.mark.parametrize("event", EVENTS)
@pytest.mark.parametrize("case,expected", [("management", 403), ("multiple_owners", 403),
    ("ordinary_group", 403), ("suspended", 403), ("expired", 403), ("missing_token", 401),
    ("conflicting_token", 403), ("duplicate_token", 403), ("authority_down", 503)])
def test_restricted_reports_keep_strict_denials_before_delivery(pipeline, monkeypatch, case, expected, event):
    p = pipeline
    headers = p.headers()
    if case == "management":
        headers["X-Emby-Token"] = MANAGEMENT
    elif case == "multiple_owners":
        p.state.rows.append({"Id": "other", "UserId": "u2", "DeviceId": DEVICE})
    elif case == "ordinary_group":
        app.state.members.upsert(UID, "fixture", {"group_id": "standard", "status": "active"})
    elif case == "suspended":
        app.state.members.set_status(UID, case)
    elif case == "expired":
        # Exercise an expired effective entitlement, not a stored label:
        # whitelist's default non-billed policy legitimately ignores old dates.
        expired = {**app.state.members.get(UID), "state": "expired"}
        monkeypatch.setattr(app.state.members, "get", lambda _uid: expired)
    elif case == "missing_token":
        headers.pop("X-Emby-Token")
    elif case == "conflicting_token":
        headers["X-MediaBrowser-Token"] = "synthetic-conflict"
    elif case == "duplicate_token":
        headers = [*headers.items(), ("X-Emby-Token", PERSONAL)]
    elif case == "authority_down":
        p.state.authority_down = True
    p.lease()
    before = app.state.db.query("SELECT * FROM stream_leases")
    response = p.client.post(f"/api/playback/{event}", headers=headers, json=PAYLOAD)
    assert response.status_code == expected
    assert p.state.posts == []
    assert p.usage_report.await_count == 0
    assert app.state.db.query("SELECT * FROM stream_leases") == before


@pytest.mark.parametrize("scope", ["official", "ordinary_entry", "forged", "duplicate_entry"])
@pytest.mark.parametrize("ownerless", [False, True])
def test_nonrestricted_reports_retain_legacy_lookup_and_behavior(pipeline, scope, ownerless):
    p = pipeline
    app.state.members.upsert(UID, "fixture", {"group_id": "standard", "status": "active"})
    if ownerless:
        p.state.rows.append({"Id": "discovery", "DeviceId": DEVICE})
    headers = {"X-Emby-Token": PERSONAL, "X-Emby-Device-Id": DEVICE}
    if scope == "ordinary_entry":
        headers = p.headers("friend")
    elif scope == "forged":
        headers = {**p.headers(), "X-Mediadeck-Entry-Key": "synthetic-forged"}
    elif scope == "duplicate_entry":
        headers = [*p.headers().items(), ("X-Mediadeck-Entry", "vip")]
    personal = AsyncMock(side_effect=AssertionError("Legacy route must not use personal guard"))
    p.live.personal_user_for_token = personal
    for event in EVENTS:
        response = p.client.post(f"/api/playback/{event}", headers=headers, json=PAYLOAD)
        assert response.status_code == (401 if ownerless else 204)
    assert len(p.state.posts) == (0 if ownerless else 3)
    personal.assert_not_awaited()


def test_same_device_discovery_full_45_second_verified_watch_and_stop(pipeline):
    p = pipeline
    p.state.rows.append({"Id": "discovery", "DeviceId": DEVICE})
    p.lease()
    assert p.client.post("/api/playback/started", headers=p.headers(), json=PAYLOAD).status_code == 204
    app.state.usage._sample(copy.deepcopy(p.state.rows), p.state.clock, None)
    base = p.state.clock
    for second in (30, 45):
        p.state.clock = base + second
        response = p.client.post("/api/playback/progress", headers=p.headers(),
                                 json={**PAYLOAD, "PositionTicks": second * 10_000_000})
        assert response.status_code == 204
        app.state.usage._sample(copy.deepcopy(p.state.rows), p.state.clock, None)
    watch = app.state.stats.watch_summary(UID, now=p.state.clock)
    assert watch["recorded_seconds"] == watch["seconds_24h"] == 45
    assert app.state.db.one("SELECT observed FROM stream_leases WHERE user_id=?", (UID,))["observed"] == 1
    assert p.client.post("/api/playback/stopped", headers=p.headers(),
                         json={**PAYLOAD, "PositionTicks": 45 * 10_000_000}).status_code == 204
    assert app.state.db.one("SELECT 1 FROM stream_leases WHERE user_id=?", (UID,)) is None
    app.state.usage._sample(copy.deepcopy(p.state.rows), p.state.clock + 15, None)
    assert app.state.stats.watch_summary(UID, now=p.state.clock + 15)["recorded_seconds"] == 45
    assert len(p.state.posts) == 4
