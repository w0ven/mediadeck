"""New no-expiry envelopes: actual Bot callbacks/transport, legacy and crash behavior."""
import asyncio
import copy
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_tg_interaction_context import GROUP

from app.core.db import Database
from app.modules.game_ui import drain_ui
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.packet_delivery import PacketDelivery, receipt_view
from app.modules.points import PointsService
from app.modules.red_packets import PacketError, PacketService
from app.modules.report_delivery import CALL_DELIVERY


@pytest.fixture
def env(request):
    e=request.getfixturevalue('bw_env')
    e.services.registry.save('red_packets',enabled=True)
    return e


def svc(e):return e.bot._packet_service()


def cash(e):
    return sum(e.services.points.balances().values())+(e.db.one("SELECT SUM(remaining) n FROM red_packets WHERE funding='user' AND status='active'")['n'] or 0)


def card(e,row,*,receipt=False):
    mid=row['receipt_message_id'] if receipt else row['card_message_id']
    m=copy.deepcopy(e.tg.message(GROUP,mid))
    m.update(chat={'id':GROUP,'type':'supergroup'},message_id=mid,message_thread_id=990)
    m['from']={'id':123,'is_bot':True}
    return m


async def press(e,row,verb='rpok',index=1,original=None,suffix=''):
    await e.bot._dispatch_update({'callback_query':{'id':'permanent-'+verb,'data':verb+':'+row['nonce']+suffix,'from':e.actors[index],'message':original or card(e,row)}})
    await drain_ui(e.bot)
    return svc(e).get(row['nonce'])


async def issue(e,mid=2101,parts=2):
    await e.bot._dispatch_update({'message':{'message_id':mid,'message_thread_id':1109,'chat':{'id':GROUP,'type':'supergroup'},'from':e.actors[1],'text':f'/红包 等额 {parts*10} {parts}'}})
    row=e.db.one('SELECT * FROM red_packets WHERE command_message_id=?',(mid,))
    return await press(e,row)


def local(e,db):
    return PacketService(db,MemberService(db,GroupService(db)),PointsService(db),lambda:e.services.registry.config('red_packets'),lambda:True,e.bot._group_chat_allowed,e.bot.is_admin,'123')


def test_new_never_expires_and_original_inline_card_pin_then_separate_public_receipt(env,monkeypatch):
    async def run():
        initial=cash(env);base=env.services.points.balance(env.uids[1])
        row=await issue(env)
        assert row['permanent']==1 and row['expires_at']==0 and row['pin_state']=='sent'
        assert env.services.points.balance(env.uids[1])==base-20 and cash(env)==initial
        payload=env.tg.message(GROUP,row['card_message_id'])
        assert any(b['callback_data']=='rpclaim:'+row['nonce'] for line in payload['reply_markup']['inline_keyboard'] for b in line)
        assert '截止' not in payload['text'] and '小时' not in payload['text'] and '已置顶' not in payload['text']
        assert len([p for m,p in env.tg.calls if m=='pinChatMessage'])==1
        now=time.time()
        # Keep the synthetic claimants valid; a permanent envelope does not waive membership expiry.
        env.db.execute('UPDATE members SET expires_at=? WHERE emby_user_id IN (?,?)',(now+400*86400,env.uids[0],env.uids[2]))
        monkeypatch.setattr('time.time',lambda:now+365*86400)
        await env.bot._packet_tick()
        assert svc(env).get(row['nonce'])['status']=='active' and cash(env)==initial
        original=card(env,row)
        row=await press(env,row,'rpclaim',0,original)
        row=await press(env,row,'rpclaim',2,original)
        assert row['status']=='exhausted' and row['remaining']==0 and row['receipt_state']=='sent'
        assert row['receipt_message_id']!=row['card_message_id']
        old=env.tg.message(GROUP,row['card_message_id'])
        assert old['reply_markup']['inline_keyboard']==[]
        result=env.tg.text(GROUP,row['receipt_message_id'])
        assert '圆满收官' in result and 'admin' in result and '&lt;小1&amp;&gt;' in result and '10' in result
        for secret in ('余额','private-login',env.uids[2],'截止'):assert secret not in result
        unpins=[p for m,p in env.tg.calls if m=='unpinChatMessage']
        assert unpins==[{'chat_id':GROUP,'message_id':row['card_message_id']}]
        assert not any(m=='unpinAllChatMessages' for m,p in env.tg.calls)
        ledger=copy.deepcopy(env.db.query('SELECT * FROM points_ledger'))
        await press(env,row,'rpclaim',2,original);await env.bot._packet_tick()
        assert env.db.query('SELECT * FROM points_ledger')==ledger and cash(env)==initial
        assert len([p for m,p in env.tg.calls if m=='sendMessage' and '圆满收官' in p.get('text','')])==1
    asyncio.run(run())


def test_pin_permission_failure_does_not_block_claim_or_lie(env):
    async def run():
        base=env.bot._call
        async def denied(method,body,**kw):
            if method in ('pinChatMessage','unpinChatMessage'):
                env.tg.calls.append((method,copy.deepcopy(body)));CALL_DELIVERY.set({'state':'failed','reason':'not enough rights'});return None
            return await base(method,body,**kw)
        env.bot._call=denied
        before=cash(env);row=await issue(env,parts=1)
        assert row['pin_state']=='failed'
        row=await press(env,row,'rpclaim',2)
        assert row['status']=='exhausted' and row['receipt_state']=='sent' and cash(env)==before
        assert '已置顶' not in env.tg.text(GROUP,row['card_message_id'])
        assert env.services.registry.get('red_packets').readonly_status()['置顶未确认']==1
        await env.bot._packet_tick()
        assert len([p for m,p in env.tg.calls if m=='pinChatMessage'])==1
    asyncio.run(run())


