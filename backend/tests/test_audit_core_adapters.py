"""Upstream shape failures must degrade honestly, not break the API."""
import httpx
import pytest

from app.adapters.live import LiveEmby, LiveProbe, probe_emby
from app.core.config import Settings
from app.core.errors import UpstreamError


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,allowed", [
    ({"Items": [{"Id": "target"}]}, True),
    ({"Items": [{"Id": "different"}]}, False),
    ({"Items": "wrong"}, False),
    ({"Items": [None]}, False),
    ([], False),
])
async def test_item_access_requires_the_requested_item(monkeypatch, payload, allowed):
    _transport(monkeypatch, payload)
    emby = LiveEmby(lambda: {"enabled": True, "url": "https://emby.invalid", "api_key": "test"})
    assert await emby.verify_item_access("target", "caller-token") is allowed


def _transport(monkeypatch, payload):
    factory = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: factory(transport=transport, **kwargs))


@pytest.mark.parametrize("payload", ["null", "123", "true", '"not-a-list"', "{}"])
def test_non_list_bootstrap_nodes_are_ignored(payload):
    assert Settings(stream_nodes=payload).nodes() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {"active_streams": None}, {"active_streams": []}])
async def test_invalid_probe_payload_reports_unavailable(monkeypatch, payload):
    _transport(monkeypatch, payload)
    assert (await LiveProbe().load("https://node.invalid/load"))["ok"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[], "wrong", 12])
async def test_invalid_emby_probe_shape_is_operator_error(monkeypatch, payload):
    _transport(monkeypatch, payload)
    with pytest.raises(UpstreamError):
        await probe_emby("https://emby.invalid", "test-key")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[1], {"User": "bad"}, {"User": [1]}])
async def test_invalid_auth_payload_fails_closed(monkeypatch, payload):
    _transport(monkeypatch, payload)
    emby = LiveEmby(lambda: {"enabled": True, "url": "https://emby.invalid", "api_key": "test"})
    assert await emby.authenticate_user("test", "password") is None
