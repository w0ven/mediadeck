"""Range rules, actual admin persistence and original-card handlers; isolated only."""
import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_scratch9 import FIXED_TIERS, card, confirm, create, select, svc
from test_scratch9 import env as scratch_env  # noqa: F401
from test_scratch9_group_confirmation import answers, click
from test_tg_interaction_context import GROUP

from app.core.db import Database
from app.main import app
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.play_money import PlayError
from app.modules.points import PointsService
from app.modules.scratch9 import (
    DEFAULTS,
    DRAW_SIZE,
    TIERS,
    Scratch9Service,
    configuration,
    max_reward,
    reward,
)
from app.modules.scratch9_view import render, view

ADMIN = ('admin', 'change-me')


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def env(request):
    e = request.getfixturevalue('scratch_env')
    e.services.registry.save('scratch9', config={'reward_tiers': DEFAULTS['reward_tiers']})
    return e


def sequence(values):
    stream = iter(values)
    calls = []
    def rng(n):
        calls.append(n)
        return next(stream)
    return rng, calls


def test_new_defaults_exact_weights_expectation_and_uniform_mapping():
    cfg = configuration({})
    assert cfg['cost'] == 30 and cfg['per_person'] == 1 and cfg['schedule_times'] == '12:00,20:00'
    tiers = json.loads(cfg['reward_tiers'], parse_float=Decimal)
    assert tiers == [{**t, 'probability': Decimal(str(t['probability']))} for t in TIERS]
    assert sum(Decimal(str(t['probability'])) for t in tiers) == 100
    expectation = sum(Decimal(str(t['probability']))*(t['min']+t['max'])/200 for t in tiers)
    assert expectation == Decimal('28.673')
    assert [(t['min'], t['max']) for t in tiers] == [(0,0),(1,30),(31,50),(51,100),(101,200),(201,400),(401,888)]
    # Same tier, different secure-random offsets: real integers, not a fixed representative.
    values = []
    for offset in (0, 9, 29):
        rng, calls = sequence([30000, offset])
        values.append(reward(cfg, rng))
        assert calls == [DRAW_SIZE, 30]
    assert values == [1, 10, 30]


@pytest.mark.parametrize('index', range(7))
def test_every_tier_cumulative_boundary_and_both_closed_interval_endpoints(index, monkeypatch):
    cfg = configuration({})
    low = sum(int(Decimal(str(t['probability']))*10000) for t in TIERS[:index])
    width = int(Decimal(str(TIERS[index]['probability']))*10000)
    tier = TIERS[index]
    size = tier['max']-tier['min']+1
    for draw in (low, low+width-1):
        for offset, expected in ((0,tier['min']), (size-1,tier['max'])):
            rng, calls = sequence([draw, offset])
            # Exercise the default production RNG selection too: no injected
            # randbelow parameter; monkeypatch is strictly inside this isolated test.
            monkeypatch.setattr('secrets.randbelow', rng)
            assert reward(cfg) == expected
            assert calls == [1000000, size]


INVALID = [
    [{'min':-1,'max':0,'probability':100}],
    [{'min':0,'max':1000001,'probability':100}],
    [{'min':2,'max':1,'probability':100}],
    [{'min':True,'max':2,'probability':100}],
    [{'min':1,'max':2.5,'probability':100}],
    [{'min':'1','max':2,'probability':100}],
    [{'min':0,'max':30,'probability':50},{'min':30,'max':50,'probability':50}],
    [{'min':0,'max':50,'probability':50},{'min':20,'max':30,'probability':50}],
    [{'min':0,'max':0,'probability':50},{'min':0,'max':0,'probability':50}],
    [{'min':0,'max':30,'probability':99.9999}],
    [{'min':0,'max':30,'probability':100.00001}],
    [{'min':0,'max':30,'probability':-1},{'min':31,'max':50,'probability':101}],
    [{'min':0,'max':30,'probability':'100'}],
    [{'min':0,'max':30,'probability':True}],
    [{'min':0,'max':30,'probability':float('nan')}],
    [{'min':0,'max':30,'probability':float('inf')}],
    [{'min':0,'max':30,'probability':50},{'reward':50,'probability':50}],
    [{'min':0,'max':30,'reward':10,'probability':100}],
    [{'min':0,'probability':100}],
    [{}], [], ['not an object'],
]


@pytest.mark.parametrize('tiers', INVALID)
def test_invalid_ranges_rejected_without_config_write(tiers, client):
    before = client.get('/api/plugins/scratch9', auth=ADMIN).json()['config']
    response = client.post('/api/plugins/scratch9', auth=ADMIN, json={'config': {'reward_tiers': json.dumps(tiers)}})
    assert response.status_code == 400
    assert client.get('/api/plugins/scratch9', auth=ADMIN).json()['config'] == before


