"""Unobserved legacy starts and real start/progress lifecycle regression."""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from test_stream_admission import client as client  # noqa: PLC0414 - pytest fixture re-export
from test_stream_admission import headers, row
from test_stream_admission import stack as stack  # noqa: PLC0414 - pytest fixture re-export

from app.main import app
from app.modules.streams import StreamAdmission


def test_pending_deadline_and_restart_do_not_extend_on_retry(stack, monkeypatch):
    members, emby, guard = stack
    members.set_overrides('u1', {'max_streams': 1})
    clock = [1000.0]
    monkeypatch.setattr('app.modules.streams.time.time', lambda: clock[0])
    async def check():
        assert (await guard.inspect('u1', 'a')).allowed
        clock[0] = 1119
        assert (await guard.inspect('u1', 'a')).allowed
        restarted = StreamAdmission(members, emby)
        assert (await restarted.inspect('u1', 'b')).reason == 'over-limit'
        clock[0] = 1120
        assert (await restarted.inspect('u1', 'b')).allowed
        assert (await restarted.inspect('u1', 'a')).reason == 'over-limit'
    asyncio.run(check())


@pytest.mark.parametrize('paused', [True, False])
def test_pending_deadline_never_discards_real_play_or_pause(stack, monkeypatch, paused):
    members, emby, guard = stack
    members.set_overrides('u1', {'max_streams': 1})
    async def check():
        assert (await guard.inspect('u1', 'a')).allowed
        emby.set_sessions([row('a', playing=True, paused=paused), row('b')])
        monkeypatch.setattr('app.modules.streams.time.time', lambda: 9999999999)
        assert (await guard.inspect('u1', 'b')).reason == 'over-limit'
        assert (await guard.inspect('u1', 'a')).allowed
    asyncio.run(check())


def test_legacy_pending_gets_one_grace_period_not_per_restart(stack, monkeypatch):
    members, emby, guard = stack
    members.set_overrides('u1', {'max_streams': 1})
    guard._db.execute("INSERT INTO stream_leases(user_id,session_id,observed,play_id) VALUES ('u1','a',0,'legacy')")
    monkeypatch.setattr('app.modules.streams.time.time', lambda: 1000)
    StreamAdmission(members, emby)
    assert guard._db.one("SELECT pending_at FROM stream_leases WHERE session_id='a'")['pending_at'] == 1000
    monkeypatch.setattr('app.modules.streams.time.time', lambda: 1120)
    restarted = StreamAdmission(members, emby)
    assert asyncio.run(restarted.inspect('u1', 'b')).allowed


@pytest.mark.parametrize('event', ['started', 'progress'])
def test_event_observes_play_without_another_admission(client, event):
    app.state.members.set_overrides('u1', {'max_streams': 1})
    r = client.post('/api/playback/info/item42', headers=headers('a'), json={})
    play = r.json()['PlaySessionId']
    assert client.post('/api/playback/' + event, headers=headers('a'),
                       json={'ItemId': 'item42', 'PlaySessionId': play}).status_code == 204
    assert app.state.db.one("SELECT observed FROM stream_leases WHERE session_id='a'")['observed'] == 1
    # No further admission while it plays, and no Stopped from a crashed client.
    app.state.emby.set_sessions([row('a'), row('b')])
    assert client.post('/api/playback/info/item42', headers=headers('b'), json={}).status_code == 200


