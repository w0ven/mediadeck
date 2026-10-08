"""Small-group discovery and low cash path via actual handlers; existing assets/fees persist."""
import asyncio
import copy
import json

import pytest
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_market_bot import body, card, click, cmd, service, transact
from test_market_bot import env as market_env  # noqa: F401

from app.core.db import Database
from app.modules.market_data import COMPANIES, DEFAULTS


@pytest.fixture
def env(request):
    e=request.getfixturevalue('market_env')
    e.services.registry.save('stock_market',config={'fee_bps':0})
    return e


def balance(e,index,value):
    uid=e.uids[index]
    e.services.points.add(uid,value-e.services.points.balance(uid),'isolated.small-market')


def cash(e):
    return sum(e.services.points.balances().values())+(e.db.one('SELECT SUM(amount) n FROM play_escrows')['n'] or 0)+(e.db.one('SELECT SUM(amount) n FROM play_funds')['n'] or 0)


def test_focused_home_all_60_keyword_search_and_configured_recommendations(env):
    async def run():
        row=await cmd(env,'/股票')
        assert json.loads(row['payload_json'])['view']=='home'
        assert [c['code'] for c in service(env).featured()]==['MD001','MD006','MD011']
        assert all(c['issue_price']==10 for c in service(env).featured())
        assert '真实待买 0' in body(env,row) and '卖出需真实买家' in body(env,row)
        all_rows=await click(env,row,'view','search')
        found=set()
        for page in range(8):
            if page:all_rows=await click(env,all_rows,'page',page)
            found|={c['code'] for c in COMPANIES if c['code'] in body(env,all_rows)}
        assert len(found)==60
        for c in COMPANIES:
            row=await cmd(env,'/股票 '+c['code'])
            assert c['name'] in body(env,row) and json.loads(row['payload_json'])['code']==c['code']
        row=await cmd(env,'/股票 农业')
        assert '丰屿种业' in body(env,row)
        env.services.registry.save('stock_market',config={'recommended_codes':'MD060,MD050,MD040'})
        row=await cmd(env,'/股票')
        assert [c['code'] for c in service(env).featured()]==['MD060','MD050','MD040']
        assert env.db.one('SELECT COUNT(*) n FROM market_companies')['n']==60
    asyncio.run(run())


def test_ten_point_ipo_then_true_book_one_share_confirm_and_conservation(env):
    async def run():
        assert DEFAULTS['fee_bps']==0
        balance(env,0,10);balance(env,1,10)
        before=cash(env)
        row=await cmd(env,'/股票 MD001',index=0)
        ledger=copy.deepcopy(env.services.points.ledger(env.uids[0]))
        row=await click(env,row,'quick','ipo_MD001')
        intent=service(env).intent(json.loads(row['payload_json'])['intent'])
        assert intent['quantity']==1 and json.loads(intent['config_json'])['fee_bps']==0
        assert '支付 10' in body(env,row) and '手续费 0' in body(env,row) and '发行认购' in body(env,row)
        assert env.services.points.ledger(env.uids[0])==ledger
        assert not env.db.query('SELECT * FROM market_subscriptions')
        row=await click(env,row,'confirm')
        assert env.services.points.balance(env.uids[0])==0 and service(env).position(env.uids[0],'MD001')['shares']==1
        await transact(env,0,'sell',1,10)
        sell=env.db.one("SELECT * FROM market_orders WHERE side='sell' AND state='open'")
        public=await cmd(env,'/股票',group=True,index=1)
        assert '待卖 1' in body(env,public)
        book=await click(env,public,'view','book')
        assert '待卖 1 股 × 10' in body(env,book)
        for private in ('private-login',env.uids[0],'余额'):assert private not in body(env,book)
        own=await cmd(env,'/股票',index=1)
        own=await click(env,own,'view','book')
        old=card(env,own)
        own=await click(env,own,'take',sell['nonce'])
        preview=service(env).intent(json.loads(own['payload_json'])['intent'])
        assert preview['quantity']==1 and preview['price']==10
        assert '最多冻结 10' in body(env,own) and '手续费 0' in body(env,own) and '玩家转让' in body(env,own)
        assert env.services.points.balance(env.uids[1])==10 and not env.db.query('SELECT * FROM market_trades')
        await click(env,own,'take',sell['nonce'],original=old)
        assert env.db.one("SELECT COUNT(*) n FROM market_intents WHERE operation='buy'")['n']==1
        own=await click(env,own,'confirm')
        assert env.db.one('SELECT fee FROM market_trades')['fee']==0
        assert env.services.points.balance(env.uids[0])==10 and env.services.points.balance(env.uids[1])==0
        assert cash(env)==before
        holdings=await cmd(env,'/持仓',index=1)
        assert '1 股' in body(env,holdings) and '成本 10' in body(env,holdings)
    asyncio.run(run())