def test_concurrent_last_claim_two_db_writers_one_receipt_intent_and_payout(env):
    row=asyncio.run(issue(env,parts=1));original=card(env,row);before=cash(env)
    dbs=[Database(env.db.path),Database(env.db.path)]
    def claim(pair):
        try:return local(env,pair[0]).claim(row['nonce'],env.actors[pair[1]],original)
        except PacketError:return None
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(claim,[(dbs[0],0),(dbs[1],2)]))
    finally:
        for db in dbs:db.close()
    assert sum(x is not None for x in outcomes)==1
    assert env.db.one('SELECT COUNT(*) n FROM red_packet_claims')['n']==1
    assert svc(env).get(row['nonce'])['receipt_state']=='pending' and cash(env)==before
    asyncio.run(env.bot._packet_tick());asyncio.run(env.bot._packet_tick())
    assert len([p for m,p in env.tg.calls if m=='sendMessage' and '圆满收官' in p.get('text','')])==1


def test_unknown_receipt_not_blind_repeat_after_reopen(env):
    async def run():
        base=env.bot._call
        async def lost(method,body,**kw):
            if method=='sendMessage' and '圆满收官' in body.get('text',''):
                env.tg.calls.append((method,copy.deepcopy(body)));CALL_DELIVERY.set({'state':'unknown'});return None
            return await base(method,body,**kw)
        env.bot._call=lost
        row=await issue(env,parts=1);row=await press(env,row,'rpclaim',2)
        assert row['receipt_state']=='unknown' and row['receipt_message_id'] is None
        await env.bot._packet_tick();db=Database(env.db.path);db.close();await env.bot._packet_tick()
        assert len([p for m,p in env.tg.calls if m=='sendMessage' and '圆满收官' in p.get('text','')])==1
        assert env.services.registry.get('red_packets').readonly_status()['结果需核实']==1
    asyncio.run(run())


def test_restart_sending_receipt_becomes_unknown_late_ack_not_falsely_confirmed(env,monkeypatch):
    async def run():
        row=await issue(env,parts=1);effects=env.bot._packet_effects
        async def quiet(nonce):pass
        env.bot._packet_effects=quiet
        row=await press(env,row,'rpclaim',2)
        delivery=PacketDelivery(svc(env));job=delivery.claim(row['nonce'],'receipt')
        assert job and svc(env).get(row['nonce'])['receipt_state']=='sending'
        clock=time.time();monkeypatch.setattr('time.time',lambda:clock+91)
        db=Database(env.db.path);db.close();env.bot._packet_effects=effects
        await env.bot._packet_tick()
        assert svc(env).get(row['nonce'])['receipt_state']=='unknown'
        delivery.finish(job,{'message_id':999},{})
        assert svc(env).get(row['nonce'])['receipt_state']=='unknown'
        assert not any(m=='sendMessage' and '圆满收官' in p.get('text','') for m,p in env.tg.calls)
    asyncio.run(run())


def test_legacy_snapshot_expiry_refunds_and_old_exhausted_no_new_messages(env,monkeypatch):
    async def run():
        initial=cash(env)
        row=await issue(env)
        # A real pre-upgrade active snapshot: expiry and no newly scheduled effects.
        end=time.time()+60
        env.db.execute("UPDATE red_packets SET permanent=0,expires_at=?,pin_state='',unpin_state='',receipt_state='' WHERE nonce=?",(end,row['nonce']))
        original=card(env,row);original['message_thread_id']=0
        row=await press(env,row,'rpclaim',2,original)
        monkeypatch.setattr('time.time',lambda:end+1)
        await env.bot._packet_tick();await env.bot._packet_tick()
        row=svc(env).get(row['nonce'])
        assert row['status']=='expired' and row['refunded']==10 and cash(env)==initial
        assert not any(m=='sendMessage' and '圆满收官' in p.get('text','') for m,p in env.tg.calls)
    asyncio.run(run())


def test_receipt_pagination_all_two_hundred_names_bounded_and_exact_card_guard(env):
    row={'nonce':'static','total':1000000,'parts':200,'mode':'equal'}
    claims=[{'display_name':f'长名字{n}'+('界'*100),'amount':5000,'slot':n} for n in range(200)]
    found=[]
    for page in range(20):
        result=receipt_view(row,claims,page)
        assert len(result['text'].encode('utf-16-le'))//2<4096
        for c in claims[page*10:(page+1)*10]:assert c['display_name'][:39]+'…' in result['text']
        found.extend(claims[page*10:(page+1)*10])
    assert len(found)==200
    # Single-page receipts have no free-form page action; context must bind known receipt ID.
    async def run():
        current=await issue(env,parts=1);current=await press(env,current,'rpclaim',2)
        with pytest.raises(PacketError):PacketDelivery(svc(env)).page(current['nonce'],env.actors[0],card(env,current),0)
    asyncio.run(run())
