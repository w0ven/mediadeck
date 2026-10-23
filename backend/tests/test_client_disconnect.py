"""A caller that disconnects mid-body is a client abort, not a server error.

The media entrance streams request bodies (no request buffering). When an
HTTP/2 player resets its stream while a lifecycle report is still uploading,
the proxy ends the upstream request early and the ASGI server reports
``http.disconnect`` instead of the rest of the body. Reading the body then
raises ``ClientDisconnect``; it must end the request quietly, never surface as
"Exception in ASGI application", and never forward a partial report upstream.
"""
import logging

import pytest
from test_stream_admission import client as client  # noqa: PLC0414
from test_stream_admission import headers

from app.main import app


def _disconnecting_call(path):
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "2",
        "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(),
        "root_path": "", "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"x-emby-token", b"tok:u1"),
                    (b"x-emby-device-id", b"a"), (b"content-type", b"application/json"),
                    (b"content-length", b"64")],
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80), "state": {},
    }

    async def run():
        await app(scope, receive, send)

    return run, sent


@pytest.mark.parametrize("event", ["started", "progress", "stopped"])
def test_report_body_disconnect_is_not_an_unhandled_error(client, event, monkeypatch, caplog):
    info = client.post("/api/playback/info/item42", headers=headers("a"), json={})
    assert info.status_code == 200
    before = app.state.db.one("SELECT * FROM stream_leases WHERE session_id='a'")

    async def never(*_args, **_kwargs):
        raise AssertionError("a partial report must not be forwarded upstream")

    monkeypatch.setattr(app.state.emby, "report_playback", never)
    run, sent = _disconnecting_call("/api/playback/" + event)
    with caplog.at_level(logging.WARNING):
        client.portal.call(run)  # raised ClientDisconnect before the fix

    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert all(m["status"] == 400 for m in starts)
    after = app.state.db.one("SELECT * FROM stream_leases WHERE session_id='a'")
    assert dict(after) == dict(before)  # no observation, no release
    assert any("disconnected" in r.getMessage() for r in caplog.records)
    assert not any("tok:u1" in r.getMessage() for r in caplog.records)


def test_complete_reports_still_work_after_the_fix(client):
    info = client.post("/api/playback/info/item42", headers=headers("a"), json={})
    play = info.json()["PlaySessionId"]
    r = client.post("/api/playback/progress", headers=headers("a"),
                    json={"ItemId": "item42", "PlaySessionId": play, "PositionTicks": 1})
    assert r.status_code == 204
    lease = app.state.db.one("SELECT * FROM stream_leases WHERE session_id='a'")
    assert lease["observed"] == 1
