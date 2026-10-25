"""The actual Mini App initData dependency, HTTP contract and per-viewer SSE."""
import asyncio
import json
import logging
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_game_center import act, start
from test_game_center import gc as game_env  # noqa: F401
from test_miniapp import sign

from app.modules import miniapp
from app.modules.economy_rules import encode
from app.modules.game_center import GameCenter
from app.modules.game_center_api import StreamAccessFilter, StreamTokens, register_admin, stream

BASE = '/api/app/games'


@pytest.fixture
def gc(request):
    return request.getfixturevalue('game_env')


@pytest.fixture
def api(gc):
    application = FastAPI()
    application.state.members = gc.members
    application.state.game_center = gc.s
    application.state.telegram = gc.bot
    gc.bot.is_admin = lambda member: False
    miniapp.register(application)
    async def admin():
        raise HTTPException(401)
    register_admin(application, admin)
    gc.clock = time.time()
    with TestClient(application) as client:
        yield client, application


def headers(uid=9000, auth_date=None):
    return {'X-Telegram-Init-Data': sign({'id':uid, 'first_name':'公开昵称'}, token='123456:isolated-test', auth_date=auth_date)}


def create(client, key='create_http_0001'):
    response = client.post(BASE + '/tables', headers=headers(), json={'request_id':key,'game':'zjh','tier':'novice'})
    assert response.status_code == 200, response.text
    return response.json()['table']


@pytest.mark.parametrize('path', ['/lobby','/me','/wallet','/zjh/tiers','/tables/missing'])
def test_every_read_requires_actual_initdata(api, path):
    client, _ = api
    assert client.get(BASE+path).status_code == 401
    h = headers()
    h['X-Telegram-Init-Data'] += 'tampered'
    assert client.get(BASE+path,headers=h).status_code == 401
    response = client.get(BASE+path,headers=headers(auth_date=int(time.time())-90000))
    assert response.status_code == 401 and response.json()['detail']['code'] == 'expired'
    assert client.get(BASE+path,headers=headers(uid=999999)).status_code == 403


def test_http_catalog_balance_defaults_and_holdem_boundary(api):
    client, _ = api
    response = client.get(BASE+'/lobby',headers=headers())
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'private, no-store'
    lobby = response.json()
    assert lobby['wallet']['beans'] == 10000
    assert lobby['games'][0]['available'] and lobby['games'][1]['available']
    assert not lobby['games'][1]['enabled']
    assert len(lobby['games']) == 6
    assert len(client.get(BASE+'/zjh/tiers',headers=headers()).json()['tiers']) == 6
    assert len(client.get(BASE+'/holdem/tiers',headers=headers()).json()['tiers']) == 6
    response = client.post(BASE+'/tables',headers=headers(),json={'request_id':'holdem_00001','game':'holdem','tier':'novice','buy_in':100})
    assert response.status_code == 503 and response.json()['detail']['code'] == 'disabled'
    assert client.get('/api/game-center/cash?user_id=u0').status_code == 401


def test_http_flow_own_views_idempotency_and_stale_action(api):
    client, _ = api
    t = create(client)
    tid = t['id']
    assert create(client)['id'] == tid
    assert client.get(BASE+'/me',headers=headers()).json()['active_tables'][0]['table_id'] == tid
    assert client.get(BASE+f'/tables/{tid}',headers=headers(9001)).json()['table']['me']['seat'] is None
    assert client.post(BASE+f'/tables/{tid}/seat',headers=headers(9001),json={'request_id':'seat_http_001'}).status_code == 200
    assert client.post(BASE+f'/tables/{tid}/ready',headers=headers(9001),json={'request_id':'ready_http_001','ready':True}).status_code == 200
    started = client.post(BASE+f'/tables/{tid}/start',headers=headers(),json={'request_id':'start_http_001'}).json()['table']
    assert started['hand_no'] == 1
    assert started['me']['wallet']['points'] == 4950
    action = {'request_id':'look_http_001','hand_no':1,'turn':1,'action':'look'}
    mine = client.post(BASE+f'/tables/{tid}/actions',headers=headers(),json=action).json()['table']
    assert mine['hand']['players'][0]['cards'] is not None
    other = client.get(BASE+f'/tables/{tid}',headers=headers(9001)).json()['table']
    assert other['hand']['players'][0]['cards'] is None
    response = client.post(BASE+f'/tables/{tid}/actions',headers=headers(),json={**action,'action':'fold'})
    assert response.status_code == 409 and response.json()['detail']['code'] == 'idempotency_conflict'
    assert client.post(BASE+f'/tables/{tid}/actions',headers=headers(),json={**action,'request_id':'fold_http_001','action':'fold'}).status_code == 200
    response = client.post(BASE+f'/tables/{tid}/actions',headers=headers(),json={**action,'request_id':'stale_http_001','action':'call'})
    assert response.status_code == 409
    assert client.get(BASE+f'/tables/{tid}/hands/1/verify',headers=headers()).json()['settled']
    assert client.get(BASE+'/wallet',headers=headers()).json()['frozen'] == 0


