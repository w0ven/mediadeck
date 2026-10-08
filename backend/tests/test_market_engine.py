"""No fake liquidity: exact fixed cash/share invariants over real transaction paths."""
import copy
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.db import Database
from app.modules.groups import GroupService
from app.modules.market import MarketService
from app.modules.market_data import DEFAULTS, fee
from app.modules.members import MemberService
from app.modules.play_money import CashBook, PlayError
from app.modules.points import PointsService


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path/'market.db')
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    points = PointsService(db)
    actors = []
    for i in range(8):
        uid, tg = f'trader-{i}', 4100+i
        members.upsert(uid, f'never-public-login-{i}', {'group_id': 'standard'})
        members.bind_telegram(uid, str(tg))
        points.add(uid, 20000, 'isolated.fixture')
        actors.append({'id': tg, 'is_bot': False, 'first_name': f'<投资者{i}>'})
    # Keep original 50bp fee/conservation regressions explicit; new defaults have separate tests.
    cfg = dict(DEFAULTS, fee_bps=50)
    e = SimpleNamespace(db=db, members=members, points=points, actors=actors, cfg=cfg, enabled=True)
    e.service = local(e, db)
    e.initial = cash(e)
    yield e
    db.close()


def local(env, db):
    return MarketService(db, MemberService(db, GroupService(db)), PointsService(db), lambda: dict(env.cfg), lambda: env.enabled, '123')


def cash(e):
    return sum(e.points.balances().values()) + (e.db.one('SELECT SUM(amount) n FROM play_escrows')['n'] or 0) + (e.db.one('SELECT SUM(amount) n FROM play_funds')['n'] or 0)


def conserved(e):
    assert cash(e) == e.initial
    for company in e.db.query('SELECT * FROM market_companies'):
        positions = e.db.one('SELECT COALESCE(SUM(shares),0) n,COALESCE(SUM(locked),0) locked FROM market_positions WHERE code=?', (company['code'],))
        assert company['inventory']+positions['n'] == company['supply'] == 1000
        assert 0 <= positions['locked'] <= positions['n']
    for o in e.db.query("SELECT * FROM market_orders WHERE side='buy'"):
        escrow = e.db.one("SELECT * FROM play_escrows WHERE scope='market' AND ref=?", (o['nonce'],))
        assert escrow['amount']+escrow['spent']+escrow['released'] == escrow['reserved']
        assert o['fee'] == fee(o['gross'], o['fee_bps'])
        if o['state'] == 'open':
            future = o['remaining']*o['price']
            assert escrow['amount'] == future+fee(o['gross']+future, o['fee_bps'])-o['fee']
        else: assert escrow['amount'] == 0
    for p in e.db.query('SELECT * FROM market_positions'):
        locked = e.db.one("SELECT COALESCE(SUM(remaining),0) n FROM market_orders WHERE side='sell' AND state='open' AND user_id=? AND code=?", (p['user_id'], p['code']))['n']
        assert p['locked'] == locked


def do(e, i, operation, quantity, price=None, code='MD001', now=1000000):
    intent = e.service.preview(e.actors[i], operation, code, quantity, price, now=now)
    result = e.service.confirm(intent['nonce'], e.actors[i], now=now+1)
    conserved(e)
    return result


def order(e, result): return e.db.one('SELECT * FROM market_orders WHERE nonce=?', (result['order_nonce'],))


