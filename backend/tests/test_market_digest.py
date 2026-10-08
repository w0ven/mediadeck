"""Future-only clock, restart/concurrency/transport and original-card tests; synthetic TG."""
import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_market_bot import env as market_env  # noqa: F401
from test_market_bot import service
from test_tg_interaction_context import GROUP

from app.core.db import Database
from app.modules.economy_rules import BEIJING
from app.modules.market_digest import DigestService, in_window, payload, times
from app.modules.market_views import news_tick
from app.modules.report_delivery import CALL_DELIVERY


def clock(h,m=0,s=0,day=10):return datetime(2026,10,day,h,m,s,tzinfo=BEIJING).timestamp()


@pytest.fixture
def env(request):
    e=request.getfixturevalue('market_env')
    e.services.registry.save('stock_market',config={'digest_enabled':True,'fee_bps':0})
    return e


def sync(s,now,*,enabled=True,startup=False,slots=None,groups=None):
    s.sync(groups or [str(GROUP)],slots or times('10:00,18:00'),enabled,startup=startup,now=now)


def test_enable_future_only_exact_daily_slots_and_no_restart_or_downtime_catchup(env):
    s=DigestService(env.db,'123')
    sync(s,clock(10,0,1),startup=True)
    rows=env.db.query('SELECT * FROM market_digest_jobs')
    assert all(r['scheduled_at']>clock(10,0,1) for r in rows)
    assert not s.claim([str(GROUP)],now=clock(10,0,1))
    sync(s,clock(17,59,50))
    sync(s,clock(18))
    job=s.claim([str(GROUP)],now=clock(18))
    assert job and job['slot']=='2026-10-10T18:00'
    s.finish(job,{'message_id':501},{},now=clock(18))
    restart=DigestService(env.db,'123')
    sync(restart,clock(18,0,2),startup=True)
    assert not restart.claim([str(GROUP)],now=clock(18,0,2))
    sync(restart,clock(10,0,2,day=11),startup=True)
    assert not restart.claim([str(GROUP)],now=clock(10,0,2,day=11))
    assert env.db.one("SELECT state FROM market_digest_jobs WHERE slot='2026-10-11T10:00'")['state']=='expired'
    assert env.db.one("SELECT COUNT(*) n FROM market_digest_jobs WHERE state='sent'")['n']==1


@pytest.mark.parametrize('h,m',[(22,0),(23,59),(0,0),(8,59)])
def test_hard_quiet_window_no_claim_even_if_db_job_due(env,h,m):
    s=DigestService(env.db,'123');sync(s,clock(9),slots=times('09:01'))
    env.db.execute("UPDATE market_digest_jobs SET due_at=?,expires_at=? WHERE day='2026-10-10'",(clock(h,m)-1,clock(h,m)+300))
    assert not in_window(clock(h,m)) and not s.claim([str(GROUP)],now=clock(h,m))
    assert env.db.one('SELECT SUM(attempts) n FROM market_digest_jobs')['n']==0


def test_two_connections_one_claim_unknown_crash_no_replay_and_daily_cap_on_reconfigure(env):
    s=DigestService(env.db,'123');sync(s,clock(9,59,50));sync(s,clock(10))
    connections=[Database(env.db.path),Database(env.db.path)]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs=list(pool.map(lambda db:DigestService(db,'123').claim([str(GROUP)],now=clock(10)),connections))
    finally:
        for db in connections:db.close()
    claimed=[j for j in jobs if j];assert len(claimed)==1
    sync(s,clock(10,0,5),startup=True)
    assert s.summary()['群资讯需核实']==1
    assert not s.claim([str(GROUP)],now=clock(10,0,5))
    # A late success after the lease was retired must not falsely mark it confirmed.
    s.finish(claimed[0],{'message_id':502},{},now=clock(10,0,6))
    assert env.db.one('SELECT state FROM market_digest_jobs WHERE id=?',(claimed[0]['id'],))['state']=='unknown'
    sync(s,clock(10,1),slots=times('18:30,20:00'))
    today=env.db.query("SELECT * FROM market_digest_jobs WHERE day='2026-10-10' AND (attempts>0 OR state='pending')")
    assert len(today)==2 and sum(r['state']=='pending' for r in today)==1
    assert env.db.one('SELECT COUNT(*) n FROM market_digest_jobs WHERE chat_id=? AND slot=?',(str(GROUP),'2026-10-10T10:00'))['n']==1


