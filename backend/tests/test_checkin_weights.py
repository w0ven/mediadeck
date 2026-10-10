"""Exact fixed-weight counts and actual isolated received-update/history behavior."""
import asyncio
import copy
import json
import time
from collections import Counter
from fractions import Fraction

import pytest
from test_checkin_interaction import economy_env, interaction_env, message, posted  # noqa: F401
from test_checkin_interaction import env as receipt_env  # noqa: F401
from test_economy import build, proof
from test_tg_interaction_context import GROUP, VIEWER

from app.core.db import Database
from app.modules.economy_rules import (
    PPM,
    RULE_VERSION,
    day_bounds,
    economy_write,
    encode,
    unbiased_map,
)
from app.modules.plugins_points import (
    CHECKIN_BASE_BANDS,
    CHECKIN_BASE_RULE,
    CHECKIN_BASE_SIZE,
    CheckinPlugin,
    checkin_base,
)


@pytest.fixture
def env(request):
    return request.getfixturevalue('receipt_env')


def test_exact_all_1000_unbiased_slots_each_integer_total_signs_and_expectation():
    counts = Counter(checkin_base(roll) for roll in range(CHECKIN_BASE_SIZE))
    assert CHECKIN_BASE_SIZE == 1000
    assert counts == {**{v:1 for v in range(-10,0)}, **{v:60 for v in range(1,11)},
                      **{v:30 for v in range(11,21)}, **{v:9 for v in range(21,31)}}
    assert 0 not in counts and sum(counts.values()) == 1000
    assert [sum(counts[v] for v in range(low,high+1)) for low,high,_ in CHECKIN_BASE_BANDS] == [10,600,300,90]
    assert sum(v*Fraction(n,1000) for v,n in counts.items()) == Fraction(1019,100)
    assert sum(n for v,n in counts.items() if v<0) == 10
    assert sum(n for v,n in counts.items() if v>0) == 990


def test_same_unbiased_hmac_mapper_1000_top_rejection_and_no_modulo_bias():
    values = [unbiased_map(n,CHECKIN_BASE_SIZE,bits=16) for n in range(2**16)]
    assert values[65000:] == [None]*536
    assert Counter(values[:65000]) == {n:65 for n in range(1000)}
    limit = 2**256-(2**256%1000)
    assert unbiased_map(limit-1,1000) == 999
    assert unbiased_map(limit,1000) is None
    assert RULE_VERSION == 'watch-checkin-v1'  # No shared lottery version change.


@pytest.mark.parametrize('roll', [-1,1000,True,1.5])
def test_invalid_internal_base_draw_rejected(roll):
    with pytest.raises(ValueError):checkin_base(roll)


@pytest.mark.parametrize('percent,weekday,activity', [
    (0,'10.19','11.2145'),(5,'10.31','11.3345'),(15,'11.276','12.3005'),(50,'15.065','16.0895')])
