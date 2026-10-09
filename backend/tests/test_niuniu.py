"""Actual five-card command/callbacks with isolated members, TG and two SQLite writers."""
import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from itertools import combinations

import pytest
from PIL import Image
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_tg_interaction_context import GROUP

from app.core.db import Database
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.niuniu import CATEGORIES, NiuniuService, rank, strength
from app.modules.points import PointsService
from app.modules.report_delivery import CALL_DELIVERY


@pytest.fixture
def env(request):
    e=request.getfixturevalue('bw_env')
    e.services.registry.save('niuniu',enabled=True)
    e.photos=[]
    async def photo(method,fields,files,**kw):
        image=files['photo'][1]
        with Image.open(BytesIO(image)) as parsed:
            assert parsed.width==1100 and parsed.format=='PNG';parsed.verify()
        e.photos.append(copy.deepcopy(fields))
        return await e.tg.call(method,fields)
    e.bot._call_multipart=photo
    return e


def svc(e):return e.bot._niuniu_service()


def cash(e):
    return sum(e.services.points.balances().values())+(e.db.one('SELECT SUM(amount) n FROM play_escrows')['n'] or 0)+(e.db.one('SELECT SUM(amount) n FROM play_funds')['n'] or 0)


def message(e,text='/牛牛',index=0,mid=1001,topic=0):
    return {'chat':{'id':GROUP,'type':'supergroup'},'from':e.actors[index],'text':text,'message_id':mid,**({'is_topic_message':True,'message_thread_id':topic} if topic else {'message_thread_id':mid+100})}


async def create(e,text='/牛牛',**kw):
    await e.bot._dispatch_update({'message':message(e,text,**kw)})
    return e.db.one('SELECT * FROM niuniu_rounds ORDER BY created_at DESC LIMIT 1')


def card(e,row):
    m=copy.deepcopy(e.tg.message(GROUP,row['card_message_id']))
    m.update(chat={'id':GROUP,'type':'supergroup'},message_id=row['card_message_id'],reply_to_message={'message_id':row['command_message_id']})
    m['from']={'id':123,'is_bot':True}
    if row['thread_id']:m.update(is_topic_message=True,message_thread_id=row['thread_id'])
    else:m['message_thread_id']=991
    return m


async def click(e,row,op,index=0,original=None):
    await e.bot._dispatch_update({'callback_query':{'id':'nn-'+op,'data':f'nn:{row["nonce"]}:{op}','from':e.actors[index],'message':original or card(e,row)}})
    return svc(e).get(row['nonce'])


def c(value,suit=0):return suit*13+(12 if value==1 else value-2)


def test_all_eleven_types_and_fixed_rank_suit_without_special_hands():
    samples={}
    for hand in combinations(range(20),5):
        key=strength(hand)[0];samples.setdefault(key,hand)
        if len(samples)==11:break
    assert set(samples)==set(range(11)) and len(CATEGORIES)==11
    for key,hand in samples.items():
        points=[min(rank(x),10) for x in hand]
        independent=0
        for triple in combinations(range(5),3):
            if sum(points[i] for i in triple)%10==0:
                independent=sum(points[i] for i in range(5) if i not in triple)%10 or 10
        assert strength(hand)[0]==independent==key
        assert strength(hand[::-1])==strength(hand)
    assert strength([c(11),c(12),c(13),c(11,1),c(12,1)])[0]==10
    # Five small cards have no extra rank; four of a kind is just ordinary bull arithmetic.
    assert strength([c(1),c(1,1),c(1,2),c(1,3),c(2)])[0]==0
    assert strength([c(2),c(2,1),c(2,2),c(2,3),c(4)])[0]==0
    same=[c(10),c(6),c(4),c(11),c(1)]
    royal=[c(10,1),c(6,1),c(4,1),c(13,1),c(1,1)]
    assert strength(royal)[0]==strength(same)[0] and strength(royal)>strength(same)
    spade=[c(10,2),c(6,2),c(4,2),c(13),c(1,2)]
    assert strength(spade)>strength(royal)
    assert rank(c(1))==1 and rank(c(13))==13
    for hand in ([0,1,2],[0,0,1,2,3],[0,1,2,3,52],[True,1,2,3,4]):
        with pytest.raises(ValueError):strength(hand)


