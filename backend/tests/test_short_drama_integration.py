"""Preserve the authorized ca1 short-drama snapshot; all identities/network are isolated."""
# ruff: noqa: F811 - fixture imported from the existing admission contract
import asyncio
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from fastapi import HTTPException
from test_stream_admission import client, headers  # noqa: F401

from app.adapters.live import LiveEmby
from app.core.config import StreamNode
from app.main import app
from app.modules.short_drama import ShortDramaSources
from app.modules.signing import user_tag, verify
from app.modules.streams import StreamAdmission

MEDIA = "/s/short/hongguo/123456/234567/index.m3u8"
SOURCE = {"Id": "short-source", "Path": "https://catalogue.example.invalid/short.strm"}
ROW = {"node": "nc1", "media_path": MEDIA, "source_ids": [SOURCE["Id"]], "paths": [SOURCE["Path"]]}


@pytest.fixture
def short(client, tmp_path, monkeypatch):
    registry = tmp_path / "short-registry.json"
    registry.write_text(json.dumps({"version": 1, "items": {"item42": ROW}}))
    service = app.state.short_drama
    service.path = str(registry)
    node = StreamNode(name="nc1", base_url="https://node.example.invalid", sign_secret="synthetic-short-key",
                      probe_url="http://127.0.0.1:9800/load")
    selected = SimpleNamespace(node=node)
    monkeypatch.setattr(app.state.scheduler, "pick", lambda **kw: selected if not kw.get("predicate") or kw["predicate"](selected) else None)
    monkeypatch.setattr(app.state.playback, "_rate_resolver", AsyncMock(return_value=(1024, user_tag("u1"))))
    catalogue = AsyncMock(side_effect=lambda *args: (200, {"MediaSources": [dict(SOURCE)]}))
    monkeypatch.setattr(app.state.emby, "short_catalogue_info", catalogue, raising=False)
    original = AsyncMock(side_effect=AssertionError("registered shorts must not open Emby media"))
    monkeypatch.setattr(app.state.emby, "playback_info", original)

    async def describe(*args):
        service._metadata[MEDIA] = (0, 60.0)
        return 60.0

    monkeypatch.setattr(service, "describe", describe)
    return client, service, node, catalogue, original


def issued(short):
    c, _service, node, catalogue, original = short
    response = c.post("/api/playback/info/item42", headers=headers(), json={})
    assert response.status_code == 200, response.text
    data = response.json()
    assert re.fullmatch("[0-9a-f]{32}", data["PlaySessionId"])
    source = data["MediaSources"][0]
    address = urlsplit(source["DirectStreamUrl"])
    query = dict(parse_qsl(address.query))
    assert verify(address.path, query[node.sign_arg_digest], int(query[node.sign_arg_expires]),
                  node.sign_secret, rate_bps=int(query["r"]), utag=query["u"])
    assert source["SupportsDirectStream"] and not source["SupportsTranscoding"]
    assert source["Container"] == "m3u8" and source["RunTimeTicks"] == 600_000_000
    assert "TranscodingUrl" not in source
    assert catalogue.await_count == 1 and original.await_count == 0
    lease = app.state.db.one("SELECT * FROM stream_leases")
    assert lease["play_id"] == data["PlaySessionId"] and lease["short_path"] == address.path
    assert lease["session_id"] == "a" and lease["short_item"] == "item42"
    return data, {"p": address.path, **query}


def test_real_playbackinfo_and_node_session_share_original_lease(short):
    _, capability = issued(short)
    assert short[0].post("/api/playback/short-session", json=capability).status_code == 204
    assert len(app.state.db.query("SELECT * FROM stream_leases")) == 1


@pytest.mark.parametrize("change", ["signature", "tag", "play", "extra", "nonstring"])
def test_original_short_session_capability_cannot_be_forged(short, change):
    _, capability = issued(short)
    if change == "signature":
        capability[short[2].sign_arg_digest] = "bad"
    elif change == "tag":
        capability["u"] = user_tag("someone-else")
    elif change == "play":
        capability["p"] = capability["p"].replace(capability["p"].split("/")[-2], "f" * 32)
    elif change == "extra":
        capability["extra"] = "x"
    else:
        capability["r"] = 1024
    assert short[0].post("/api/playback/short-session", json=capability).status_code == 403