def test_exact_fixed_offering_real_ipo_sinks_and_no_fake_valuation(env):
    companies = env.db.query('SELECT * FROM market_companies ORDER BY code')
    assert len(companies) == 60
    assert {r['sector'] for r in companies} == {'新能源','科技','医疗','消费','制造','文娱','物流','农业','环保','旅游'}
    assert all(sum(c['sector'] == sector for c in companies) == 6 for sector in {c['sector'] for c in companies})
    assert all(sum(c['issue_price'] == p for c in companies) == 12 for p in (10,15,20,25,30))
    assert sum(c['supply'] for c in companies) == 60000
    result = do(env, 0, 'ipo', 50)
    assert result['price'] == 10 and result['fee'] == 3
    assert env.points.balance('trader-0') == 19497
    assert env.service.position('trader-0','MD001')['shares'] == 50
    assert env.service.position('trader-0','MD001')['cost'] == 503
    assert env.db.one("SELECT amount FROM play_funds WHERE kind='issuance'")['amount'] == 500
    assert env.db.one("SELECT amount FROM play_funds WHERE kind='fee'")['amount'] == 3
    assert env.db.one('SELECT COUNT(*) n FROM market_trades')['n'] == 0
    assert env.service.portfolio(env.actors[0])[0]['last_price'] is None
    assert env.service.portfolio(env.actors[0])[0]['floating'] is None
    with pytest.raises(PlayError): do(env,0,'ipo',1)
    with pytest.raises(sqlite3.IntegrityError): env.db.execute('UPDATE market_companies SET supply=2000 WHERE code=?', ('MD001',))
    with pytest.raises(sqlite3.IntegrityError): env.db.execute('UPDATE market_companies SET issue_price=15 WHERE code=?', ('MD001',))
    with pytest.raises(sqlite3.IntegrityError): env.db.execute('DELETE FROM market_companies WHERE code=?', ('MD001',))
    db = Database(env.db.path)
    restored = local(env,db)
    assert restored.company('MD001')['inventory'] == 950 and restored.position('trader-0','MD001')['shares'] == 50
    db.close()
    conserved(env)
    if os.environ.get('GAMES_MARKET_ARTIFACTS'):
        p = Path(os.environ['GAMES_MARKET_ARTIFACTS'])/'market-issuance.json'
        p.write_text(json.dumps({'companies':len(companies),'total_fixed_shares':60000,'sectors':10,'each_supply':1000,'issue_prices':[10,15,20,25,30],'price_companies_each':12,'example_ipo':result,'secondary_trade_count':0,'no_secondary_valuation':True},ensure_ascii=False,indent=2)+'\n')


def test_price_time_priority_passive_price_partial_many_orders_and_cash_improvement(env):
    for i in range(3): do(env,i,'ipo',10)
    a=do(env,0,'sell',2,12)
    b=do(env,1,'sell',1,11)
    c=do(env,2,'sell',3,12)
    buy=do(env,3,'buy',5,15)
    trades=env.db.query('SELECT * FROM market_trades ORDER BY id')
    assert [(t['price'],t['quantity'],t['sell_order']) for t in trades] == [(11,1,order(env,b)['id']),(12,2,order(env,a)['id']),(12,2,order(env,c)['id'])]
    assert order(env,c)['remaining'] == 1 and order(env,c)['state'] == 'open'
    assert order(env,buy)['gross'] == 59 and order(env,buy)['fee'] == 1
    esc=env.db.one("SELECT * FROM play_escrows WHERE scope='market' AND ref=?", (buy['order_nonce'],))
    assert (esc['reserved'],esc['spent'],esc['released'],esc['amount']) == (76,60,16,0)
    assert env.points.balance('trader-3') == 19940
    assert env.service.position('trader-3','MD001')['cost'] == 60
    assert env.service.portfolio(env.actors[3])[0]['floating'] == 0  # latest actual price12 * 5 -60
    conserved(env)


def test_sell_taker_uses_high_buy_first_then_fifo_passive_bid_price(env):
    do(env,0,'ipo',10)
    a=do(env,1,'buy',1,14)
    b=do(env,2,'buy',2,20)
    seller=do(env,0,'sell',3,10)
    assert order(env,seller)['gross'] == 54
    trades=env.db.query('SELECT * FROM market_trades ORDER BY id')
    assert [(t['price'],t['quantity'],t['buy_order']) for t in trades] == [(20,2,order(env,b)['id']),(14,1,order(env,a)['id'])]
    conserved(env)


@pytest.mark.parametrize('bps',[0,1,50,100])
def test_fee_accumulates_per_buy_not_per_fill_and_remaining_cents_are_safe(env,bps):
    env.cfg['fee_bps']=bps
    do(env,0,'ipo',20)
    for _ in range(3): do(env,0,'sell',1,1)
    buy=do(env,1,'buy',3,2)
    o=order(env,buy)
    fills=env.db.query('SELECT * FROM market_trades ORDER BY id')
    assert o['gross'] == 3 and o['fee'] == fee(3,bps)
    assert [t['fee'] for t in fills] == ([0,0,0] if bps==0 else [1,0,0])
    assert env.points.balance('trader-1') == 20000-3-fee(3,bps)
    assert env.service.position('trader-1','MD001')['cost'] == 3+fee(3,bps)
    conserved(env)