@pytest.mark.parametrize('body', [{}, {'request_id':'bad'}, {'request_id':123}, {'request_id':'valid_123','ready':'true'}])
def test_write_parameter_validation(api, body):
    client, _ = api
    t = create(client)
    response = client.post(BASE+f"/tables/{t['id']}/ready",headers=headers(),json=body)
    assert response.status_code == 400
    assert set(response.json()['detail']) == {'code','message'}


def test_tokens_tampering_cross_table_expiry_and_restart():
    tokens = StreamTokens(b'a'*32)
    token, exp = tokens.issue('u0','9000','table',now=1000)
    assert tokens.verify(token,'table',now=1000)['uid'] == 'u0' and exp == 1120
    for bad, table, clock in [(token+'x','table',1000),(token,'other',1000),(token,'table',1120),('', 'table', 1000)]:
        with pytest.raises(HTTPException) as exc:
            tokens.verify(bad,table,now=clock)
        assert exc.value.status_code == 401
    with pytest.raises(HTTPException):
        StreamTokens(b'b'*32).verify(token,'table',now=1000)


def test_stream_http_token_is_not_initdata_and_lost_binding_rejected(api, gc):
    client, application = api
    t = create(client)
    tid = t['id']
    assert client.post(BASE+f'/tables/{tid}/stream-token').status_code == 401
    response = client.post(BASE+f'/tables/{tid}/stream-token',headers=headers())
    assert response.status_code == 200
    token = response.json()['token']
    assert client.get(BASE+f'/tables/{tid}/events?token=invalid').status_code == 401
    assert client.get(BASE+f'/tables/other/events?token={token}').status_code == 401
    assert client.get(BASE+'/wallet',headers={'X-Telegram-Init-Data':token}).status_code == 401
    gc.members.bind_telegram('u0','9100')
    assert client.get(response.json()['url']).status_code == 401
    application.state.game_center = GameCenter(gc.db,gc.members,gc.points,lambda:gc.cfg,lambda:True)
    assert client.get(response.json()['url']).status_code == 401


def test_sse_first_snapshot_changes_keepalive_privacy_and_unsubscribe(gc):
    t = start(gc)
    async def disconnected():
        return False
    async def run():
        a = stream(gc.s,t['id'],'u0',lambda:True,time.time()+10,disconnected,keepalive=.01)
        b = stream(gc.s,t['id'],'u1',lambda:True,time.time()+10,disconnected,keepalive=.01)
        first = await anext(a)
        assert 'event: snapshot' in first and 'id: ' in first
        await anext(b)
        act(gc,t,'look')
        own, rival = await anext(a), await anext(b)
        def payload(frame):
            return json.loads(frame.split('data: ',1)[1])['table']['hand']
        assert payload(own)['players'][0]['cards'] is not None
        assert payload(rival)['players'][0]['cards'] is None
        assert 'salt' not in rival and 'private-login' not in rival
        assert await anext(a) == ': keepalive\n\n'
        await a.aclose()
        await b.aclose()
        assert gc.s._listeners == {}
    asyncio.run(run())


