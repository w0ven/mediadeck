"""Explicitly authorized Emby public media artwork, not a general image bypass."""

# ruff: noqa: F811 - imported pytest fixtures
import pytest
from fastapi import HTTPException
from test_external_entries import client  # noqa: F401
from test_whitelist_handlers import restricted  # noqa: F401

from app.modules.whitelist_route import public_bootstrap, target


@pytest.mark.parametrize("prefix", ["/Items", "/emby/Items", "/EMBY/items"])
@pytest.mark.parametrize("kind", ["Primary", "Backdrop", "Thumb", "Logo", "Banner", "Art", "Disc"])
@pytest.mark.parametrize("index", ["", "/0", "/12"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_exact_public_media_image_shapes(prefix, kind, index, method):
    path, _ = target(f"{prefix}/safe-id_42/Images/{kind}{index}?tag=synthetic&maxWidth=400")
    assert public_bootstrap(path, method)


@pytest.mark.parametrize(
    "uri",
    [
        "/Users/u1/Images/Primary",
        "/emby/Users/u1/Images/Primary",
        "/Items/item42",
        "/Items/item42/PlaybackInfo",
        "/Videos/item42/stream.mkv",
        "/Items/item42/Download",
        "/Videos/item42/source42/Subtitles/0/Stream.srt",
        "/Items/item42/Images/Chapter",
        "/Items/item42/Images/Private",
        "/Items/item42/Images/Primary/secret",
        "/Items/item42/Images/Primary/-1",
        "/Items/item42/Images/Primary/1/extra",
        "/Items/item42/Images/Primary/",
        "/Items/id.extra/Images/Primary",
        "/Items//Images/Primary",
        "/Items/" + ("a" * 129) + "/Images/Primary",
        "/Items/item42/Images/Primary/12345678901",
        "/Items/item42/Images/Primary%00",
        "/Items/item42/Images/Primary/../Private",
        "/Items/item42/Images/Primary/%2e%2e/Private",
        "/Items/item42%2fother/Images/Primary",
    ],
)
def test_no_adjacent_permission_expansion(uri):
    try:
        path, _ = target(uri)
    except HTTPException:
        return
    assert not public_bootstrap(path, "GET")
    assert not public_bootstrap(path, "HEAD")


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_media_art_read_only(method):
    assert not public_bootstrap("/emby/Items/item42/Images/Primary", method)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_real_admission_art_public_after_trusted_entry_only(restricted, method):
    client, h = restricted
    h = {
        **h,
        "X-Original-Method": method,
        "X-Original-URI": "/emby/Items/item42/Images/Primary?tag=synthetic&maxWidth=400",
    }
    assert client.get("/api/access/route-admit", headers=h).status_code == 204
    # An anonymous image request still cannot select or forge a trusted entry.
    for forged in (
        {},
        {**h, "X-Mediadeck-Entry-Key": "forged"},
        {**h, "X-Mediadeck-Entry": "unregistered"},
    ):
        assert client.get("/api/access/route-admit", headers=forged).status_code == 403
    assert (
        client.get("/api/access/route-admit", headers={**h, "Upgrade": "websocket"}).status_code
        == 401
    )


@pytest.mark.parametrize(
    "uri",
    [
        "/Users/u1/Images/Primary",
        "/Items/item42",
        "/Items/item42/PlaybackInfo",
        "/Videos/item42/stream.mkv",
        "/Items/item42/Download",
        "/Videos/item42/source42/Subtitles/0/Stream.srt",
        "/Items/item42/Images/Private",
    ],
)
def test_actual_handler_nonpublic_anonymous_refused(restricted, uri):
    client, h = restricted
    h = {**h, "X-Original-Method": "GET", "X-Original-URI": uri}
    assert client.get("/api/access/route-admit", headers=h).status_code == 401