def test_cumulative_fee_reserved_with_limit_future_gross_partial_then_cancel(env):
    do(env,0,'ipo',10)
    do(env,0,'sell',1,1)
    buy=do(env,1,'buy',10,30)
    o=order(env,buy)
    assert (o['gross'],o['remaining'],o['fee']) == (1,9,1)
    esc=env.db.one("SELECT * FROM play_escrows WHERE ref=?", (buy['order_nonce'],))
    assert esc['amount'] == 270+fee(271,50)-1 == 271
    env.service.cancel(buy['order_nonce'],env.actors[1],now=1000002)
    assert env.points.balance('trader-1') == 19998
    assert env.db.one("SELECT amount FROM play_escrows WHERE ref=?", (buy['order_nonce'],))['amount'] == 0
    conserved(env)


def test_self_trade_skips_self_book_never_mints_price_or_volume(env):
    do(env,0,'ipo',10)
    own_sell=do(env,0,'sell',2,10)
    own_buy=do(env,0,'buy',2,10)
    assert not env.db.query('SELECT * FROM market_trades')
    assert order(env,own_buy)['state'] == order(env,own_sell)['state'] == 'open'
    do(env,1,'buy',1,10)
    assert env.db.one('SELECT COUNT(*) n FROM market_trades')['n'] == 1
    do(env,0,'sell',1,10)
    assert env.db.one('SELECT COUNT(*) n FROM market_trades')['n'] == 1
    conserved(env)


def test_cost_integer_allocation_clear_position_realized_and_real_only_mark_to_market(env):
    do(env,0,'ipo',3)  # cost31
    do(env,0,'sell',1,12)
    do(env,1,'buy',1,12)
    p=env.service.position('trader-0','MD001')
    assert (p['shares'],p['cost'],p['realized']) == (2,21,2)
    assert env.service.portfolio(env.actors[0])[0]['floating'] == 3
    do(env,0,'sell',2,13)
    do(env,1,'buy',2,13)
    p=env.service.position('trader-0','MD001')
    assert (p['shares'],p['cost'],p['realized']) == (0,0,7)
    assert sum(t['seller_cost'] for t in env.db.query('SELECT * FROM market_trades')) == 31
    conserved(env)


def test_confirmation_owner_snapshot_idempotent_expiry_no_pre_deduction(env):
    before=copy.deepcopy(env.db.query('SELECT * FROM points_ledger ORDER BY id'))
    intent=env.service.preview(env.actors[0],'buy','MD001',1,10,now=1000)
    assert env.db.query('SELECT * FROM points_ledger ORDER BY id') == before
    assert not env.db.query('SELECT * FROM play_escrows')
    with pytest.raises(PlayError): env.service.confirm(intent['nonce'],env.actors[1],now=1001)
    env.cfg['fee_bps']=100
    result=env.service.confirm(intent['nonce'],env.actors[0],now=1001)
    assert order(env,result)['fee_bps'] == 50
    count=env.db.one('SELECT COUNT(*) n FROM points_ledger')['n']
    for _ in range(3): assert env.service.confirm(intent['nonce'],env.actors[0],now=1002) == result
    assert env.db.one('SELECT COUNT(*) n FROM points_ledger')['n'] == count
    late=env.service.preview(env.actors[1],'ipo','MD001',1,now=1003)
    with pytest.raises(PlayError): env.service.confirm(late['nonce'],env.actors[1],now=late['expires_at'])
    env.service.maintain(now=late['expires_at'])
    assert env.service.intent(late['nonce'])['state']=='expired'
    conserved(env)