def test_429_bounded_same_slot_retry_permissions_unknown_and_quiet_boundary_visible(env):
    s=DigestService(env.db,'123');sync(s,clock(9,59,50));sync(s,clock(10))
    job=s.claim([str(GROUP)],now=clock(10))
    s.finish(job,None,{'state':'retry','retry_after':30,'reason':'Telegram 限流'},now=clock(10))
    assert s.summary()['群资讯待重试']==1
    assert not s.claim([str(GROUP)],now=clock(10,0,29))
    sync(s,clock(10,0,30));job=s.claim([str(GROUP)],now=clock(10,0,30))
    assert job['attempts']==2
    s.finish(job,{'message_id':503},{},now=clock(10,0,30))
    sync(s,clock(17,59,50));sync(s,clock(18));job=s.claim([str(GROUP)],now=clock(18))
    s.finish(job,None,{'state':'failed','reason':'没有群发送权限'},now=clock(18))
    assert s.summary()['群资讯失败']==1 and '权限' in s.summary()['最近群资讯异常']
    assert not s.claim([str(GROUP)],now=clock(18,0,10))
    sync(s,clock(21,58),slots=times('21:59'))
    sync(s,clock(21,58,50),slots=times('21:59'));sync(s,clock(21,59),slots=times('21:59'))
    # Today's two attempted slots already consumed the day limit; use next day's edge.
    sync(s,clock(21,58,50,day=11),slots=times('21:59'));sync(s,clock(21,59,day=11),slots=times('21:59'))
    job=s.claim([str(GROUP)],now=clock(21,59,day=11));assert job
    s.finish(job,None,{'state':'retry','retry_after':60,'reason':'限流'},now=clock(21,59,day=11))
    assert env.db.one('SELECT state FROM market_digest_jobs WHERE id=?',(job['id'],))['state']=='failed'
    assert not s.claim([str(GROUP)],now=clock(22,day=11))


def test_disable_reenable_only_future_groups_changed_no_assets_or_historical_resurrection(env):
    s=DigestService(env.db,'123');sync(s,clock(9,59,50))
    before=copy.deepcopy(env.db.query('SELECT * FROM points_ledger'))
    sync(s,clock(9,59,55),enabled=False)
    assert not s.claim([str(GROUP)],now=clock(10))
    sync(s,clock(10,0,1),enabled=True)
    assert not s.claim([str(GROUP)],now=clock(10,0,1))
    assert env.db.query('SELECT * FROM points_ledger')==before
    sync(s,clock(17,59,50));sync(s,clock(18))
    assert not s.claim(['-900999'],now=clock(18))
    assert env.db.one("SELECT state FROM market_digest_jobs WHERE slot='2026-10-10T18:00'")['state']=='cancelled'


def test_actual_scheduled_worker_card_buttons_and_original_context_without_test_delivery(env):
    async def run():
        cfg=env.services.registry.config('stock_market')
        raw=copy.deepcopy(env.services.registry._store.section('telegram'))
        raw['registration_notify_chat_id']='-900999'
        env.services.registry._store.set_section('telegram',raw)
        before=copy.deepcopy(env.db.query('SELECT * FROM points_ledger'))
        env.tg.calls.clear()
        await env.bot._market_digest_tick(cfg,now=clock(9,59,50))
        assert not any(m=='sendMessage' for m,p in env.tg.calls)
        await env.bot._market_digest_tick(cfg,now=clock(10))
        sends=[p for m,p in env.tg.calls if m=='sendMessage']
        assert len(sends)==1 and str(sends[0]['chat_id'])==str(GROUP)
        assert '模拟资讯' in sends[0]['text'] and '今日暂无成交' in sends[0]['text']
        for private in ('private-login','余额',env.uids[0]):assert private not in sends[0]['text']
        job=env.db.one("SELECT * FROM market_digest_jobs WHERE state='sent'")
        message=copy.deepcopy(env.tg.message(GROUP,job['message_id']))
        message.update(chat={'id':GROUP,'type':'supergroup'},message_id=job['message_id'])
        message['from']={'id':123,'is_bot':True}
        for view in ('home','book','help'):
            await env.bot._dispatch_update({'callback_query':{'id':'digest-'+view,'data':'std:'+view,'from':env.actors[1],'message':copy.deepcopy(message)}})
            assert env.db.one('SELECT payload_json FROM market_panels ORDER BY created_at DESC LIMIT 1')['payload_json']==json.dumps({'page':0,'view':view},separators=(',',':'),sort_keys=True)
        panels=env.db.one('SELECT COUNT(*) n FROM market_panels')['n']
        wrong=copy.deepcopy(message);wrong['message_id']+=1
        await env.bot._dispatch_update({'callback_query':{'id':'wrong-card','data':'std:home','from':env.actors[1],'message':wrong}})
        assert env.db.one('SELECT COUNT(*) n FROM market_panels')['n']==panels
        # Restart at a past due timestamp must not send another summary.
        env.bot._market_digest_booted=False
        await env.bot._market_digest_tick(cfg,now=clock(10,0,5))
        assert len([p for m,p in env.tg.calls if m=='sendMessage' and '小群市场 · 模拟资讯' in p.get('text','')])==1
        assert env.db.query('SELECT * FROM points_ledger')==before
    asyncio.run(run())