def test_actual_admin_range_editor_metadata_save_readback_and_preserve_other_parameters(client):
    response = client.post('/api/plugins/scratch9', auth=ADMIN, json={'enabled':True,
        'config':{'cost':37,'per_person':2,'duration_minutes':41,'schedule_times':'13:05,21:10'}})
    assert response.status_code == 200
    before = response.json()['config']
    custom = [{'min':0,'max':0,'probability':3},{'min':1,'max':17,'probability':97}]
    response = client.post('/api/plugins/scratch9', auth=ADMIN, json={'config': {'reward_tiers':json.dumps(custom)}})
    assert response.status_code == 200
    readback = client.get('/api/plugins/scratch9', auth=ADMIN).json()
    assert readback['enabled'] is True
    assert json.loads(readback['config']['reward_tiers']) == custom
    assert {k:v for k,v in readback['config'].items() if k!='reward_tiers'} == {k:v for k,v in before.items() if k!='reward_tiers'}
    raw = app.state.plugins._store.section('plugins')['scratch9']['config']
    assert json.loads(raw['reward_tiers']) == custom
    field = next(f for f in readback['fields'] if f['key']=='reward_tiers')
    assert field['kind'] == 'text' and 'min/max' in field['help']
    assert json.loads(field['default']) == TIERS and '仅后台' in field['label']
    assert max_reward(readback['config']) == 17


@pytest.mark.parametrize('draw,offset,amount', [(0,0,0),(30000,9,10),(730000,16,47),(999999,487,888)])
def test_actual_two_click_handler_real_integer_atomic_ledgers_public_original_card_and_no_odds(env, monkeypatch, draw, offset, amount):
    rng, calls = sequence([draw, offset])
    monkeypatch.setattr('secrets.randbelow', rng)
    async def run():
        row = await create(env)
        mid = row['card_message_id']
        initial = copy.deepcopy(env.tg.message(GROUP, mid))
        balance = env.services.points.balance(env.uids[1])
        total = sum(env.services.points.balances().values())
        assert '最高 888 积分' in initial['caption']
        assert '再次' in await click(env, row, request='first')
        assert calls == [] and not svc(env).cells(row['nonce'])
        assert env.tg.message(GROUP, mid) == initial
        assert '获得'+str(amount)+'积分' in await click(env, row, request='second')
        assert len(calls) == 2
        cell = svc(env).cells(row['nonce'])[0]
        assert cell['reward'] == amount and cell['cost'] == 30
        assert env.services.points.balance(env.uids[1]) == balance-30+amount
        assert sum(env.services.points.balances().values()) == total-30+amount
        ledger = env.db.query("SELECT * FROM points_ledger WHERE reason LIKE 'scratch9.%'")
        assert [r['delta'] for r in ledger] == ([-30,amount] if amount else [-30])
        calls_before = calls[:]
        assert '这个格子已被刮开' in await click(env, row, request='new-third')
        assert '本场已参与' in await click(env, row, cell=2, request='other-cell')
        assert env.db.query("SELECT * FROM points_ledger WHERE reason LIKE 'scratch9.%'") == ledger
        assert calls == calls_before
        current = svc(env).get(row['nonce'])
        assert current['card_message_id'] == mid and current['revision'] == current['rendered_revision']
        text = env.tg.text(GROUP, mid)
        assert str(amount)+'积分' in text
        for secret in ('概率','%','余额','private-login','min','max','probability'):
            assert secret not in text
        assert not any(m=='sendMessage' for m,_ in env.tg.calls)
        assert len([m for m,_ in env.media if m=='sendPhoto']) == 1
        assert all(p['message_id']==mid for m,p in env.media if m=='editMessageMedia')
        for request in ('first','second','new-third','other-cell'):
            reply = answers(env,request)
            assert len(reply)==1 and reply[0]['show_alert'] is True and reply[0]['text']
    asyncio.run(run())


