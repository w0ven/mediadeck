"""Live adapter behavior with entirely local HTTP mock transports."""
# ruff: noqa: F811 - pytest injects fixtures using the imported names
import asyncio

import httpx
import pytest
from test_stream_admission import (
    client,  # noqa: F401 - pytest fixture
    headers,
)

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