@pytest.mark.parametrize('text,stake',[('/牛牛',10),('/牛牛 100',100)])
def test_actual_fixed_join_freeze_manual_public_five_reveal_and_repeated_callbacks(env,text,stake):
    async def run():
        initial=cash(env);balances=[env.services.points.balance(u) for u in env.uids]
        row=await create(env,text)
        assert row['stake']==stake and env.services.points.balance(env.uids[0])==balances[0]-stake
        assert row['thread_id']==0
        origin=card(env,row)
        row=await click(env,row,'join',1)
        assert env.services.points.balance(env.uids[1])==balances[1]-stake
        row=await click(env,row,'join',1,origin)
        assert len(svc(env).players(row['nonce']))==2
        await click(env,row,'start',1)
        assert svc(env).get(row['nonce'])['state']=='lobby'
        row=await click(env,row,'start',0)
        players=svc(env).players(row['nonce'])
        assert row['state']=='settled' and len(players)==2
        assert all(len(json.loads(p['cards_json']))==5 for p in players)
        assert len({x for p in players for x in json.loads(p['cards_json'])})==10
        assert sum(p['result_amount'] for p in players)==stake*2
        winner=max(players,key=lambda p:strength(json.loads(p['cards_json'])))
        assert winner['result_amount']==stake*2
        ledger=copy.deepcopy(env.services.points.ledger(env.uids[0]))
        await click(env,row,'start',0,origin);await click(env,row,'join',1,origin)
        assert env.services.points.ledger(env.uids[0])==ledger and cash(env)==initial
        body=env.tg.text(GROUP,row['card_message_id'])
        assert '五张' in body and '揭晓' in body and '获得' in body
        for old in ('炸金花','跟注','加注','看牌','余额','private-login'):assert old not in body
        assert len(env.photos)==1
        await env.bot._niuniu_tick();assert len(env.photos)==1
    asyncio.run(run())


def test_exit_rejoin_creator_cancel_and_timeout_atomic_refund(env):
    async def run():
        initial=cash(env);base=env.services.points.balance(env.uids[1])
        row=await create(env)
        row=await click(env,row,'join',1)
        row=await click(env,row,'leave',1)
        assert env.services.points.balance(env.uids[1])==base
        row=await click(env,row,'join',1)
        assert env.services.points.balance(env.uids[1])==base-10
        row=await click(env,row,'leave',0)
        assert row['state']=='cancelled' and env.services.points.balance(env.uids[1])==base and cash(env)==initial
        row=await create(env,mid=1002)
        row=await click(env,row,'join',1)
        svc(env).expire(row['nonce'],now=row['expires_at']+1)
        svc(env).expire(row['nonce'],now=row['expires_at']+2)
        assert svc(env).get(row['nonce'])['state']=='cancelled' and cash(env)==initial
        assert env.db.one("SELECT SUM(amount) n FROM play_escrows WHERE scope='niuniu'")['n']==0
    asyncio.run(run())


def local(e,db):
    return NiuniuService(db,MemberService(db,GroupService(db)),PointsService(db),lambda:e.services.registry.config('niuniu'),lambda:True,e.bot._group_chat_allowed,'123')


def test_full_fifth_join_and_host_start_two_connections_compete_once(env):
    async def prepare():
        row=await create(env)
        for i in range(1,4):row=await click(env,row,'join',i)
        return row
    row=asyncio.run(prepare());m=card(env,row);before=cash(env)
    conns=[Database(env.db.path),Database(env.db.path)]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda pair:local(env,pair[0]).lobby(row['nonce'],env.actors[pair[1]],m,'start' if pair[1]==0 else 'join'),[(conns[0],0),(conns[1],4)]))
    finally:
        for db in conns:db.close()
    row=svc(env).get(row['nonce']);players=svc(env).players(row['nonce'])
    assert row['state']=='settled' and len(players) in (4,5) and cash(env)==before
    assert env.db.one("SELECT COUNT(*) n FROM play_cash_events WHERE ref=? AND operation='payout'",('niuniu:'+row['nonce'],))['n']==1
    assert len({x for p in players for x in json.loads(p['cards_json'])})==len(players)*5


def test_five_auto_not_six_wrong_original_card_and_legacy_callbacks(env):
    async def run():
        row=await create(env,topic=44);original=card(env,row)
        wrong=copy.deepcopy(original);wrong['message_id']+=1
        await click(env,row,'join',1,wrong)
        assert len(svc(env).players(row['nonce']))==1
        for i in range(1,5):row=await click(env,row,'join',i)
        assert row['state']=='settled' and len(svc(env).players(row['nonce']))==5
        before=cash(env)
        await click(env,row,'join',5,original)
        await env.bot._dispatch_update({'callback_query':{'id':'old','data':f'pg:{row["nonce"]}:0:start','from':env.actors[0],'message':original}})
        assert len(svc(env).players(row['nonce']))==5 and cash(env)==before
        await env.bot._dispatch_update({'message':message(env,'/炸金花 10',mid=999)})
        assert not env.db.query("SELECT * FROM play_rounds WHERE kind='poker'")
    asyncio.run(run())