@pytest.mark.parametrize('bad', ['unbound','bot','disabled','missing','expired','rebind','wrong_bot'])
def test_only_valid_original_binding_can_place_confirm(env,bad):
    intent=env.service.preview(env.actors[0],'buy','MD001',1,10,now=1000)
    actor=copy.deepcopy(env.actors[0])
    if bad=='unbound': actor['id']=555555
    elif bad=='bot': actor['is_bot']=True
    elif bad=='disabled': env.members.upsert('trader-0','never-public',{'status':'suspended'})
    elif bad=='missing': env.db.execute('UPDATE members SET emby_missing_since=1 WHERE emby_user_id=?',('trader-0',))
    elif bad=='expired': env.members.set_overrides('trader-0',{'expires_at_override':999})
    elif bad=='rebind': env.members.bind_telegram('trader-1',str(actor['id']))
    else: env.service.bot_id='456'
    with pytest.raises(PlayError): env.service.confirm(intent['nonce'],actor,now=1001)
    assert not env.db.query('SELECT * FROM market_orders') and not env.db.query('SELECT * FROM play_escrows')
    conserved(env)


def test_holding_counts_locked_and_pending_buys_no_double_sell_max_open_orders(env):
    do(env,0,'ipo',50)
    do(env,0,'sell',50,100)
    do(env,0,'buy',100,1)
    with pytest.raises(PlayError): do(env,0,'buy',51,1)
    with pytest.raises(PlayError): do(env,0,'sell',1,1)
    do(env,0,'buy',50,1)
    assert env.service._capacity(env.db._conn,'trader-0','MD001') == 200
    for _ in range(17): do(env,0,'buy',1,1,code='MD002')
    with pytest.raises(PlayError): do(env,0,'buy',1,1,code='MD002')
    assert env.db.one("SELECT COUNT(*) n FROM market_orders WHERE state='open'")['n'] == 20
    conserved(env)


@pytest.mark.parametrize('operation,quantity,price', [('buy',0,10),('buy',1.0,10),('buy',101,1),('buy',10,1001),('buy',6,1000),('sell',1,10),('buy',True,1),('buy',1,True)])
def test_risk_parameters_no_invalid_order_or_spend(env,operation,quantity,price):
    with pytest.raises(PlayError): do(env,0,operation,quantity,price)
    assert not env.db.query('SELECT * FROM market_orders') and not env.db.query('SELECT * FROM play_escrows')
    conserved(env)


def test_short_funds_between_preview_and_confirm_rolls_back_no_order_escrow(env):
    intent=env.service.preview(env.actors[0],'buy','MD001',100,50,now=1000)
    env.points.add('trader-0',-19000,'isolated.fixture')
    env.initial-=19000
    with pytest.raises(PlayError): env.service.confirm(intent['nonce'],env.actors[0],now=1001)
    assert not env.db.query('SELECT * FROM market_orders') and not env.db.query('SELECT * FROM play_escrows')
    assert env.service.intent(intent['nonce'])['state']=='preview'
    conserved(env)


def test_closed_plugin_disabled_actor_expired_or_rebound_book_returns_assets(env):
    do(env,0,'ipo',10)
    sell=do(env,0,'sell',3,10)
    buy=do(env,1,'buy',2,1)
    env.members.upsert('trader-1','never-public',{'status':'suspended'})
    # Cancelling a disabled owner's existing reserve requires ownership, not new-risk eligibility.
    env.service.cancel(buy['order_nonce'],env.actors[1],now=1000002)
    assert env.points.balance('trader-1')==20000
    env.enabled=False
    env.service.maintain(now=1000002)
    assert order(env,sell)['state']=='closed' and env.service.position('trader-0','MD001')['locked']==0
    with pytest.raises(PlayError): do(env,0,'ipo',1)
    env.enabled=True
    env.members.upsert('trader-1','never-public',{'status':'active'})
    buy=do(env,1,'buy',1,1)
    env.members.bind_telegram('trader-2',str(env.actors[1]['id']))
    env.service.maintain(now=1000002)
    assert order(env,buy)['state']=='invalid'
    assert env.points.balance('trader-1')==20000
    conserved(env)


