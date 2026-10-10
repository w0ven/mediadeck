"""Actual check-in service + received update renders only awarded extras."""
import asyncio
import copy
import json
import os
import time
from html import escape
from pathlib import Path

import pytest
from test_checkin_interaction import (
    economy_env,  # noqa: F401
    interaction_env,  # noqa: F401
    message,
    posted,
)
from test_checkin_interaction import env as receipt_env  # noqa: F401
from test_economy import proof
from test_tg_interaction_context import GROUP, VIEWER

from app.modules.economy_rules import DEFAULT_CARDS, day_bounds, encode
from app.modules.plugins_points import CHECKIN_BASE_SIZE, checkin_base


@pytest.fixture
def env(request):
    return request.getfixturevalue('receipt_env')


def core(body):
    return body.split(' 的签到\n\n',1)[-1]


@pytest.mark.parametrize('chat', [VIEWER,GROUP])
@pytest.mark.parametrize('base,percent,multiplier,drop', [
    (2,0,1,False), (2,15,1,False), (-2,100,1,False),
    (20,15,1,False), (20,15,2,False), (2,0,2,True),
])
def test_real_service_private_group_only_actual_award_lines_history_and_balance(env,monkeypatch,chat,base,percent,multiplier,drop):
    drop_name='<邀请&卡>'
    spec=dict(DEFAULT_CARDS[0],name=drop_name)
    env.services.registry.save('checkin',config={
        'streak_tiers':encode([{'days':1,'percent':percent}]),'weekends':False,
        'double_ppm':1000000 if multiplier>1 else 0,'multiplier':multiplier,
        'drops':encode([{'ppm':1000000,'spec':spec}]) if drop else '[]',
        'holidays':encode([{'date':day_bounds(time.time())[0],'name':'隔离测试活动'}]),
    })
    roll = next(r for r in range(CHECKIN_BASE_SIZE) if checkin_base(r) == base)
    monkeypatch.setattr('app.modules.plugins_points.draw',
                        lambda secret,uid,day,domain,bound:roll if domain=='base' else 0)
    if base>=0:
        env.services.points.add('u1',-env.services.points.balance('u1'),'isolated.reset')
    before_balance=env.services.points.balance('u1')
    proof(env.db,'u1',now=time.time())
    async def run():
        row=message(chat=chat,thread=7 if chat==GROUP else None)
        await env.bot._dispatch_update({'message':row})
        body=posted(env)[-1]['text'];plain=core(body)
        bonus=max(base,0)*percent//100
        effective_multiplier=multiplier if base>0 else 1
        award=base*effective_multiplier+bonus
        saved=env.db.one('SELECT * FROM checkins')
        frozen=json.loads(saved['result_json'])
        assert frozen['points']==award and frozen['bonus']==bonus and frozen['base']==base
        assert frozen['streak_percent']==percent and frozen['streak']==1
        assert frozen['multiplier']==effective_multiplier
        assert f'积分 <b>{award:+d}</b>' in plain
        assert f'余额 <b>{before_balance+award}</b>' in plain
        assert env.services.points.balance('u1')==before_balance+award
        assert all(word not in plain for word in ('基础','×1','连续签到','仅基础','不参与','已入包','含连签'))
        assert ('连签奖励' in plain)==(bonus>0)
        if bonus>0:assert f'连签奖励 <b>+{bonus}</b>' in plain
        assert ('🍀 幸运' in plain)==(effective_multiplier>1)
        assert ('🎁' in plain)==drop
        if drop:
            assert escape(drop_name) in plain
            assert len(env.services.bag.items('u1'))==1
        if not bonus and effective_multiplier==1 and not drop:
            assert plain.splitlines()==['✅ <b>签到成功</b>',f'积分 <b>{award:+d}</b>',f'余额 <b>{before_balance+award}</b>']
        if chat==GROUP:assert '&lt;Viewer&gt;' in body and ' 的签到' in body
        ledger=copy.deepcopy(env.services.points.ledger('u1'))
        # Current config changes cannot rewrite frozen history on a second check-in.
        env.services.registry.save('checkin',config={'streak_tiers':encode([{'days':1,'percent':50}]),'drops':'[]'})
        row['message_id']+=1
        await env.bot._dispatch_update({'message':row})
        repeated=core(posted(env)[-1]['text'])
        assert '今天已签到' in repeated and '连签' not in repeated and '基础' not in repeated
        assert env.db.one('SELECT * FROM checkins')==saved
        assert env.services.points.ledger('u1')==ledger
        assert env.services.points.balance('u1')==before_balance+award
        if os.getenv('PACKET_UI_ARTIFACTS') and chat==GROUP:
            path=Path(os.environ['PACKET_UI_ARTIFACTS'])/'checkin-effect-samples.json'
            samples=json.loads(path.read_text()) if path.exists() else {}
            if (base,percent,multiplier,drop)==(2,0,1,False):samples['normal']=body
            if (base,percent,multiplier,drop)==(20,15,2,False):samples['reward']=body
            if drop:samples['drop']=body
            path.write_text(json.dumps(samples,ensure_ascii=False,indent=2)+'\n')
    asyncio.run(run())


@pytest.mark.parametrize('chat',[VIEWER,GROUP])
@pytest.mark.parametrize('points,base,bonus',[(15,10,None),(15,10,0),(15,10,5),(0,0,None),(0,0,0)])
def test_frozen_legacy_success_fields_render_without_recomputing_or_mutating_history(env,monkeypatch,chat,points,base,bonus):
    saved={'ok':True,'points':points,'balance':115,'streak':30,'base':base,'multiplier':1}
    if bonus is not None:saved['bonus']=bonus
    original=copy.deepcopy(saved)
    # Frozen old receipt need not have streak_percent/version/drop_spec.
    monkeypatch.setattr(env.services.checkin,'checkin',lambda user:saved)
    before={table:env.db.query('SELECT * FROM '+table) for table in ('checkins','points_ledger','inventory')}
    asyncio.run(env.bot._dispatch_update({'message':message(chat=chat)}))
    body=core(posted(env)[-1]['text'])
    assert f'积分 <b>{points:+d}</b>' in body and '余额 <b>115</b>' in body
    assert ('连签奖励' in body)==bool(bonus)
    assert all(word not in body for word in ('连续签到','基础','×1','%','幸运','🎁'))
    assert saved==original
    assert {table:env.db.query('SELECT * FROM '+table) for table in before}==before
