"""A policy patch that changes nothing must not be POSTed: each Emby policy write makes Emby recompute
that user's library access, and bulk enforcement of unchanged values stalls the whole server."""
import asyncio

import httpx

from app.adapters.live import LiveEmby


def _emby(policy: dict, posts: list):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/emby/Users/u1":
            return httpx.Response(200, json={"Id": "u1", "Policy": dict(policy)})
        if request.method == "POST" and request.url.path == "/emby/Users/u1/Policy":
            posts.append(request.read())
            return httpx.Response(204)
        return httpx.Response(404)

    emby = LiveEmby.__new__(LiveEmby)
    transport = httpx.MockTransport(handler)
    emby._conn = lambda: ("http://emby", {}, 5, True)
    emby._client = lambda timeout, verify: httpx.AsyncClient(transport=transport)
    emby._check = lambda r: r
    return emby


CURRENT = {"IsAdministrator": False, "IsDisabled": False, "RemoteClientBitrateLimit": 8000000,
           "EnableAllFolders": False, "EnabledFolders": ["b", "a"], "SimultaneousStreamLimit": 2}


def test_unchanged_patch_is_not_written():
    posts = []
    emby = _emby(CURRENT, posts)
    want = {"IsDisabled": False, "RemoteClientBitrateLimit": 8000000, "EnableAllFolders": False,
            "EnabledFolders": ["a", "b"], "SimultaneousStreamLimit": 2}
    assert asyncio.run(emby.apply_member_policy("u1", want))["status"] == "applied"
    assert asyncio.run(emby.apply_policy("u1", {"IsDisabled": False}))
    assert posts == []


def test_changed_value_is_written():
    posts = []
    emby = _emby(CURRENT, posts)
    assert asyncio.run(emby.apply_member_policy("u1", {"RemoteClientBitrateLimit": 4000000}))["status"] == "applied"
    assert asyncio.run(emby.apply_policy("u1", {"EnabledFolders": ["a"]}))
    assert len(posts) == 2


def test_bool_and_int_are_not_confused():
    posts = []
    emby = _emby({**CURRENT, "SimultaneousStreamLimit": 1}, posts)
    assert asyncio.run(emby.apply_policy("u1", {"SimultaneousStreamLimit": True}))
    assert len(posts) == 1


def test_admin_protection_still_applies_first():
    posts = []
    emby = _emby({**CURRENT, "IsAdministrator": True}, posts)
    assert asyncio.run(emby.apply_member_policy("u1", {"IsDisabled": False}))["status"] == "skipped_admin"
    assert posts == []
