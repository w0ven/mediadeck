"""Only the remaining integration gap: same ledger, five real services, five SQLite connections."""
import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_blackwhite import card as bw_card
from test_blackwhite import create as bw_create
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_economy import build, proof
from test_poker import card as pg_card
from test_poker import click as pg_click
from test_poker import create as pg_create
from test_tg_interaction_context import GROUP

from app.core.db import Database
from app.modules.blackwhite import BlackwhiteService
from app.modules.groups import GroupService
from app.modules.market import MarketService
from app.modules.members import MemberService
from app.modules.play_money import PlayError
from app.modules.points import PointsService
from app.modules.poker import PokerService
from app.modules.red_packets import DEFAULT_PACKET_CONFIG, PacketError, PacketService


@pytest.fixture
def env(request):
    e=request.getfixturevalue('bw_env')
    for pid in ('poker','stock_market'):e.services.registry.save(pid,enabled=True)
    return e


def cash(e):
    return (sum(e.services.points.balances().values())+(e.db.one('SELECT SUM(amount) n FROM play_escrows')['n'] or 0)
            +(e.db.one('SELECT SUM(amount) n FROM play_funds')['n'] or 0)
            +(e.db.one("SELECT SUM(remaining) n FROM red_packets WHERE funding='user' AND status='active'")['n'] or 0))


def local(e,db):
    members=MemberService(db,GroupService(db));points=PointsService(db)
    allowed=lambda chat:chat.get('id')==GROUP
    config=lambda pid:lambda:e.services.registry.config(pid)
    on=lambda pid:lambda:e.services.registry.enabled(pid)
    return (BlackwhiteService(db,members,points,config('blackwhite'),on('blackwhite'),allowed,'123'),
            PokerService(db,members,points,config('poker'),on('poker'),allowed,'123'),
            MarketService(db,members,points,config('stock_market'),on('stock_market'),'123'),
            PacketService(db,members,points,lambda:dict(DEFAULT_PACKET_CONFIG),lambda:True,allowed,lambda m:'admin' in m['roles'],'123'))


@pytest.mark.parametrize('repeat',range(3))
def test_five_connections_cross_game_packet_market_checkin_cash_competition_and_restart(env,repeat):
    uid=env.uids[1];actor=env.actors[1]
    env.services.points.add(uid,600-env.services.points.balance(uid),'isolated.adjust')
    proof(env.db,uid,now=time.time())
    async def prepare():
        bw=await bw_create(env,stake=500)
        pg=await pg_create(env)
        pg=await pg_click(env,pg,'join',index=1)
        return bw,pg
    bw,pg=asyncio.run(prepare())
    bw_msg,pg_msg=bw_card(env,bw),pg_card(env,pg)
    services=local(env,env.db)
    market=services[2]
    intent=market.preview(actor,'buy','MD001',50,10)
    packet=services[3].prepare({'from':actor,'chat':{'id':GROUP,'type':'supergroup'},'message_id':850,'message_thread_id':27},500,5,'equal','all')
    assert services[3].bind_card(packet['nonce'],950)
    rp_msg={'chat':{'id':GROUP,'type':'supergroup'},'message_id':950,'message_thread_id':27,
            'from':{'id':123,'is_bot':True},'reply_markup':{'inline_keyboard':[[{'callback_data':'rpok:'+packet['nonce']}]]}}
    initial=cash(env)
    barrier=threading.Barrier(5)
    dbs=[Database(env.db.path) for _ in range(5)]
    def run(index):
        db=dbs[index];bw_service,pg_service,market_service,packet_service=local(env,db)
        checkin=build(db,env.services.registry._store).checkin if index==4 else None
        barrier.wait(timeout=15)
        try:
            if index==0:bw_service.join(bw['nonce'],actor,bw_msg,'black');return True
            if index==1:return pg_service.lobby(pg['nonce'],env.actors[0],pg_msg,'start')['state']=='running'
            if index==2:market_service.confirm(intent['nonce'],actor);return True
            if index==3:packet_service.confirm(packet['nonce'],actor,rp_msg);return True
            return checkin.checkin(uid)
        except (PlayError,PacketError):return False
    try:
        with ThreadPoolExecutor(max_workers=5) as pool:outcomes=list(pool.map(run,range(5)))
    finally:
        for db in dbs:db.close()
    assert outcomes[4]['ok']
    reward=outcomes[4]['points']
    assert sum(outcomes[:4])==1  # 600 real points cannot simultaneously underwrite any two >=500 risks
    assert cash(env)==initial+reward
    assert all(value>=0 for value in env.services.points.balances().values())
    assert env.db.one('SELECT COUNT(*) n FROM checkins WHERE emby_user_id=?',(uid,))['n']==1
    assert not env.db.query('SELECT * FROM market_trades')
    for c in env.db.query('SELECT * FROM market_companies'):
        owned=env.db.one('SELECT COALESCE(SUM(shares),0) n FROM market_positions WHERE code=?',(c['code'],))['n']
        assert c['inventory']+owned==c['supply']==1000
    # New connection services recover persisted original state, no fixture reset or old DB restore.
    reopened=Database(env.db.path)
    try:
        bw_service,pg_service,market_service,packet_service=local(env,reopened)
        deadline=max(bw['expires_at'],pg_service.get(pg['nonce'])['expires_at'],packet_service.get(packet['nonce'])['expires_at'] or 0)+200000
        bw_service.expire(bw['nonce'],now=deadline)
        pg_service.expire(pg['nonce'],now=deadline)
        market_service.maintain(now=deadline)
        packet_service.expire(packet['nonce'],now=deadline)
        before=cash(env)
        bw_service.expire(bw['nonce'],now=deadline)
        pg_service.expire(pg['nonce'],now=deadline)
        market_service.maintain(now=deadline)
        packet_service.expire(packet['nonce'],now=deadline)
        assert cash(env)==before==initial+reward
        assert (env.db.one('SELECT SUM(amount) n FROM play_escrows')['n'] or 0)==0
        assert (env.db.one("SELECT SUM(amount) n FROM play_funds WHERE kind IN ('blackwhite','poker')")['n'] or 0)==0
        assert not env.db.query("SELECT * FROM market_orders WHERE state='open'")
        assert packet_service.get(packet['nonce'])['status']!='active'
    finally:reopened.close()
    target=os.getenv('GAMES_MARKET_ARTIFACTS')
    if target:Path(target,f'cross-business-{repeat}.json').write_text(json.dumps({'successful_risks':outcomes[:4],'checkin_delta':reward,'initial_cash':initial,'final_cash':cash(env),'shares':60000},indent=2))