def test_legacy_fixed_and_new_range_snapshots_coexist_old_intent_no_rule_switch_and_restart_no_redraw(env, monkeypatch):
    async def run():
        env.services.registry.save('scratch9', config={'reward_tiers':json.dumps(FIXED_TIERS)})
        old = await create(env)
        old_cfg = old['config_json']
        old_intent = await select(env,old,index=1,request='old-first')
        env.services.registry.save('scratch9', config={'reward_tiers':DEFAULTS['reward_tiers']})
        rng,calls = sequence([250000])
        monkeypatch.setattr('secrets.randbelow',rng)
        old_done = await confirm(env,old_intent,index=1)
        assert json.loads(old_done['result_json'])['reward']==10 and calls==[1000000]
        assert svc(env).get(old['nonce'])['config_json']==old_cfg
        env.db.execute("UPDATE scratch9_rounds SET state='closed' WHERE nonce=?",(old['nonce'],))
        new = await create(env,mid=1778)
        new_cfg = new['config_json']
        assert json.loads(json.loads(new_cfg)['reward_tiers'])==TIERS
        new_intent = await select(env,new,index=1,request='new-first')
        # Later admin edit cannot change this new round either.
        env.services.registry.save('scratch9',config={'reward_tiers':'[{"min":0,"max":0,"probability":100}]'})
        rng,calls = sequence([30000,16])
        monkeypatch.setattr('secrets.randbelow',rng)
        new_done = await confirm(env,new_intent,index=1)
        assert json.loads(new_done['result_json'])['reward']==17 and calls==[1000000,30]
        ledger = env.db.query('SELECT * FROM points_ledger')
        def must_not_draw(n):
            raise AssertionError('committed snapshot retry attempted to redraw')
        db = Database(env.db.path)
        resumed = Scratch9Service(db,MemberService(db,GroupService(db)),PointsService(db),
            lambda:env.services.registry.config('scratch9'),lambda:True,env.bot._group_chat_allowed,'123',randbelow=must_not_draw)
        try:
            for row,intent,amount in ((old,old_intent,10),(new,new_intent,17)):
                result = resumed.confirm(intent['token'],env.actors[1],card(env,resumed.get(row['nonce'])),'yes',request='restart-'+intent['token'])
                assert result['reward']==amount
            assert resumed.get(old['nonce'])['config_json']==old_cfg
            assert resumed.get(new['nonce'])['config_json']==new_cfg
        finally:
            db.close()
        assert env.db.query('SELECT * FROM points_ledger')==ledger
    asyncio.run(run())


@pytest.mark.parametrize('invalid', [-1, 30, True, 1.5])
def test_second_rng_invalid_rolls_back_fee_cell_and_reward_then_handler_alert(env, monkeypatch, invalid):
    rng,calls = sequence([30000,invalid])
    monkeypatch.setattr('secrets.randbelow',rng)
    async def run():
        row = await create(env)
        await click(env,row,request='first')
        ledger = env.db.query('SELECT * FROM points_ledger')
        assert '暂未完成' in await click(env,row,request='second')
        assert calls==[1000000,30]
        assert env.db.query('SELECT * FROM points_ledger')==ledger and not svc(env).cells(row['nonce'])
    asyncio.run(run())


def test_public_max_uses_snapshot_possible_prizes_and_never_draws_unclaimed_cells(monkeypatch):
    cfg = configuration({'reward_tiers':'[{"min":1,"max":17,"probability":100},{"min":18,"max":999,"probability":0}]'})
    def forbidden(n):
        raise AssertionError('renderer must not draw')
    monkeypatch.setattr('secrets.randbelow',forbidden)
    row={'config_json':json.dumps(cfg),'nonce':'preview-only','state':'active','expires_at':1800000000}
    text,keys = view(row,[])
    assert '最高 17 积分' in text and '999' not in text
    assert '概率' not in text and '%' not in text
    assert len(keys)==3 and all('分' not in b['text'] for line in keys for b in line)
    assert render(row,[]).startswith(b'\x89PNG')
    legacy = configuration({'reward_tiers':'[{"reward":7,"probability":100},{"reward":888,"probability":0}]'})
    assert max_reward(legacy)==7


def test_range_two_sqlite_writers_compete_one_cell_exactly_one_fee_and_one_reward(env):
    async def prepare():
        row = await create(env)
        intents = [await select(env,row,index=i+1,request=f'competitor-{i}') for i in range(2)]
        return row,intents,[card(env,row),card(env,row)]
    row,intents,messages = asyncio.run(prepare())
    before = sum(env.services.points.balances().values())
    databases = [Database(env.db.path),Database(env.db.path)]
    def write(i):
        db = databases[i]
        service = Scratch9Service(db,MemberService(db,GroupService(db)),PointsService(db),
            lambda:env.services.registry.config('scratch9'),lambda:True,env.bot._group_chat_allowed,'123',randbelow=lambda n:n-1)
        try:
            return service.confirm(intents[i]['token'],env.actors[i+1],messages[i],'yes',request=f'confirm-{i}')
        except PlayError as exc:
            assert '已被刮开' in str(exc)
            return None
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(write,(0,1)))
    finally:
        for db in databases:
            db.close()
    assert sum(r is not None for r in result)==1
    assert len(svc(env).cells(row['nonce']))==1
    assert [r['delta'] for r in env.db.query("SELECT * FROM points_ledger WHERE reason LIKE 'scratch9.%' ORDER BY id")]==[-30,888]
    assert sum(env.services.points.balances().values())==before-30+888
    assert env.db.one("SELECT COUNT(*) n FROM audit_log WHERE action='scratch9.claim'")['n']==1