def test_multiple_connections_last_inventory_and_duplicate_confirm_no_reissue(env):
    env.cfg.update(ipo_limit=1000,holding_limit=1000,max_notional=50000)
    do(env,0,'ipo',998)
    env.cfg.update(DEFAULTS)
    intents=[env.service.preview(env.actors[i],'ipo','MD001',2,now=1000002) for i in (1,2)]
    dbs=[Database(env.db.path) for _ in range(2)]
    services=[local(env,d) for d in dbs]
    def ipo(i):
        try: services[i].confirm(intents[i]['nonce'],env.actors[i+1],now=1000003); return True
        except PlayError: return False
    with ThreadPoolExecutor(max_workers=2) as pool: assert sum(pool.map(ipo,range(2)))==1
    assert env.service.company('MD001')['inventory']==0
    conserved(env)
    for d in dbs: d.close()


def test_concurrent_cancel_and_match_cash_stock_exact_either_valid_serial_order(env):
    do(env,0,'ipo',10)
    sell=do(env,0,'sell',5,10)
    intent=env.service.preview(env.actors[1],'buy','MD001',5,10,now=1000002)
    dbs=[Database(env.db.path) for _ in range(2)]
    services=[local(env,d) for d in dbs]
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(services[0].cancel,sell['order_nonce'],env.actors[0],now=1000003)
        b=pool.submit(services[1].confirm,intent['nonce'],env.actors[1],now=1000003)
        a.result();result=b.result()
    filled=order(env,result)['quantity']-order(env,result)['remaining']
    assert filled in (0,5)
    assert env.service.position('trader-0','MD001')['shares']==10-filled
    assert env.service.position('trader-1','MD001')['shares']==filled
    assert env.service.position('trader-0','MD001')['locked']==0
    conserved(env)
    env.service.maintain(now=1000003+24*3600)
    conserved(env)
    assert env.db.one('SELECT SUM(amount) n FROM play_escrows')['n']==0
    for d in dbs:d.close()


def test_expired_sell_at_exact_deadline_not_matched_and_buyer_stays_real_pending(env):
    do(env,0,'ipo',10)
    sell=do(env,0,'sell',2,10)
    cutoff=order(env,sell)['expires_at']
    intent=env.service.preview(env.actors[1],'buy','MD001',2,10,now=cutoff-1)
    env.service.confirm(intent['nonce'],env.actors[1],now=cutoff)
    assert order(env,sell)['state']=='expired'
    assert not env.db.query('SELECT * FROM market_trades')
    assert env.service.position('trader-0','MD001')['locked']==0
    conserved(env)


def test_trade_fault_after_real_seller_credit_rolls_back_every_effect_and_retry(env,monkeypatch):
    do(env,0,'ipo',10)
    sell=do(env,0,'sell',2,10)
    intent=env.service.preview(env.actors[1],'buy','MD001',2,12,now=1000002)
    before=copy.deepcopy(env.db.query('SELECT * FROM points_ledger ORDER BY id'))
    original=CashBook.transfer_held
    def crash(self,*args,**kw):
        original(self,*args,**kw)
        raise RuntimeError('injected after ledger seller credit')
    monkeypatch.setattr(CashBook,'transfer_held',crash)
    with pytest.raises(RuntimeError): env.service.confirm(intent['nonce'],env.actors[1],now=1000003)
    assert env.db.query('SELECT * FROM points_ledger ORDER BY id')==before
    assert not env.db.query('SELECT * FROM market_trades')
    assert order(env,sell)['remaining']==2
    assert env.service.position('trader-0','MD001')['shares']==10
    assert env.service.intent(intent['nonce'])['state']=='preview'
    assert env.db.one('SELECT COUNT(*) n FROM market_orders')['n']==1
    conserved(env)
    monkeypatch.setattr(CashBook,'transfer_held',original)
    result=env.service.confirm(intent['nonce'],env.actors[1],now=1000003)
    assert result['filled']==2
    conserved(env)


def test_ipo_fault_after_share_update_refunds_no_partial_inventory_or_cash(env,monkeypatch):
    intent=env.service.preview(env.actors[0],'ipo','MD001',5,now=1000)
    before=env.db.query('SELECT * FROM points_ledger ORDER BY id')
    original=MarketService._move
    def crash(*args,**kw):
        original(*args,**kw)
        raise RuntimeError('injected after buyer stock journal write')
    monkeypatch.setattr(MarketService,'_move',staticmethod(crash))
    with pytest.raises(RuntimeError): env.service.confirm(intent['nonce'],env.actors[0],now=1001)
    assert env.db.query('SELECT * FROM points_ledger ORDER BY id')==before
    assert env.service.company('MD001')['inventory']==1000
    assert not env.db.query('SELECT * FROM market_positions')
    assert not env.db.query('SELECT * FROM market_subscriptions') and not env.db.query('SELECT * FROM play_funds')
    assert env.service.intent(intent['nonce'])['state']=='preview'
    conserved(env)


