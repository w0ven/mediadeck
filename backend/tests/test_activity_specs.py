"""Independent activity specs and pre-existing item references reach real inventory use."""
import json

import pytest
from test_economy import NOW, PPM, proof
from test_economy import env as economy_env  # noqa: F401

from app.modules.economy_rules import DEFAULT_CARDS, day_bounds, encode


@pytest.fixture
def env(request):
    return request.getfixturevalue('economy_env')


def test_global_and_each_holiday_specs_remain_independent_of_live_shop_and_freeze_on_drop(env):
    referenced=env.shop.create(dict(DEFAULT_CARDS[1],amount=100,enabled=True))
    global_spec=dict(DEFAULT_CARDS[1],name='isolated global',amount=45,duration_days=7)
    first_spec=dict(DEFAULT_CARDS[1],name='isolated holiday A',amount=25,duration_days=3)
    second_spec=dict(DEFAULT_CARDS[1],name='isolated holiday B',amount=75,duration_days=5)
    holidays=[{'date':day_bounds(NOW)[0],'name':'A','drops':[{'ppm':PPM,'spec':first_spec}]},
              {'date':day_bounds(NOW+86400)[0],'name':'B','drops':[{'ppm':PPM,'spec':second_spec}]}]
    env.registry.save('checkin',config={'drops':encode([{'ppm':PPM,'spec':global_spec}]),'holidays':encode(holidays)})
    proof(env.db,'u')
    first=env.checkin.checkin('u')
    assert first['drop_spec']==first_spec and first['drop_spec']['amount'] != referenced['amount']
    card=env.db.one('SELECT * FROM inventory WHERE id=?',(first['card_id'],))
    history=env.db.one('SELECT * FROM checkins WHERE emby_user_id=?',('u',))
    env.shop.update(referenced['id'],{'amount':200,'duration_days':11})
    holidays[0]['drops'][0]['spec']=dict(first_spec,amount=30,duration_days=4)
    env.registry.save('checkin',config={'holidays':encode(holidays)})
    assert env.checkin.checkin('u')['saved_result']==first
    assert env.db.one('SELECT * FROM inventory WHERE id=?',(first['card_id'],))==card
    assert env.db.one('SELECT * FROM checkins WHERE emby_user_id=?',('u',))==history
    assert json.loads(env.registry.config('checkin')['drops'])[0]['spec']==global_spec
    used=env.bag.use('u',first['card_id'])
    assert used['expires_at']==NOW+3*86400 and used['bandwidth_bonus_kbps']==25000
    env.clock[0]+=86400
    proof(env.db,'u',now=env.clock[0])
    second=env.checkin.checkin('u')
    assert second['drop_spec']==second_spec
    used_second=env.bag.use('u',second['card_id'])
    assert used_second['expires_at']==NOW+6*86400 and used_second['bandwidth_bonus_kbps']==75000
    assert env.members.get('u')['bandwidth_limit_kbps']==20*1024+100000
    env.clock[0]=NOW+3*86400
    assert env.members.get('u')['bandwidth_limit_kbps']==20*1024+75000
    env.clock[0]=NOW+6*86400
    assert env.members.get('u')['bandwidth_limit_kbps']==20*1024


def test_existing_item_id_reference_reads_at_drop_then_does_not_follow_later_shop_change(env):
    item=env.shop.create(dict(DEFAULT_CARDS[1],amount=100,duration_days=9,enabled=True))
    drops=[{'ppm':PPM,'item_id':item['id']}]
    env.registry.save('checkin',config={'holidays':'[]','drops':encode(drops)})
    proof(env.db,'u')
    first=env.checkin.checkin('u')
    assert first['drop_spec']['amount']==100 and first['drop_spec']['duration_days']==9
    env.shop.update(item['id'],{'amount':80,'duration_days':4})
    proof(env.db,'v')
    second=env.checkin.checkin('v')
    assert second['drop_spec']['amount']==80 and second['drop_spec']['duration_days']==4
    assert json.loads(env.registry.config('checkin')['drops'])==drops
    assert json.loads(env.db.one('SELECT spec_json FROM inventory WHERE id=?',(first['card_id'],))['spec_json'])==first['drop_spec']
    assert env.bag.use('u',first['card_id'])['bandwidth_bonus_kbps']==100000
    assert env.bag.use('v',second['card_id'])['bandwidth_bonus_kbps']==80000