def test_sse_token_lifetime_and_membership_revocation_close_stream(gc):
    t = start(gc)
    async def disconnected():
        return False
    async def run():
        valid = [True]
        s = stream(gc.s,t['id'],'u0',lambda:valid[0],time.time()+10,disconnected,keepalive=.01)
        await anext(s)
        valid[0] = False
        gc.s.notify(t['id'])
        with pytest.raises(StopAsyncIteration):
            await anext(s)
        assert not gc.s._listeners
        expired = stream(gc.s,t['id'],'u0',lambda:True,time.time()-.1,disconnected)
        with pytest.raises(StopAsyncIteration):
            await anext(expired)
        assert not gc.s._listeners
    asyncio.run(run())


def test_sse_connection_limit_headers_and_release(api):
    client, application = api
    t = create(client)
    token = client.post(BASE+f"/tables/{t['id']}/stream-token", headers=headers()).json()['token']
    endpoint = next(route.endpoint for route in application.routes if getattr(route, 'path', '') == BASE+'/tables/{table_id}/events')
    async def disconnected():
        return False
    request = SimpleNamespace(is_disconnected=disconnected)
    async def run():
        responses = []
        for _ in range(3):
            response = await endpoint(t['id'], request, token)
            assert response.headers['x-accel-buffering'] == 'no'
            assert response.headers['cache-control'] == 'no-store'
            assert 'snapshot' in await anext(response.body_iterator)
            responses.append(response)
        with pytest.raises(HTTPException) as exc:
            await endpoint(t['id'], request, token)
        assert exc.value.status_code == 429
        for response in responses:
            await response.body_iterator.aclose()
        again = await endpoint(t['id'], request, token)
        await anext(again.body_iterator)
        await again.body_iterator.aclose()
    asyncio.run(run())


def test_stream_access_logs_strip_token_only_for_game_path():
    record = logging.LogRecord('uvicorn.access', logging.INFO, '', 0, '%s %s %s %s %s',
                               ('local', 'GET', BASE+'/tables/table/events?token=secret&x=1', '1.1', 200), None)
    assert StreamAccessFilter().filter(record)
    assert 'secret' not in record.getMessage() and 'token=[redacted]&x=1' in record.getMessage()


def test_http_out_of_turn_look_is_available_and_duplicate_safe(api):
    client, _ = api
    t = create(client)
    tid = t['id']
    for path, body in [('seat', {'request_id':'seat_look_001'}), ('ready', {'request_id':'ready_look_001','ready':True})]:
        assert client.post(BASE+f'/tables/{tid}/{path}', headers=headers(9001), json=body).status_code == 200
    t = client.post(BASE+f'/tables/{tid}/start',headers=headers(),json={'request_id':'start_look_001'}).json()['table']
    other = client.get(BASE+f'/tables/{tid}',headers=headers(9001)).json()['table']
    assert other['hand']['legal_actions'] == {'look':{}}
    body = {'request_id':'other_look_001','hand_no':1,'turn':1,'action':'look'}
    mine = client.post(BASE+f'/tables/{tid}/actions',headers=headers(9001),json=body).json()['table']
    assert mine['hand']['players'][1]['cards'] is not None and mine['turn_deadline'] == t['turn_deadline']
    assert client.post(BASE+f'/tables/{tid}/actions',headers=headers(9001),json=body).status_code == 200
    assert client.get(BASE+f'/tables/{tid}',headers=headers()).json()['table']['hand']['players'][1]['cards'] is None
    response = client.post(BASE+f'/tables/{tid}/actions',headers=headers(),json={**body,'request_id':'invalid_act_001','action':[]})
    assert response.status_code == 400


def test_deep_link_public_preview_cannot_see_practice_or_private_ids(api):
    client, _ = api
    response = client.post(BASE+'/tables',headers=headers(),json={'request_id':'practice_001','game':'zjh','tier':'practice'})
    tid = response.json()['table']['id']
    assert client.get(BASE+f'/tables/{tid}',headers=headers(9001)).status_code == 404
    assert client.post(BASE+f'/tables/{tid}/stream-token',headers=headers(9001)).status_code == 404
    public = client.get(BASE+'/zjh/tiers',headers=headers(9001)).json()
    assert tid not in encode(public) and 'u0' not in encode(public)