def test_low_points_exact_creation_and_no_partial_rows(env):
    async def run():
        for index in (0,1):
            env.services.points.add(env.uids[index],30-env.services.points.balance(env.uids[index]),'isolated.balance')
        assert await create(env,'/牛牛 100') is None
        assert not env.db.query("SELECT * FROM play_escrows WHERE scope='niuniu'")
        row=await create(env,mid=1002);row=await click(env,row,'join',1);row=await click(env,row,'start')
        assert row['state']=='settled' and cash(env)>=60
        env.services.points.add(env.uids[2],9-env.services.points.balance(env.uids[2]),'isolated.balance')
        await create(env,mid=1003,index=2)
        assert env.db.one('SELECT COUNT(*) n FROM niuniu_rounds')['n']==1
        assert any('需 10' in p.get('text','') for m,p in env.tg.calls if m=='sendMessage')
    asyncio.run(run())


def test_restart_waiting_preserves_share_then_refund_unsafe_running(env):
    async def prepare():
        row=await create(env);return await click(env,row,'join',1)
    row=asyncio.run(prepare());ledger=copy.deepcopy(env.db.query('SELECT * FROM points_ledger'));before=cash(env)
    db=Database(env.db.path)
    try:
        resumed=local(env,db);resumed.expire_due()
        assert resumed.get(row['nonce'])['state']=='lobby' and db.query('SELECT * FROM points_ledger')==ledger
        db.execute("UPDATE niuniu_rounds SET state='running' WHERE nonce=?",(row['nonce'],))
        resumed.expire_due();resumed.expire_due()
        assert resumed.get(row['nonce'])['state']=='cancelled'
    finally:db.close()
    assert cash(env)==before and env.db.one("SELECT SUM(amount) n FROM play_escrows WHERE scope='niuniu'")['n']==0


def test_unknown_photo_never_repeats_across_tick_and_restart(env):
    async def run():
        async def lost(method,fields,files,**kw):
            env.photos.append(fields);CALL_DELIVERY.set({'state':'unknown'})
        env.bot._call_multipart=lost
        row=await create(env);row=await click(env,row,'join',1);row=await click(env,row,'start')
        assert svc(env).get(row['nonce'])['photo_state']=='unknown'
        await env.bot._niuniu_tick();db=Database(env.db.path);db.close();await env.bot._niuniu_tick()
        assert len(env.photos)==1 and row['state']=='settled'
    asyncio.run(run())


def test_two_new_joiners_compete_for_last_seat_no_sixth_debit(env):
    async def prepare():
        row=await create(env)
        for i in range(1,4):row=await click(env,row,'join',i)
        return row
    row=asyncio.run(prepare());m=card(env,row);before=cash(env)
    original={i:env.services.points.balance(env.uids[i]) for i in (4,5)}
    conns=[Database(env.db.path),Database(env.db.path)]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda pair:local(env,pair[0]).lobby(row['nonce'],env.actors[pair[1]],m,'join'),[(conns[0],4),(conns[1],5)]))
    finally:
        for db in conns:db.close()
    players=svc(env).players(row['nonce']);ids={p['user_id'] for p in players}
    assert len(players)==5 and cash(env)==before
    loser=next(i for i in (4,5) if env.uids[i] not in ids)
    assert env.services.points.balance(env.uids[loser])==original[loser]
    assert env.db.one("SELECT COUNT(*) n FROM play_cash_events WHERE ref=? AND operation='payout'",('niuniu:'+row['nonce'],))['n']==1


@pytest.mark.parametrize('fault',['member','dealer'])
def test_unsafe_start_refunds_all_without_deal_or_second_settlement(env,monkeypatch,fault):
    async def run():
        before=cash(env)
        row=await create(env);row=await click(env,row,'join',1)
        if fault=='member':
            env.members.upsert(env.uids[1], 'private-login', {'status':'suspended'})
        else:
            def broken(self,deck):raise RuntimeError('isolated shuffle failure')
            monkeypatch.setattr('secrets.SystemRandom.shuffle',broken)
        row=await click(env,row,'start')
        assert row['state']=='cancelled'
        assert env.db.one("SELECT SUM(amount) n FROM play_escrows WHERE scope='niuniu'")['n']==0
        assert not env.photos and cash(env)==before
        assert all(p['cards_json']=='[]' for p in svc(env).players(row['nonce']))
    asyncio.run(run())
