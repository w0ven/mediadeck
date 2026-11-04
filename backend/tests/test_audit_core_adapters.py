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


@pytest.mark.asyncio
async def test_create_user_uploads_default_avatar_when_configured(monkeypatch, tmp_path):
    import base64

    avatar_file = tmp_path / "default-user-avatar.png"
    raw_png = b"\x89PNG\r\n\x1a\nfake"
    avatar_file.write_bytes(raw_png)

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/emby/Users/New":
            return httpx.Response(200, json={"Id": "u-new", "Name": "alice"})
        if request.url.path == "/emby/Users/u-new/Images/Primary":
            return httpx.Response(204)
        return httpx.Response(404)

    factory = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: factory(transport=transport, **kwargs))

    emby = LiveEmby(
        lambda: {"enabled": True, "url": "https://emby.invalid", "api_key": "test-key"},
        default_avatar_path=str(avatar_file),
    )
    created = await emby.create_user("alice")
    assert created == {"Id": "u-new", "Name": "alice"}
    assert [r.url.path for r in seen] == ["/emby/Users/New", "/emby/Users/u-new/Images/Primary"]
    assert seen[1].headers["content-type"] == "image/png"
    assert seen[1].content == base64.b64encode(raw_png)