def test_holdem_http_contract_flow_and_private_sse(api):
    client, application = api
    service = application.state.game_center
    service.config()['holdem_enabled'] = True
    counter = 0
    def post(path, uid=9000, **body):
        nonlocal counter
        counter += 1
        body.setdefault('request_id', f'holdem_http_{counter:04}')
        response = client.post(BASE+path, headers=headers(uid), json=body)
        assert response.status_code == 200, response.text
        return response.json()
    assert client.post(BASE+'/tables', json={'game':'holdem','tier':'novice','buy_in':100}).status_code == 401
    t = post('/tables', game='holdem', tier='novice', buy_in=100)['table']
    tid = t['id']
    assert t['phase'] == 'waiting' and t['seats'][0]['stack'] == 100 and t['button'] is None
    response = post('/quick-start', game='holdem', tier='novice', buy_in=100)
    assert not response['created'] and response['table']['id'] == tid
    assert post(f'/tables/{tid}/top-up', amount=10, request_id='http_topup_idem')['table']['seats'][0]['stack'] == 110
    assert post(f'/tables/{tid}/top-up', amount=10, request_id='http_topup_idem')['table']['seats'][0]['stack'] == 110
    post(f'/tables/{tid}/seat', uid=9001, buy_in=100, seat=2)
    post(f'/tables/{tid}/away', uid=9001, enabled=True)
    response = client.post(BASE+f'/tables/{tid}/start', headers=headers(), json={'request_id':'not_enough_holdem'})
    assert response.status_code == 409 and response.json()['detail']['code'] == 'not_ready'
    post(f'/tables/{tid}/away', uid=9001, enabled=False)
    t = post(f'/tables/{tid}/start')['table']
    assert t['hand']['turn'] == 1 and t['hand']['engine']['small_blind_seat'] == 0
    assert t['hand']['legal_actions'] == t['hand']['engine']['legal_actions']
    assert 'hole' in t['hand']['engine']['seats'][0] and 'hole' not in t['hand']['engine']['seats'][1]
    assert client.get(BASE+f'/tables/{tid}/hands/1/verify', headers=headers()).json()['settled'] is False
    token = client.post(BASE+f'/tables/{tid}/stream-token', headers=headers(9001)).json()['token']
    assert service.stream_tokens.verify(token, tid)['uid'] == 'u1'
    async def connected():
        return False
    async def run():
        it = stream(service, tid, 'u1', lambda: True, time.time()+20, connected)
        message = await anext(it)
        view = json.loads(message.split('data: ')[1])['table']
        assert 'hole' not in view['hand']['engine']['seats'][0]
        assert 'hole' in view['hand']['engine']['seats'][1]
        await it.aclose()
    asyncio.run(run())
    t = post(f'/tables/{tid}/auto', enabled=True)['table']
    original_deadline = t['turn_deadline']
    t = post(f'/tables/{tid}/auto', enabled=False)['table']
    assert t['turn_deadline'] == original_deadline
    action = {'hand_no':1, 'turn':1, 'action':'raise', 'amount':4, 'request_id':'http_holdem_raise'}
    t = post(f'/tables/{tid}/actions', **action)['table']
    assert t['hand']['turn'] == 2
    assert post(f'/tables/{tid}/actions', **action)['table']['hand']['turn'] == 2
    response = client.post(BASE+f'/tables/{tid}/actions',headers=headers(),json={**action,'request_id':'stale_holdem_001'})
    assert response.status_code == 409 and response.json()['detail']['code'] == 'stale_turn'
    t = post(f'/tables/{tid}/actions', uid=9001, hand_no=1, turn=2, action='fold')['table']
    assert t['phase'] == 'between' and t['hand'] is None and t['last_hand']['finished']
    assert t['last_hand']['result'] == t['last_hand']['engine']['results']
    assert client.get(BASE+f'/tables/{tid}/hands/1/verify', headers=headers()).json()['settled'] is True
    post(f'/tables/{tid}/leave')
    t = post(f'/tables/{tid}/leave', uid=9001)['table']
    assert t['phase'] == 'closed'
    assert client.get(BASE+'/wallet',headers=headers()).json()['frozen'] == 0
    assert client.get(BASE+'/me',headers=headers()).json()['active_tables'] == []