def test_actual_transport_unknown_is_visible_not_automatic_resend(env):
    async def run():
        cfg=env.services.registry.config('stock_market')
        calls=[]
        async def lost(method,body,**kw):
            calls.append(method);CALL_DELIVERY.set({'state':'unknown','reason':'未取得发送确认'})
        env.bot._call=lost
        await env.bot._market_digest_tick(cfg,now=clock(9,59,50))
        await env.bot._market_digest_tick(cfg,now=clock(10))
        await env.bot._market_digest_tick(cfg,now=clock(10,0,5))
        assert calls==['sendMessage']
        assert env.services.registry.get('stock_market').readonly_status()['群资讯需核实']==1
    asyncio.run(run())


def test_transport_loop_rechecks_quiet_window_before_each_group(env,monkeypatch):
    async def run():
        raw=copy.deepcopy(env.services.registry._store.section('telegram'))
        raw['group_interaction_chats']=str(GROUP)+',-900998'
        env.services.registry._store.set_section('telegram',raw)
        env.services.registry.save('stock_market',config={'digest_times':'21:59'})
        cfg=env.services.registry.config('stock_market')
        await env.bot._market_digest_tick(cfg,now=clock(21,58,50))
        wall=[clock(21,59,10)];starts=[]
        async def send(method,body,**kw):
            starts.append(wall[0]);wall[0]=clock(22);return {'message_id':601}
        env.bot._call=send
        monkeypatch.setattr('app.modules.bot_market_digest.time.time',lambda:wall[0])
        await env.bot._market_digest_tick(cfg)
        assert starts==[clock(21,59,10)]
        assert env.db.one("SELECT COUNT(*) n FROM market_digest_jobs WHERE state='sent'")['n']==1
    asyncio.run(run())


def test_actual_worker_429_waits_finite_retry_and_keeps_same_payload(env):
    async def run():
        cfg=env.services.registry.config('stock_market');bodies=[]
        async def limited(method,body,**kw):
            bodies.append(copy.deepcopy(body))
            if len(bodies)==1:
                CALL_DELIVERY.set({'state':'retry','reason':'Telegram 限流','retry_after':10})
            else:return {'message_id':602}
        env.bot._call=limited
        await env.bot._market_digest_tick(cfg,now=clock(9,59,50))
        await env.bot._market_digest_tick(cfg,now=clock(10))
        await env.bot._market_digest_tick(cfg,now=clock(10,0,5))
        assert len(bodies)==1
        await env.bot._market_digest_tick(cfg,now=clock(10,0,10))
        assert len(bodies)==2 and bodies[0]==bodies[1]
        assert env.db.one("SELECT attempts FROM market_digest_jobs WHERE state='sent'")['attempts']==2
    asyncio.run(run())


def test_summary_uses_one_simulated_news_and_true_orders_trades_without_asset_effect(env):
    market=service(env)
    def deal(index,op,q,price=None):
        intent=market.preview(env.actors[index],op,'MD001',q,price,now=clock(9))
        market.confirm(intent['nonce'],env.actors[index],now=clock(9,0,1))
    deal(0,'ipo',2);deal(0,'sell',2,10);deal(1,'buy',1,10);deal(2,'buy',1,9)
    news_tick(env.db,now=clock(10))
    tables=('points_ledger','market_companies','market_orders','market_positions','market_trades','play_escrows','play_funds')
    before={t:copy.deepcopy(env.db.query('SELECT * FROM '+t)) for t in tables}
    card=payload(env.db,clock(10))
    assert '模拟资讯' in card['text'] and '待买 1 单 · 待卖 1 单' in card['text']
    assert '今日成交 1 笔 · 1 股' in card['text']
    for private in (*env.uids,'private-login','余额'):assert private not in card['text']
    assert all(env.db.query('SELECT * FROM '+t)==before[t] for t in tables)
