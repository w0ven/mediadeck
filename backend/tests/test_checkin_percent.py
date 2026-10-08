"""Confirmed base-only percentages, actual HMAC/service/API/Bot and history preservation."""
import asyncio
import copy
import json

import pytest
from fastapi.testclient import TestClient
from test_economy import NOW, PPM, SECRET, parallel, proof
from test_economy import env as economy_env  # noqa: F401

from app.main import app
from app.modules.economy_rules import DEFAULT_CARDS, RULE_VERSION, day_bounds, draw, encode


@pytest.fixture
def env(request):
    return request.getfixturevalue('economy_env')


def matching_user(e, base):
    day = day_bounds(NOW)[0]
    uid = next(str(n) for n in range(10000) if draw(SECRET, str(n), day, 'base', 41) - 10 == base)
    e.members.upsert(uid, uid, {'group_id': 'standard'})
    proof(e.db, uid)
    return uid


def configure(e, tiers):
    e.registry.save('checkin', config={'streak_tiers': encode(tiers), 'double_ppm': PPM,
                                      'multiplier': 2, 'drops': '[]', 'holidays': '[]'})


@pytest.mark.parametrize('base', range(-10, 31))
def test_all_41_real_hmac_bases_floor_only_positive_and_not_lucky_multiplied(env, base):
    uid = matching_user(env, base)
    configure(env, [{'days': 1, 'percent': 15}])
    result = env.checkin.checkin(uid)
    expected = base * (2 if base > 0 else 1) + max(base, 0) * 15 // 100
    assert result['base'] == base and result['bonus'] == max(base, 0) * 15 // 100
    assert result['points'] == result['balance'] == expected
    assert result['streak_percent'] == 15 and result['calculation_version'] == 'base-percent-v2'
    assert result['rule_version'] == RULE_VERSION == 'watch-checkin-v1'
    if base == 20:
        assert expected == 43
    if base <= 0:
        assert result['bonus'] == 0 and result['points'] == base
    day = day_bounds(NOW)[0]
    assert result['rolls'] == {'lucky': draw(SECRET, uid, day, 'lucky', PPM),
                               'drop': draw(SECRET, uid, day, 'drop', PPM)}
    saved = env.db.one('SELECT * FROM checkins WHERE emby_user_id=?', (uid,))
    configure(env, [{'days': 1, 'percent': 10000}])
    assert env.checkin.checkin(uid)['saved_result'] == result
    assert env.db.one('SELECT * FROM checkins WHERE emby_user_id=?', (uid,)) == saved
    assert len(env.points.ledger(uid)) == 1


@pytest.mark.parametrize('prior,highest', [(0,0),(1,0),(2,5),(5,5),(6,15),(28,15),(29,50),(100,50)])
def test_only_highest_achieved_tier_no_sum_and_missed_day_resets(env, prior, highest):
    uid = matching_user(env, 20)
    if prior:
        day = day_bounds(NOW - 86400)[0]
        env.db.execute('INSERT INTO checkins(emby_user_id,day,streak,points,created_at) VALUES(?,?,?,?,?)',
                       (uid, day, prior, 1, NOW - 86400))
    configure(env, [{'days':1,'percent':0},{'days':3,'percent':5},{'days':7,'percent':15},{'days':30,'percent':50}])
    result = env.checkin.checkin(uid)
    assert result['streak'] == prior + 1 and result['streak_percent'] == highest
    assert result['points'] == 40 + 20 * highest // 100
    env.clock[0] = NOW + 2 * 86400
    proof(env.db, uid, now=env.clock[0])
    assert env.checkin.checkin(uid)['streak'] == 1


