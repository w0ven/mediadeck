"""Native clients may send a JSON object with Content-Type: text/plain."""
import json

import pytest
from test_stream_admission import client as client  # noqa: PLC0414
from test_stream_admission import headers

from app.main import app
from app.modules.playback import caller_device, caller_token


@pytest.mark.parametrize('event', ['started', 'progress', 'stopped'])
def test_text_plain_json_report_with_query_identity(client, event):
    app.state.members.set_overrides('u1', {'max_streams': 1})
    info = client.post('/api/playback/info/item42', headers=headers('a'), json={})
    play = info.json()['PlaySessionId']
    params = {'UserId': 'not-the-owner', 'X-Emby-Token': 'tok:u1',
              'X-Emby-Device-Id': 'a', 'reqformat': 'json'}
    report = client.post('/api/playback/' + event, params=params,
                        headers={'Content-Type': 'text/plain'},
                        content=json.dumps({'ItemId': 'item42', 'PlaySessionId': play, 'PositionTicks': 123}))
    assert report.status_code == 204
    lease = app.state.db.one("SELECT * FROM stream_leases WHERE session_id='a'")
    if event == 'stopped':
        assert lease is None
    else:
        assert lease['user_id'] == 'u1' and lease['observed'] == 1


def test_empty_query_report_and_invalid_shape(client):
    info = client.post('/api/playback/info/item42', headers=headers(), json={})
    play = info.json()['PlaySessionId']
    assert client.post('/api/playback/stopped', headers=headers(),
                       params={'PlaySessionId': play}).status_code == 204
    assert app.state.db.query('SELECT * FROM stream_leases') == []
    assert client.post('/api/playback/progress', headers=headers(), content='[]').status_code == 400
    assert client.post('/api/playback/progress', headers=headers(), content='not-json').status_code == 400


def test_query_emby_authorization_extracts_real_identity_not_userid():
    query = {'UserId': 'untrusted', 'X-Emby-Authorization':
             'MediaBrowser Token="synthetic-caller", DeviceId="synthetic-device"'}
    assert caller_token({}, query) == 'synthetic-caller'
    assert caller_device({}, query) == 'synthetic-device'
    assert caller_token({}, {'UserId': 'untrusted'}) == ''
    assert caller_token({'x-emby-token': 'header-token'}, query) == 'header-token'
    assert caller_device({'x-emby-device-id': 'header-device'}, query) == 'header-device'


def test_invalid_query_token_cannot_report_for_forged_user(client):
    assert client.post('/api/playback/progress', params={'UserId': 'u1',
        'X-Emby-Token': 'invalid-token', 'X-Emby-Device-Id': 'a'},
        headers={'Content-Type': 'text/plain'}, content='{"PlaySessionId":"forged"}').status_code == 401
    assert app.state.db.query('SELECT * FROM stream_leases') == []