def test_quick_buy_no_counterparty_freezes_only_on_confirm_and_cancel_refunds(env):
    async def run():
        balance(env,1,10);before=cash(env)
        row=await cmd(env,'/股票 MD001',index=1)
        row=await click(env,row,'quick','buy_MD001')
        assert env.services.points.balance(env.uids[1])==10
        intent=service(env).intent(json.loads(row['payload_json'])['intent'])
        assert intent['quantity']==1 and intent['price']==10
        row=await click(env,row,'confirm')
        order=env.db.one('SELECT * FROM market_orders')
        assert order['state']=='open' and order['fee_bps']==0
        assert not env.db.query('SELECT * FROM market_trades') and env.services.points.balance(env.uids[1])==0
        env.services.registry.save('stock_market',config={'digest_enabled':False})
        row=await cmd(env,'/委托',index=1)
        await click(env,row,'cancel',order['nonce'])
        assert env.services.points.balance(env.uids[1])==10 and cash(env)==before
    asyncio.run(run())


def test_old_buy_fee_snapshot_survives_zero_new_fee_and_additive_restart(env):
    async def run():
        env.services.registry.save('stock_market',config={'fee_bps':50})
        await transact(env,0,'ipo',2)
        await transact(env,1,'buy',2,10)
        old=env.db.one("SELECT * FROM market_orders WHERE side='buy'")
        assert old['fee_bps']==50
        env.services.registry.save('stock_market',config={'fee_bps':0})
        before=cash(env);ledger=copy.deepcopy(env.db.query('SELECT * FROM points_ledger'))
        check=Database(env.db.path);check.close()
        assert env.db.query('SELECT * FROM points_ledger')==ledger
        assert env.db.one('SELECT * FROM market_orders WHERE id=?',(old['id'],))==old
        await transact(env,0,'sell',1,10)
        assert env.db.one('SELECT fee FROM market_trades')['fee']==1
        assert env.db.one('SELECT fee_bps FROM market_orders WHERE id=?',(old['id'],))['fee_bps']==50
        await transact(env,0,'sell',1,10)
        assert env.db.one('SELECT SUM(fee) n FROM market_trades')['n']==1
        assert cash(env)==before
        await transact(env,2,'buy',1,10)
        assert env.db.one("SELECT fee_bps FROM market_orders WHERE user_id=?",(env.uids[2],))['fee_bps']==0
        env.services.registry.save('stock_market',config={'fee_bps':50})
        preview=service(env).preview(env.actors[3],'buy','MD001',1,10)
        env.services.registry.save('stock_market',config={'fee_bps':0})
        service(env).confirm(preview['nonce'],env.actors[3])
        assert env.db.one('SELECT fee_bps FROM market_orders WHERE nonce=?',(preview['nonce'],))['fee_bps']==50
    asyncio.run(run())


@pytest.mark.parametrize('cfg', [{'recommended_codes':'MD001,MD001,MD006'},{'recommended_codes':'MD001,MD006,MD061'},{'digest_times':'10:00,18:00,20:00'},{'digest_times':'22:00'},{'digest_times':'08:59'},{'digest_times':'10:99'}])
def test_new_config_validation_preserves_old_settings(env,cfg):
    old=copy.deepcopy(env.services.registry._store.section('plugins'))
    with pytest.raises(ValueError):env.services.registry.save('stock_market',config=cfg)
    assert env.services.registry._store.section('plugins')==old


def test_saving_new_controls_retains_unknown_top_and_config_and_does_not_touch_assets(env):
    store=env.services.registry._store
    raw=copy.deepcopy(store.section('plugins'))
    raw['stock_market']['parallel_unknown']={'keep':[1,2]}
    raw['stock_market'].setdefault('config',{})['unknown_saved']={'nested':'keep'}
    store.set_section('plugins',raw)
    ledger=copy.deepcopy(env.db.query('SELECT * FROM points_ledger'))
    companies=copy.deepcopy(env.db.query('SELECT * FROM market_companies'))
    env.services.registry.save('stock_market',config={'digest_enabled':False,'digest_times':'11:00,19:00','recommended_codes':'MD060,MD050,MD040'})
    saved=store.section('plugins')['stock_market']
    assert saved['parallel_unknown']==raw['stock_market']['parallel_unknown']
    assert saved['config']['unknown_saved']=={'nested':'keep'}
    assert saved['config']['digest_times']=='11:00,19:00'
    assert env.db.query('SELECT * FROM points_ledger')==ledger and env.db.query('SELECT * FROM market_companies')==companies
