"""Live adapter behavior with entirely local HTTP mock transports."""
# ruff: noqa: F811 - pytest injects fixtures using the imported names
import asyncio

import httpx
import pytest
from fastapi import HTTPException, Request
from test_stream_admission import (
    client,  # noqa: F401 - pytest fixture
    headers,
)

from app import main
from app.adapters.live import LiveEmby
from app.main import app


def adapter(monkeypatch, upstream):
    emby = LiveEmby(lambda: {"enabled": True, "url": "https://emby.example.invalid",
                            "api_key": "synthetic-configured-admin", "verify_ssl": True})
    monkeypatch.setattr(emby, "_client", lambda *_: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)))
    return emby


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_device_across_users_is_unresolved_independent_of_list_order(monkeypatch, reverse):
    rows = [{"Id": "s1", "UserId": "u1", "DeviceId": "shared"},
            {"Id": "s2", "UserId": "u2", "DeviceId": "shared"}]
    if reverse:
        rows.reverse()
    emby = adapter(monkeypatch, lambda request: httpx.Response(200, json=rows))
    assert asyncio.run(emby.user_for_token("synthetic-shared-key", "shared")) is None


@pytest.mark.parametrize("second_uid", ["u1", None])
def test_duplicate_device_requires_one_agreed_owner_on_every_match(monkeypatch, second_uid):
    rows = [{"Id": "s1", "UserId": "u1", "DeviceId": "shared"},
            {"Id": "s2", "UserId": second_uid, "DeviceId": "shared"}]
    emby = adapter(monkeypatch, lambda request: httpx.Response(200, json=rows))
    assert asyncio.run(emby.user_for_token("synthetic-caller", "shared")) == ("u1" if second_uid else None)


def test_shared_key_unique_device_and_single_owner_fallback_preserved(monkeypatch):
    rows = [{"Id": "s1", "UserId": "u1", "DeviceId": "a"},
            {"Id": "s2", "UserId": "u2", "DeviceId": "b"}]
    emby = adapter(monkeypatch, lambda request: httpx.Response(200, json=rows))
    assert asyncio.run(emby.user_for_token("synthetic-caller", "b")) == "u2"
    assert asyncio.run(emby.user_for_token("synthetic-caller")) is None
    rows.pop()
    assert asyncio.run(emby.user_for_token("synthetic-caller")) == "u1"


@pytest.mark.parametrize("status", [200, 204, 403, 503])
def test_head_empty_response_preserves_status_and_caller_identity(monkeypatch, status):
    def upstream(request):
        assert request.method == "HEAD"
        assert request.headers["x-emby-token"] == "synthetic-caller"
        assert request.url.params["DeviceId"] == "a"
        assert "synthetic-configured-admin" not in str(request.headers)
        return httpx.Response(status)

    emby = adapter(monkeypatch, upstream)
    assert asyncio.run(emby.playback_info("item42", "HEAD", {"X-Emby-Token": "synthetic-caller"},
                                        {"DeviceId": "a"}, None)) == (status, {})


@pytest.mark.parametrize("status", [200, 503])
def test_http_head_success_has_no_fake_binding_and_failure_releases_new_seat(client, monkeypatch, status):
    emby = adapter(monkeypatch, lambda request: httpx.Response(status))
    app.state.emby.playback_info = emby.playback_info
    response = client.head("/api/playback/info/item42", headers=headers("a"))
    assert response.status_code == status
    assert response.content == b""
    leases = app.state.db.query("SELECT * FROM stream_leases")
    if status == 200:
        assert len(leases) == 1 and leases[0]["play_id"] == ""
    else:
        assert leases == []


def test_get_empty_success_still_fails_closed_instead_of_inventing_metadata(monkeypatch):
    emby = adapter(monkeypatch, lambda request: httpx.Response(200))
    with pytest.raises(ValueError):
        asyncio.run(emby.playback_info("item42", "GET", {"X-Emby-Token": "caller"}, {}, None))


def test_playback_admission_checks_owner_and_item_concurrently(client, monkeypatch):
    owner_started = asyncio.Event()
    item_started = asyncio.Event()
    release = asyncio.Event()

    async def owner(token, device):
        assert (token, device) == ("tok:u1", "a")
        owner_started.set()
        await release.wait()
        return "u1"

    async def item(item_id, token):
        assert (item_id, token) == ("item42", "tok:u1")
        item_started.set()
        await release.wait()
        return True

    monkeypatch.setattr(app.state.emby, "user_for_token", owner)
    monkeypatch.setattr(app.state.emby, "verify_item_access", item)
    request = Request({"type": "http", "method": "GET", "path": "/Videos/item42/original",
                       "headers": [(b"x-emby-token", b"tok:u1"),
                                   (b"x-emby-device-id", b"a")], "query_string": b""})

    async def check():
        pending = asyncio.create_task(main._admit_playback(request, "item42", {}, reserve=False))
        try:
            await asyncio.wait_for(asyncio.gather(owner_started.wait(), item_started.wait()), 1)
        finally:
            release.set()
        assert await pending == "u1"

    asyncio.run(check())


def test_playback_admission_still_refuses_when_item_check_fails(client, monkeypatch):
    async def owner(token, device):
        return "u1"

    async def item(item_id, token):
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(app.state.emby, "user_for_token", owner)
    monkeypatch.setattr(app.state.emby, "verify_item_access", item)
    request = Request({"type": "http", "method": "GET", "path": "/Videos/item42/original",
                       "headers": [(b"x-emby-token", b"tok:u1"),
                                   (b"x-emby-device-id", b"a")], "query_string": b""})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main._admit_playback(request, "item42", {}, reserve=False))
    assert exc.value.status_code == 503
    assert app.state.db.query("SELECT * FROM stream_leases") == []