def test_redirect_reuses_deadline_and_stop_revokes_original_capability(short):
    data, capability = issued(short)
    c = short[0]
    redirect = c.get("/Videos/item42/stream.m3u8", headers=headers(), params={
        "Static": "true", "MediaSourceId": SOURCE["Id"], "PlaySessionId": data["PlaySessionId"]}, follow_redirects=False)
    assert redirect.status_code == 302, redirect.text
    query = dict(parse_qsl(urlsplit(redirect.headers["location"]).query))
    assert query[short[2].sign_arg_expires] == capability[short[2].sign_arg_expires]
    assert c.post("/api/playback/stopped", headers=headers(), json={"PlaySessionId": data["PlaySessionId"]}).status_code == 204
    assert c.post("/api/playback/short-session", json=capability).status_code == 403


@pytest.mark.parametrize("failure", ["metadata", "unsupported", "wrong-source"])
def test_failed_short_issue_releases_new_seat_without_origin_fallback(short, monkeypatch, failure):
    c, service, _, catalogue, original = short
    payload = {}
    if failure == "metadata":
        monkeypatch.setattr(service, "describe", AsyncMock(side_effect=HTTPException(503, "synthetic outage")))
        expected = 503
    elif failure == "unsupported":
        payload = {"EnableDirectStream": False}
        expected = 409
    else:
        catalogue.side_effect = None
        catalogue.return_value = (200, {"MediaSources": [{**SOURCE, "Id": "wrong"}]})
        expected = 403
    assert c.post("/api/playback/info/item42", headers=headers(), json=payload).status_code == expected
    assert app.state.db.query("SELECT * FROM stream_leases") == []
    assert original.await_count == 0


def test_short_binding_survives_admission_reinitialization_and_refuses_lost_session(short):
    _, capability = issued(short)
    app.state.streams = StreamAdmission(app.state.members, app.state.emby)
    short[1].admission = app.state.streams
    assert short[0].post("/api/playback/short-session", json=capability).status_code == 204
    app.state.emby.set_sessions([])
    assert short[0].post("/api/playback/short-session", json=capability).status_code == 403


@pytest.mark.parametrize("path", ["/Videos/item42/stream.mkv", "/Videos/item42/master.m3u8"])
def test_registered_short_never_uses_origin_transcoding_admission(short, path):
    response = short[0].get("/api/playback/admit", headers={**headers(), "X-Original-URI": path})
    assert response.status_code == 403


def test_registry_hot_reload_and_source_guard_do_not_redesign_normal_films(tmp_path):
    path = tmp_path / "registry.json"
    service = ShortDramaSources(str(path))
    normal = {"MediaSources": [{"Id": "film"}]}
    assert service.item("film") is None
    assert asyncio.run(service.decorate_info(None, "film", normal, None, {}, "", "")) is normal
    path.write_text(json.dumps({"version": 1, "items": {"item42": ROW}}))
    assert service.source("item42", SOURCE) == ROW
    with pytest.raises(HTTPException):
        service.source("item42", {**SOURCE, "Path": "https://other.example.invalid"})
    path.write_text('{"version": 999}')
    assert service.registry() == {} and service.item("film") is None


def test_live_short_catalogue_uses_caller_headers_not_configured_admin(monkeypatch):
    emby = LiveEmby(lambda: {"enabled": True, "url": "https://emby.example.invalid", "api_key": "synthetic-admin"})

    def upstream(request):
        assert request.method == "GET" and request.url.path == "/emby/Items"
        assert request.headers["x-emby-token"] == "synthetic-caller"
        assert "synthetic-admin" not in str(request.headers)
        assert "x-mediadeck-entry-key" not in request.headers
        assert request.url.params["Ids"] == "item42"
        return httpx.Response(200, json={"Items": [{"Id": "item42", "MediaSources": [SOURCE]}]})

    monkeypatch.setattr(emby, "_client", lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    code, data = asyncio.run(emby.short_catalogue_info("item42", {"X-Emby-Token": "synthetic-caller", "X-Mediadeck-Entry-Key": "synthetic-entry"}))
    assert code == 200 and data == {"MediaSources": [SOURCE]}
