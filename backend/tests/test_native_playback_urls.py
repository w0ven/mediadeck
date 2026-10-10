"""Authenticated PlaybackInfo must produce usable URL-only native fetches.
Synthetic identities; no assumption about a particular proprietary client.
"""

# ruff: noqa: F811 - pytest fixtures imported from existing real-handler tests
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers
from test_external_entries import client  # noqa: F401
from test_whitelist_handlers import restricted  # noqa: F401
from test_whitelist_route import GD, MOBILE, entry, guard  # noqa: F401

from app.main import app
from app.modules.whitelist_route import target

CALLER = "synthetic-native-caller"


def metadata(source=GD, field="DirectStreamUrl", **query):
    values = {"MediaSourceId": source["Id"], "PlaySessionId": "play42", **query}
    endpoint = "stream.mkv?Static=true&" if field == "DirectStreamUrl" else "master.m3u8?"
    return {
        "PlaySessionId": "play42",
        "MediaSources": [{**source, field: "/Videos/item42/" + endpoint + urlencode(values)}],
    }


@pytest.mark.parametrize("source", [GD, MOBILE])
@pytest.mark.parametrize("field", ["DirectStreamUrl", "TranscodingUrl"])
def test_existing_caller_credential_carried_without_metadata_header_inheritance(
    guard, source, field
):
    before = metadata(source, field)
    out = guard.decorate(entry(), "u-vip", "item42", before, CALLER)
    assert before == metadata(source, field)
    parsed = urlsplit(out["MediaSources"][0][field])
    path, q = target(parsed.path + "?" + parsed.query)
    assert q["api_key"] == CALLER
    expected_host = "emby.example.com" if source is MOBILE else (
        None if field == "DirectStreamUrl" else "vip.example.com"
    )
    assert parsed.hostname == expected_host
    assert asyncio.run(guard.user(Headers(), q)) == "u-vip"
    if source is GD and field == "TranscodingUrl":
        assert guard.claims(entry(), "u-vip", path, q)["kind"] == "gd"
    if source is MOBILE:
        assert "md_route" not in q


@pytest.mark.parametrize(
    "key", ["api_key", "ApiKey", "apikey", "X-Emby-Token", "X-MediaBrowser-Token"]
)
def test_matching_aliases_become_one_canonical_credential(guard, key):
    out = guard.decorate(entry(), "u-vip", "item42", metadata(**{key: CALLER}), CALLER)
    q = parse_qs(urlsplit(out["MediaSources"][0]["DirectStreamUrl"]).query)
    assert q["api_key"] == [CALLER]
    assert (
        sum(k.lower() in ("api_key", "apikey", "x-emby-token", "x-mediabrowser-token") for k in q)
        == 1
    )


@pytest.mark.parametrize(
    "key",
    ["api_key", "ApiKey", "X-Emby-Token", "X-Emby-Authorization", "X-MediaBrowser-Authorization"],
)
def test_upstream_conflicting_credential_is_not_silently_replaced(guard, key):
    value = "other" if "authorization" not in key.lower() else 'MediaBrowser Token="other"'
    with pytest.raises(HTTPException) as e:
        guard.decorate(entry(), "u-vip", "item42", metadata(**{key: value}), CALLER)
    assert e.value.status_code == 503


def test_duplicate_authorization_component_and_operator_key_refused(guard):
    with pytest.raises(HTTPException):
        guard.decorate(
            entry(),
            "u-vip",
            "item42",
            metadata(**{"X-Emby-Authorization": 'MediaBrowser Token="a",Token="a"'}),
            CALLER,
        )
    with pytest.raises(HTTPException):
        guard.decorate(entry(), "u-vip", "item42", metadata(), "configured-admin")


def test_replayed_or_revoked_identity_and_ordinary_group_still_refused(guard):
    out = guard.decorate(entry(), "u-vip", "item42", metadata(), CALLER)
    parsed = urlsplit(out["MediaSources"][0]["DirectStreamUrl"])
    _, q = target(parsed.path + "?" + parsed.query)
    for owner in ("u-normal", None):
        guard.state.emby.personal_user_for_token = AsyncMock(return_value=owner)
        with pytest.raises(HTTPException):
            asyncio.run(guard.user(Headers(), q))


def test_header_only_actual_PlaybackInfo_handler_produces_native_URL(
    restricted, monkeypatch, tmp_path
):
    client, entry_headers = restricted
    app.state.members.upsert("u1", "fixture", {"group_id": "whitelist"})
    app.state.emby.set_sessions(
        [{"Id": "entry-session", "UserId": "u1", "DeviceId": "entry-client"}]
    )
    registry = tmp_path / "registry.json"
    registry.write_text("{}")
    app.state.whitelist_route.registry_path = str(registry)
    body = metadata()
    monkeypatch.setattr(
        app.state.emby, "playback_info", AsyncMock(return_value=(200, deepcopy(body)))
    )
    monkeypatch.setattr(
        app.state.emby, "personal_user_for_token", AsyncMock(return_value="u1"), raising=False
    )
    headers = {**entry_headers, "X-Emby-Token": CALLER, "X-Emby-Device-Id": "entry-client"}
    # nginx maps the public Emby endpoint to this existing Deck handler.
    r = client.post("/api/playback/info/item42", headers=headers, json={})
    assert r.status_code == 200
    url = r.json()["MediaSources"][0]["DirectStreamUrl"]
    parsed = urlsplit(url)
    assert not parsed.netloc and parsed.path == "/Videos/item42/stream.mkv"
    assert parse_qs(parsed.query)["api_key"] == [CALLER]
    # No token header on the subsequent native-player admission subrequest.
    h = {
        **entry_headers,
        "X-Original-Method": "GET",
        "X-Original-URI": parsed.path + "?" + parsed.query,
    }
    assert client.get("/api/access/route-admit", headers=h).status_code == 204