def test_exact_confirmed_conditional_expectations_and_floor_only_positive(percent,weekday,activity):
    bases = [checkin_base(roll) for roll in range(1000)]
    ordinary = sum(Fraction(v+max(v,0)*percent//100,1000) for v in bases)
    extra = sum(Fraction(max(v,0),10000) for v in bases)
    assert ordinary == Fraction(weekday)
    assert ordinary+extra == Fraction(activity)


@pytest.mark.parametrize('chat', [VIEWER,GROUP])
@pytest.mark.parametrize('roll,expected', [(0,-10),(9,-1),(10,1),(609,10),(610,11),(909,20),(910,21),(999,30)])
def test_all_band_boundaries_real_received_checkin_and_repeat_no_redraw(env,monkeypatch,chat,roll,expected):
    e = env
    e.services.registry.save('checkin',config={'weekends':False,'holidays':'[]','drops':'[]'})
    calls = []
    def fixed_draw(secret,uid,day,domain,size):
        calls.append((domain,size))
        return roll if domain == 'base' else PPM-1
    monkeypatch.setattr('app.modules.plugins_points.draw',fixed_draw)
    initial = e.services.points.balance('u1')
    proof(e.db,'u1',now=time.time())
    async def run():
        row = message(chat=chat,thread=7 if chat==GROUP else None)
        await e.bot._dispatch_update({'message':row})
        assert calls == [('base',1000),('lucky',PPM),('drop',PPM)]
        saved = e.db.one('SELECT * FROM checkins')
        result = json.loads(saved['result_json'])
        assert result['base'] == result['points'] == expected
        assert result['bonus'] == 0 and result['multiplier'] == 1
        assert result['base_rule_version'] == CHECKIN_BASE_RULE
        assert result['rolls']['base'] == roll and result['rule_version'] == RULE_VERSION
        assert e.services.points.balance('u1') == initial+expected
        ledger = copy.deepcopy(e.services.points.ledger('u1'))
        assert ledger[0]['delta'] == expected
        text = posted(e)[-1]['text']
        assert f'积分 <b>{expected:+d}</b>' in text
        assert all(s not in text for s in ('概率','%','基础','连签奖励','幸运 ×1'))
        row['message_id'] += 1
        await e.bot._dispatch_update({'message':row})
        assert '今天已签到' in posted(e)[-1]['text']
        assert e.db.one('SELECT * FROM checkins') == saved
        assert e.services.points.ledger('u1') == ledger and len(calls) == 3
    asyncio.run(run())


@pytest.mark.parametrize('old_base', [-10,0,30])
def test_today_legacy_uniform_result_survives_new_rule_and_restart_without_draw(env,monkeypatch,old_base):
    e = env
    now = time.time();day = day_bounds(now)[0]
    old_result = {'ok':True,'base':old_base,'points':old_base,'bonus':0,'streak':1,
                  'multiplier':1,'rule_version':RULE_VERSION,'calculation_version':'base-percent-v2',
                  'rolls':{'lucky':17,'drop':10000},'rule_snapshot':e.services.registry.config('checkin')}
    e.db.execute('INSERT INTO checkins(emby_user_id,day,streak,points,created_at,result_json) VALUES(?,?,?,?,?,?)',
                 ('u1',day,1,old_base,now,encode(old_result)))
    with economy_write(e.db) as conn:
        e.services.points._apply(conn,'u1',old_base,'checkin',day,'checkin',int(now))
    before = {table:e.db.query('SELECT * FROM '+table) for table in ('checkins','points_ledger','inventory')}
    def forbidden(*args):raise AssertionError('already checked in must never draw or recalculate')
    monkeypatch.setattr('app.modules.plugins_points.draw',forbidden)
    assert e.services.checkin.checkin('u1')['saved_result'] == old_result
    db = Database(e.db.path)
    try:
        resumed = build(db,e.services.registry._store)
        assert resumed.checkin.checkin('u1',now=now)['saved_result'] == old_result
    finally:db.close()
    assert {table:e.db.query('SELECT * FROM '+table) for table in before} == before
    assert 'base_rule_version' not in json.loads(e.db.one('SELECT result_json FROM checkins')['result_json'])


def test_unqualified_received_command_never_draws_or_debits(env,monkeypatch):
    e = env
    proof(e.db,'u1',now=time.time(),seconds=599)
    before = {table:e.db.query('SELECT * FROM '+table) for table in ('checkins','points_ledger','inventory')}
    def forbidden(*args):raise AssertionError('watch eligibility precedes all random draws')
    monkeypatch.setattr('app.modules.plugins_points.draw',forbidden)
    asyncio.run(e.bot._dispatch_update({'message':message(chat=GROUP)}))
    assert '599/600' in posted(e)[-1]['text']
    assert {table:e.db.query('SELECT * FROM '+table) for table in before} == before


def test_effective_admin_rule_description_matches_fixed_weights_without_new_config_capabilities():
    text = CheckinPlugin.spec.description
    for fragment in ('-10～-1占1%','1～10占60%','11～20占30%','21～30占9%','0不抽取','无保底'):
        assert fragment in text
    assert '等概率（可为负）' not in text
    assert {field.key for field in CheckinPlugin.spec.fields} == {
        'streak_tiers','weekends','double_ppm','multiplier','drops','holidays'}