def test_stored_legacy_digits_read_compatible_no_read_side_write_save_canonical_and_history_unchanged(env):
    legacy = [{'days':1,'bonus':7},{'days':3,'bonus':16},{'days':30,'bonus':80}]
    raw = dict(env.registry.config('checkin'), streak_tiers=encode(legacy))
    env.registry._store.data['plugins']['checkin']['config'] = copy.deepcopy(raw)
    stored = copy.deepcopy(env.registry._store.data)
    old_result = {'ok':True,'base':20,'bonus':7,'points':47,'streak':1,'multiplier':2,
                  'rule_version':RULE_VERSION,'rule_snapshot':raw}
    env.db.execute('INSERT INTO checkins(emby_user_id,day,streak,points,created_at,result_json) VALUES(?,?,?,?,?,?)',
                   ('u',day_bounds(NOW)[0],1,47,NOW,encode(old_result)))
    env.points.add('u',47,'checkin')
    env.points.add('u',1000,'isolated.test')
    item = env.shop.create(dict(DEFAULT_CARDS[1],enabled=True))
    env.shop.redeem('u',item['id'])
    before = {table:env.db.query('SELECT * FROM '+table) for table in ('checkins','inventory','points_ledger','shop_orders')}
    canonical = [{'days':r['days'],'percent':r['bonus']} for r in legacy]
    assert json.loads(env.registry.config('checkin')['streak_tiers']) == canonical
    assert env.registry._store.data == stored
    assert env.checkin.checkin('u')['saved_result'] == old_result
    env.registry.save('checkin',config={'streak_tiers':encode(legacy)})
    once = copy.deepcopy(env.registry._store.data)
    assert json.loads(once['plugins']['checkin']['config']['streak_tiers']) == canonical
    env.registry.save('checkin',config={'streak_tiers':env.registry.config('checkin')['streak_tiers']})
    assert env.registry._store.data == once
    for table,rows in before.items():
        assert env.db.query('SELECT * FROM '+table) == rows
    for k,v in raw.items():
        if k != 'streak_tiers':
            assert env.registry.config('checkin')[k] == v


def test_percent_same_result_across_sixteen_sqlite_connections(env):
    uid = matching_user(env, 20)
    configure(env,[{'days':1,'percent':15}])
    result = parallel(env,lambda e:e.checkin.checkin(uid),n=16)
    assert sum(r['ok'] for r in result) == 1
    assert env.points.balance(uid) == 43 and len(env.points.ledger(uid)) == 1
    winner = next(r for r in result if r['ok'])
    assert all(r.get('saved_result',r) == winner for r in result)


def test_actual_api_legacy_percent_normalization_and_bot_response_43(monkeypatch):
    monkeypatch.setenv('MEDIADECK_CHECKIN_SECRET',SECRET)
    with TestClient(app) as client:
        auth = ('admin','change-me')
        clock = NOW
        uid = next(str(i) for i in range(10000) if draw(SECRET,str(i),day_bounds(clock)[0],'base',41)-10 == 20)
        app.state.members.upsert(uid,'isolated-percent',{'group_id':'standard'})
        app.state.members.bind_telegram(uid,'902')
        proof(app.state.db,uid,now=clock)
        monkeypatch.setattr('time.time',lambda:clock)
        response = client.post('/api/plugins/checkin',auth=auth,json={'enabled':True,'config':{
            'streak_tiers':encode([{'days':1,'bonus':15}]),'double_ppm':PPM,'multiplier':2,'holidays':'[]','drops':'[]'}})
        assert response.status_code == 200
        assert json.loads(response.json()['config']['streak_tiers']) == [{'days':1,'percent':15}]
        assert client.post('/api/plugins/checkin',auth=auth,json={'config':{
            'streak_tiers':encode([{'days':1,'percent':15,'bonus':15}])}}).status_code == 400
        texts=[]
        async def call(method,payload=None,**kwargs):
            texts.append((method,payload or {})); return {'message_id':800}
        monkeypatch.setattr(app.state.telegram,'_call',call)
        asyncio.run(app.state.telegram._checkin(902,10,app.state.members.get(uid)))
        assert app.state.points.balance(uid) == 43
        joined='\n'.join(p.get('text','') for m,p in texts)
        assert '+43' in joined and '连签奖励 <b>+3</b>' in joined and '幸运 ×2' in joined
        assert '15%' not in joined and '不参与翻倍' not in joined and '基础' not in joined