def test_duplicate_confirm_two_connections_one_reserve_and_one_order(env):
    intent=env.service.preview(env.actors[0],'buy','MD001',3,10,now=1000)
    dbs=[Database(env.db.path) for _ in range(2)]
    services=[local(env,d) for d in dbs]
    def commit(i):return services[i].confirm(intent['nonce'],env.actors[0],now=1001)
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(commit,range(2)))
    assert results[0]==results[1]
    assert env.db.one('SELECT COUNT(*) n FROM market_orders')['n']==1
    assert env.db.one('SELECT COUNT(*) n FROM play_escrows')['n']==1
    assert env.points.balance('trader-0')==19969
    conserved(env)
    for d in dbs:d.close()


def test_two_buyers_compete_last_shares_not_double_fill(env):
    do(env,0,'ipo',3)
    sell=do(env,0,'sell',3,10)
    intents=[env.service.preview(env.actors[i],'buy','MD001',3,10,now=1000002) for i in (1,2)]
    dbs=[Database(env.db.path) for _ in range(2)]
    services=[local(env,d) for d in dbs]
    def commit(i):return services[i].confirm(intents[i]['nonce'],env.actors[i+1],now=1000003)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(commit,range(2)))
    assert sorted(r['filled'] for r in results)==[0,3]
    assert env.db.one('SELECT SUM(quantity) n FROM market_trades')['n']==3
    assert env.service.position('trader-0','MD001')['shares']==0
    assert order(env,sell)['remaining']==0
    conserved(env)
    for d in dbs:d.close()


def test_fresh_component_pause_and_company_halt_override_old_preview_but_not_fee_snapshot(env):
    ipo=env.service.preview(env.actors[0],'ipo','MD001',3,now=1000)
    env.cfg['ipo_enabled']=False
    with pytest.raises(PlayError):env.service.confirm(ipo['nonce'],env.actors[0],now=1001)
    assert not env.db.query('SELECT * FROM play_escrows')
    env.cfg['ipo_enabled']=True
    env.service.confirm(ipo['nonce'],env.actors[0],now=1001)
    buy=do(env,1,'buy',3,1,now=1002)
    pending=env.service.preview(env.actors[2],'buy','MD001',3,1,now=1004)
    env.cfg['trading_enabled']=False
    with pytest.raises(PlayError):env.service.confirm(pending['nonce'],env.actors[2],now=1005)
    env.service.maintain(now=1005)
    assert order(env,buy)['state']=='closed' and env.points.balance('trader-1')==20000
    do(env,3,'ipo',2,now=1006)  # independently open IPO, no secondary trading
    env.cfg['trading_enabled']=True
    sell=do(env,0,'sell',1,10,now=1008)
    env.db.execute('UPDATE market_companies SET halted=1 WHERE code=?',('MD001',))
    env.service.maintain(now=1010)
    assert order(env,sell)['state']=='halted' and env.service.position('trader-0','MD001')['locked']==0
    with pytest.raises(PlayError):do(env,0,'ipo',1,now=1011)
    conserved(env)


def test_restart_open_orders_preserved_then_partial_match_expire_and_no_inventory_refill(env):
    do(env,0,'ipo',10)
    sell=do(env,0,'sell',4,12)
    db=Database(env.db.path)
    restored=local(env,db)
    assert restored.company('MD001')['inventory']==990
    intent=restored.preview(env.actors[1],'buy','MD001',2,15,now=1000002)
    result=restored.confirm(intent['nonce'],env.actors[1],now=1000003)
    assert result['filled']==2 and order(env,sell)['remaining']==2
    conserved(env)
    restored.maintain(now=order(env,sell)['expires_at'])
    assert restored.position('trader-0','MD001')['locked']==0
    assert restored.company('MD001')['inventory']==990
    conserved(env)
    db.close()
