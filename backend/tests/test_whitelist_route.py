"""Restricted route contract; synthetic identities with real production URI shapes."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode, urlsplit

import httpx
import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers
from test_telegram import _bot

from app.adapters.live import LiveEmby
from app.core.config import StreamNode
from app.core.errors import ConfigError
from app.modules.entries import PlaybackEntry, identify_entry, validate_entries
from app.modules.entry_proxy import friend_config
from app.modules.settings import normalize_playback_lines
from app.modules.signing import sign_url, user_tag
from app.modules.whitelist_route import WhitelistRoute, target

ENTRY = {"id": "vip", "origin": "https://vip.example.com", "proxy_key": "S" * 43,
         "whitelist_only": True}
GD = {"Id": "source-gd", "Path": "/media/Show/E01.mkv"}
MOBILE = {"Id": "source-mobile", "Path": "/cmcc/Show/E01.mkv"}


@pytest.fixture
def guard(tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({MOBILE["Id"]: {"item_ids": ["item42"]}}))
    rows = {"u-vip": {"emby_user_id": "u-vip", "group_id": "whitelist", "state": "active"},
            "u-normal": {"emby_user_id": "u-normal", "group_id": "standard", "state": "active"}}
    node = StreamNode(name="nc1", base_url="https://node.example.com", sign_secret="synthetic",
                      probe_url="http://127.0.0.1:9800/load",
                      pools=[{"name": "main", "emby_prefix": "/media", "url_prefix": "/s/main"},
                             {"name": "gd3", "emby_prefix": "/media-gd3", "url_prefix": "/s/gd3"}])
    state = SimpleNamespace(
        members=SimpleNamespace(get=rows.get, list=lambda **kw: list(rows.values())),
        emby=SimpleNamespace(personal_user_for_token=AsyncMock(return_value="u-vip")),
        settings_service=SimpleNamespace(
            integration_config=lambda: {"external_entries": [ENTRY], "emby_public_url": "https://emby.example.com"},
            emby_config=lambda: {"api_key": "configured-admin"}, nodes=lambda: [node]))
    return WhitelistRoute(state, str(registry))


def entry():
    return PlaybackEntry("vip", ENTRY["origin"], whitelist_only=True)


def cap_query(guard, kind="gd"):
    return {"MediaSourceId": GD["Id"], "PlaySessionId": "play42",
            "md_route": guard.mint(entry(), "u-vip", "item42", GD["Id"], "play42", kind)}


@pytest.mark.parametrize("value", ["//evil/x", "/a/../x", "/a/%2e%2e/x", "/a%00", "/a%7f",
                                   "/x?api_key=a&Api_Key=b", "/x?%61pi_key=a", "/x?md_route=a&md_route=b"])
def test_target_rejects_ambiguous_uri(value):
    with pytest.raises(HTTPException):
        target(value)


def test_entry_roundtrip_retains_policy_and_key():
    public = {k: v for k, v in ENTRY.items() if k != "proxy_key"}
    rows = validate_entries([public], [ENTRY])
    assert rows[0] == ENTRY
    assert validate_entries([{"id": "vip", "origin": ENTRY["origin"]}], rows)[0] == ENTRY
    assert identify_entry(Headers({"X-Mediadeck-Entry": "vip", "X-Mediadeck-Entry-Key": "S" * 43}), rows).whitelist_only
    with pytest.raises(ConfigError):
        friend_config(ENTRY, "https://emby.example.com", [])
    for value in (1, "true", None):
        with pytest.raises(ConfigError):
            validate_entries([{**public, "whitelist_only": value}], rows)


@pytest.mark.parametrize("member,visible", [(None, False), ({"group_id": "standard", "state": "active"}, False),
    ({"group_id": "whitelist", "state": "active"}, True), ({"group_id": "whitelist", "state": "expired"}, False)])
def test_bot_filter_and_setting_roundtrip(member, visible):
    lines = [{"label": "original", "url": "https://emby.example.com"},
             {"label": "VIP", "url": "https://vip.example.com", "whitelist_only": True}]
    assert normalize_playback_lines(lines) == lines
    text = asyncio.run(_bot(cfg={"playback_lines": lines, "playback_lines_show_load": False})._nodes_text(member))
    assert ("https://vip.example.com" in text) == visible
    assert "https://emby.example.com" in text


@pytest.mark.parametrize("headers,query", [({"X-Emby-Token": "caller"}, {}),
    ({"Authorization": 'MediaBrowser Token="caller"'}, {}), ({}, {"api_key": "caller"})])
def test_personal_identity(guard, headers, query):
    assert asyncio.run(guard.user(Headers(headers), query)) == "u-vip"


@pytest.mark.parametrize("headers,query", [({}, {}), ({"X-Emby-Token": "configured-admin"}, {}),
    ({"X-Emby-Token": "caller"}, {"api_key": "other"}),
    ({"X-Emby-Token": "caller", "Authorization": 'MediaBrowser Token="other"'}, {}),
    ({"Authorization": 'MediaBrowser Token="caller", Token="other"'}, {})])
def test_conflicting_or_management_identity_refused(guard, headers, query):
    with pytest.raises(HTTPException):
        asyncio.run(guard.user(Headers(headers), query))


def test_live_group_and_fault_closed(guard):
    rows = guard.state.members.list()
    rows[0]["state"] = "expired"
    with pytest.raises(HTTPException):
        asyncio.run(guard.user(Headers({"X-Emby-Token": "caller"}), {}))
    rows[0]["state"] = "active"
    guard.state.emby.personal_user_for_token = AsyncMock(return_value="u-normal")
    with pytest.raises(HTTPException):
        asyncio.run(guard.user(Headers({"X-Emby-Token": "caller", "X-Emby-Device-Id": "vip"}), {}))
    guard.state.emby.personal_user_for_token = AsyncMock(side_effect=RuntimeError("synthetic"))
    with pytest.raises(HTTPException) as error:
        asyncio.run(guard.user(Headers({"X-Emby-Token": "caller"}), {}))
    assert error.value.status_code == 503


def test_real_source_classification_and_gd3(guard):
    assert guard.source_kind("item42", GD) == "gd"
    assert guard.source_kind("item42", {"Id": "gd3", "Path": "/media-gd3/E01.mkv"}) == "gd"
    assert guard.source_kind("item42", MOBILE) == "mobile"
    for source in ({"Id": "unknown", "Path": "/cmcc/X.mkv"}, {"Id": "none", "Path": "/media/X.strm"}):
        with pytest.raises(HTTPException):
            guard.source_kind("item42", source)
    with pytest.raises(HTTPException):
        guard.source_kind("other-item", MOBILE)
    with pytest.raises(HTTPException):
        WhitelistRoute(guard.state, "/nonexistent/registry").source_kind("item42", GD)


def test_mixed_playback_sources_keep_mobile_on_original_and_gd_hls_new(guard):
    sources = []
    for src in (GD, MOBILE):
        query = {"MediaSourceId": src["Id"], "PlaySessionId": "play42", "api_key": "caller"}
        sources.append({**src, "TranscodingUrl": "/Videos/item42/master.m3u8?" + urlencode(query),
                        "DirectStreamUrl": "/Videos/item42/stream.mkv?Static=true&MediaSourceId=" + src["Id"]})
    data = {"PlaySessionId": "play42", "MediaSources": sources}
    out = guard.decorate(entry(), "u-vip", "item42", data)
    assert "md_route" not in str(data)
    gd, mobile = out["MediaSources"]
    assert urlsplit(gd["TranscodingUrl"]).hostname == "vip.example.com"
    path, query = target(urlsplit(gd["TranscodingUrl"]).path + "?" + urlsplit(gd["TranscodingUrl"]).query)
    assert guard.claims(entry(), "u-vip", path, query)["kind"] == "gd"
    assert urlsplit(mobile["TranscodingUrl"]).hostname == "emby.example.com"
    assert urlsplit(mobile["DirectStreamUrl"]).hostname == "emby.example.com"


@pytest.mark.parametrize("change", ["uid", "item", "source", "play", "expired", "tamper"])
def test_hls_binding_cannot_swap_source_or_identity(guard, change):
    query = cap_query(guard)
    uid, path = "u-vip", "/Videos/item42/hls1/main/0.ts"
    if change == "uid":
        uid = "u-normal"
    if change == "item":
        path = path.replace("item42", "other")
    if change == "source":
        query["MediaSourceId"] = MOBILE["Id"]
    if change == "play":
        query["PlaySessionId"] = "other"
    if change == "expired":
        query["md_route"] = guard.mint(entry(), uid, "item42", GD["Id"], "play42", "gd", now=1)
    if change == "tamper":
        query["md_route"] = "x" + query["md_route"][1:]
    with pytest.raises(HTTPException):
        guard.claims(entry(), uid, path, query)


def test_production_master_child_segment_shape_and_uri_attributes(guard):
    original = "/Videos/item42/main.m3u8?" + urlencode(cap_query(guard))
    manifest = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="hls1/key?PlaySessionId=play42"\n#EXTINF:6,\nhls1/main/0.ts?PlaySessionId=play42\n'
    text = guard.rewrite_playlist(entry(), "u-vip", original, manifest, "caller")
    segment = text.splitlines()[-1]
    parsed = urlsplit(segment)
    path, query = target(parsed.path + "?" + parsed.query)
    assert parsed.hostname == "vip.example.com" and query["api_key"] == "caller"
    assert guard.claims(entry(), "u-vip", path, query)["source"] == GD["Id"]
    assert 'URI="https://vip.example.com' in text
    for bad in ("https://evil.example/steal", "hls1/main/0.ts?MediaSourceId=source-mobile"):
        with pytest.raises(HTTPException):
            guard.rewrite_playlist(entry(), "u-vip", original, "#EXTM3U\n" + bad + "\n", "caller")


@pytest.mark.parametrize("uid", ["u-vip", "u-normal"])
def test_finished_node_signature_is_rechecked_against_live_group(guard, uid):
    signed = sign_url("https://node.example.com", "/s/main/剧 E01.mkv", "synthetic", 300,
                      utag=user_tag(uid), rate_bps=1000000, arg_digest="k", arg_expires="e")
    parsed = urlsplit(signed)
    wrapped = "/_n/nc1" + parsed.path + "?" + parsed.query
    if uid == "u-vip":
        assert guard.file_user(wrapped) == uid
        guard.state.members.get(uid)["state"] = "suspended"
    with pytest.raises(HTTPException):
        guard.file_user(wrapped)
    with pytest.raises(HTTPException):
        guard.file_user(wrapped.replace("r=1000000", "r=0"))


@pytest.mark.parametrize("keys_status,keys,sessions,expected", [
    (403, {}, [{"UserId": "u-vip"}], "u-vip"),
    (200, {"Items": [{"AccessToken": "caller"}]}, [{"UserId": "u-vip"}], None),
    (200, {"Items": []}, [{"UserId": "u-vip"}, {"UserId": "u-normal"}], None),
    (403, {}, [{"UserId": "u-vip"}, {}, {}], "u-vip"),
    (403, {}, [{}, {}], None),
    (403, {}, [{"UserId": "u-vip"}, "malformed"], None),
    (403, {}, [], None)])
def test_adapter_scoped_identity_rejects_shared_key_and_missing_owner(monkeypatch, keys_status, keys, sessions, expected):
    emby = LiveEmby(lambda: {"enabled": True, "url": "https://emby.example.com", "api_key": "configured-admin"})
    def upstream(request):
        assert request.headers["x-emby-token"] == "caller"
        if request.url.path.endswith("/Auth/Keys"):
            return httpx.Response(keys_status, json=keys)
        return httpx.Response(200, json=sessions)
    monkeypatch.setattr(emby, "_client", lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    assert asyncio.run(emby.personal_user_for_token("caller")) == expected