def test_progress_failure_old_play_and_other_user_do_not_observe(stack):
    _, _, guard = stack
    async def check():
        assert (await guard.issue_info('u1', 'a', '', AsyncMock(return_value=(200, {'PlaySessionId': 'new'}))))[0].allowed
        for uid, payload, status in [('u1', {'PlaySessionId': 'old'}, 204),
                                     ('other', {'PlaySessionId': 'new'}, 204),
                                     ('u1', {'PlaySessionId': 'new'}, 503),
                                     ('u1', {}, 204),
                                     ('u1', {'PlaySessionId': 'new', 'SessionId': 'b'}, 204)]:
            assert await guard.report_activity(uid, payload, AsyncMock(return_value=status)) == status
            assert guard._db.one("SELECT observed FROM stream_leases WHERE session_id='a'")['observed'] == 0
        await guard.report_activity('u1', {'PlaySessionId': 'new'}, AsyncMock(return_value=204))
        assert guard._db.one("SELECT observed FROM stream_leases WHERE session_id='a'")['observed'] == 1
    asyncio.run(check())


def test_unsolicited_activity_cannot_create_or_extend_seat(stack):
    _, _, guard = stack
    asyncio.run(guard.report_activity('u1', {'PlaySessionId': 'fake'}, AsyncMock(return_value=204)))
    assert guard._db.query('SELECT * FROM stream_leases') == []


@pytest.mark.parametrize('failure', [503, RuntimeError('upstream'), asyncio.CancelledError()])
def test_failed_info_releases_only_its_new_reservation(stack, failure):
    _, _, guard = stack
    issue = AsyncMock(side_effect=failure) if isinstance(failure, BaseException) else AsyncMock(return_value=(failure, {}))
    async def check():
        try:
            await guard.issue_info('u1', 'a', '', issue)
        except (RuntimeError, asyncio.CancelledError) as exc:
            assert exc is failure
        assert guard._db.query('SELECT * FROM stream_leases') == []
        await guard.issue_info('u1', 'a', '', AsyncMock(return_value=(200, {'PlaySessionId': 'current'})))
        try:
            await guard.issue_info('u1', 'a', '', issue)
        except (RuntimeError, asyncio.CancelledError) as exc:
            assert exc is failure
        assert guard._db.one("SELECT play_id FROM stream_leases WHERE session_id='a'")['play_id'] == 'current'
    asyncio.run(check())


def test_http_info_failure_has_no_pre_reservation(client):
    app.state.emby.playback_info = AsyncMock(return_value=(503, {}))
    assert client.post('/api/playback/info/item42', headers=headers('a'), json={}).status_code == 503
    assert app.state.db.query('SELECT * FROM stream_leases') == []


def test_http_progress_auth_and_delivery_failure(client):
    assert client.post('/api/playback/progress', json={}).status_code == 401
    r = client.post('/api/playback/info/item42', headers=headers('a'), json={})
    play = r.json()['PlaySessionId']
    app.state.emby.report_playback = AsyncMock(return_value=503)
    assert client.post('/api/playback/progress', headers=headers('a'), json={'PlaySessionId': play}).status_code == 503
    assert app.state.db.one("SELECT observed FROM stream_leases WHERE session_id='a'")['observed'] == 0


def test_adapter_event_preserves_caller_identity_and_body(monkeypatch):
    from app.adapters.live import LiveEmby
    seen = []
    def upstream(request):
        seen.append(request)
        assert request.headers['authorization'] == 'MediaBrowser Token="caller", DeviceId="a"'
        assert 'x-emby-token' not in request.headers
        assert request.url.params['DeviceId'] == 'a'
        assert json.loads(request.content) == {'PlaySessionId': 'p', 'ItemId': 'i', 'IsPaused': True}
        return httpx.Response(204)
    adapter = LiveEmby(lambda: {'enabled': True, 'url': 'https://emby.example.invalid', 'api_key': 'admin'})
    monkeypatch.setattr(adapter, '_client', lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    async def check():
        for event in ['started', 'progress']:
            assert await adapter.report_playback(event, {'Authorization': 'MediaBrowser Token="caller", DeviceId="a"'},
                {'DeviceId': 'a'}, {'PlaySessionId': 'p', 'ItemId': 'i', 'IsPaused': True}) == 204
    asyncio.run(check())
    assert [r.url.path for r in seen] == ['/emby/Sessions/Playing', '/emby/Sessions/Playing/Progress']
