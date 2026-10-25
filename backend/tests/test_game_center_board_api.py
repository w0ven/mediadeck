"""Board HTTP wire contract and viewer-specific SSE, with actual initData auth."""
import asyncio
import json
import time

import pytest
from test_game_center import gc as game_env  # noqa: F401
from test_game_center_api import BASE, headers
from test_game_center_api import api as api_env  # noqa: F401

from app.modules.game_center_api import stream
from app.modules.xiangqi_engine import Position


@pytest.fixture
def gc(request, monkeypatch):
    e = request.getfixturevalue('game_env')
    e.cfg.update(gomoku_enabled=True, xiangqi_enabled=True)
    monkeypatch.setattr('app.modules.game_center_board.secrets.randbelow', lambda _: 0)
    yield e
    e.s.board_robots.pool.shutdown(wait=True)


@pytest.fixture
def api(request):
    return request.getfixturevalue('api_env')


@pytest.mark.parametrize('game', ['gomoku', 'xiangqi'])
def test_board_http_contract_actions_and_private_legal_moves(api, gc, game):
    client, _ = api
    def post(path, who=9000, **body):
        return client.post(BASE+path, headers=headers(who), json=body)
    t = post('/tables', request_id='create_board_http', game=game, tier='novice').json()['table']
    tid = t['id']
    assert t['rules']['auto_play'] is False and t['seats'][0]['color'] is None
    assert post(f'/tables/{tid}/seat', 9001, request_id='seat_board_http').status_code == 200
    assert post(f'/tables/{tid}/ready', 9001, request_id='ready_board_http', ready=True).status_code == 200
    t = post(f'/tables/{tid}/start', request_id='start_board_http').json()['table']
    hand = t['hand']
    assert set(hand) == {'number', 'turn', 'current_player', 'finished', 'players', 'board', 'moves',
                         'last_move', 'clocks', 'in_check', 'draw_offer', 'legal_actions', 'result'} | (
                             {'legal_moves'} if game == 'xiangqi' else set())
    assert hand['clocks']['turn_deadline'] == t['turn_deadline']
    other = client.get(BASE+f'/tables/{tid}', headers=headers(9001)).json()['table']
    watcher = client.get(BASE+f'/tables/{tid}', headers=headers(9002)).json()['table']
    assert 'legal_moves' not in other['hand'] and 'legal_moves' not in watcher['hand']
    assert watcher['hand']['legal_actions'] == {}
    async def get_sse(uid):
        async def connected():
            return False
        events = stream(gc.s, tid, uid, lambda: True, time.time()+5, connected)
        try:
            data = await anext(events)
            return json.loads(next(line[6:] for line in data.splitlines() if line.startswith('data: ')))['table']
        finally:
            await events.aclose()
    mine, theirs, public = [asyncio.run(get_sse(uid)) for uid in ('u0', 'u1', 'u2')]
    assert mine['hand']['board'] == theirs['hand']['board'] == public['hand']['board']
    if game == 'xiangqi':
        assert mine['hand']['legal_moves'] == hand['legal_moves'] == Position.from_fen(hand['board']['fen']).legal_moves()
        assert len(hand['board']['fen'].split()) == 2
    assert 'legal_moves' not in theirs['hand'] and 'legal_moves' not in public['hand']
    body = {'request_id': 'move_board_http', 'hand_no': 1, 'turn': 1,
            'action': 'move', 'move': [7, 7] if game == 'gomoku' else 'h2e2'}
    response = post(f'/tables/{tid}/actions', **body)
    assert response.status_code == 200
    t = response.json()['table']
    assert t['hand']['turn'] == 2 and t['hand']['last_move']['move'] == body['move']
    repeat = post(f'/tables/{tid}/actions', **body)
    assert repeat.status_code == 200 and repeat.json()['table']['hand']['turn'] == 2
    stale = post(f'/tables/{tid}/actions', **{**body, 'request_id': 'stale_board_http'})
    assert stale.status_code == 409 and stale.json()['detail']['code'] == 'stale_turn'
    assert post(f'/tables/{tid}/auto', request_id='auto_board_http', enabled=True).status_code == 400


def test_practice_is_private_robot_level_validation_and_spectator_forbidden(api):
    client, _ = api
    for tier, level in [('practice', 'expert'), ('novice', 'easy')]:
        r = client.post(BASE+'/tables', headers=headers(), json={'game': 'gomoku', 'tier': tier,
            'robot_level': level, 'request_id': 'invalid_board_http_'+tier})
        assert r.status_code == 400 and r.json()['detail']['code'] == 'invalid'
    r = client.post(BASE+'/tables', headers=headers(), json={'game': 'gomoku', 'tier': 'practice',
        'robot_level': 'hard', 'request_id': 'practice_board_http'})
    t = r.json()['table']
    assert len(t['seats']) == 2 and t['seats'][1]['bot']
    assert t['rules']['robot_level'] == 'hard'
    assert client.get(BASE+f"/tables/{t['id']}", headers=headers(9001)).status_code == 404
