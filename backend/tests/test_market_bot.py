"""Actual Telegram handlers, owned persistent confirmations/inputs and real PNG transport."""
import asyncio
import copy
import json
import os
import time
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from test_blackwhite import env as bw_env  # noqa: F401
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_tg_interaction_context import GROUP

from app.core.db import Database
from app.modules.market_views import chart, news_tick


@pytest.fixture
def env(request):
    e=request.getfixturevalue('bw_env')
    e.services.registry.save('stock_market',enabled=True,config={'fee_bps':50})
    e.services.registry.get('stock_market').ctx.telegram=e.bot
    for uid in e.uids:e.services.points.add(uid,20000,'isolated.fixture')
    e.mid=800
    e.photos=[]
    async def multipart(method,fields,files,**kw):
        assert method=='sendPhoto' and files['photo'][2]=='image/png'
        data=files['photo'][1]
        with Image.open(BytesIO(data)) as image:
            assert image.format=='PNG' and image.size==(1040,670)
            image.verify()
        e.photos.append({'fields':copy.deepcopy(fields),'bytes':data})
        return await e.tg.call(method,fields)
    e.bot._call_multipart=multipart
    return e


def service(e):return e.bot._market_service()


def message(e,value,index=0,group=False,mid=None,reply=None):
    if mid is None:e.mid+=1;mid=e.mid
    actor=e.actors[index]
    result={'from':actor,'chat':{'id':GROUP if group else actor['id'],'type':'supergroup' if group else 'private'},'message_id':mid,'text':value}
    if group:result.update(message_thread_id=27,is_topic_message=True)
    if reply:result['reply_to_message']={'message_id':reply,'from':{'id':123,'is_bot':True}}
    return result


def panel(e,index=0,group=False):
    return e.db.one('SELECT * FROM market_panels WHERE tg_id=? AND chat_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',(str(e.actors[index]['id']),str(GROUP if group else e.actors[index]['id'])))


async def cmd(e,value,index=0,group=False,**kw):
    await e.bot._dispatch_update({'message':message(e,value,index,group,**kw)})
    return panel(e,index,group)


def card(e,row):
    m=copy.deepcopy(e.tg.message(row['chat_id'],row['message_id']))
    m.update(chat={'id':int(row['chat_id']),'type':'private' if row['chat_id']==row['tg_id'] else 'supergroup'},
             message_id=row['message_id'],**{'from':{'id':123,'is_bot':True}})
    if row['thread_id']:m['is_topic_message']=True
    return m


def body(e,row):return e.tg.text(row['chat_id'],row['message_id'])


async def click(e,row,op,arg=None,index=None,original=None):
    if index is None:index=next(i for i,a in enumerate(e.actors) if str(a['id'])==row['tg_id'])
    if arg is None and op in ('confirm','dismiss'):
        action_message=original or card(e,row)
        choices=[b.get('callback_data','') for line in (action_message.get('reply_markup') or {}).get('inline_keyboard',[]) for b in line if b.get('callback_data','').startswith(f'st:{row["nonce"]}:{op}:')]
        arg=choices[0].rsplit(':',1)[1] if choices else json.loads(row['payload_json'])['intent']
    data=f'st:{row["nonce"]}:{op}'+(':'+str(arg) if arg is not None else '')
    await e.bot._dispatch_update({'callback_query':{'id':'market-test','data':data,'from':e.actors[index],'message':original or card(e,row)}})
    return e.bot._market_panel(row['nonce'])


async def transact(e,index,op,q,price=None,code='MD001'):
    command={'ipo':'/认购','buy':'/买入','sell':'/卖出'}[op]
    row=await cmd(e,f'{command} {code} {q}'+(' '+str(price) if price is not None else ''),index=index)
    assert json.loads(row['payload_json'])['view']=='confirm'
    row=await click(e,row,'confirm',index=index)
    assert json.loads(row['payload_json'])['view']=='done'
    return row


def assets(e):
    return {table:e.db.query('SELECT * FROM '+table+' ORDER BY rowid') for table in ('points_ledger','play_escrows','play_funds','market_positions','market_orders','market_subscriptions','market_trades','market_stock_moves')}


def test_full_received_update_search_watch_ipo_buy_sell_cost_pnl_orders_cancel_history_charts(env):
    async def run():
        row=await cmd(env,'/股票 新能源')
        assert '曦光储能' in body(env,row) and '暂无成交' in body(env,row)
        row=await click(env,row,'company','MD001')
        assert '固定认购价 10' in body(env,row) and '库存 1000' in body(env,row)
        old=card(env,row)
        row=await click(env,row,'favorite','MD001_on')
        await click(env,row,'favorite','MD001_on',original=old)
        assert env.db.one('SELECT COUNT(*) n FROM market_watch')['n']==1
        await transact(env,0,'ipo',12)
        await transact(env,0,'sell',5,12)
        await transact(env,1,'buy',3,15)
        row=await cmd(env,'/委托',index=0)
        sell=env.db.one("SELECT * FROM market_orders WHERE side='sell' AND state='open'")
        assert sell['remaining']==2
        row=await click(env,row,'cancel',sell['nonce'])
        assert service(env).position(env.uids[0],'MD001')['locked']==0
        assert '已撤单' in body(env,row)
        await transact(env,1,'sell',1,14)
        await transact(env,2,'buy',1,14)
        row=await cmd(env,'/持仓',index=1)
        own=body(env,row)
        assert '2 股' in own and '成本 25' in own and '已实现 +2' in own and '浮盈 +3' in own
        assert 'never-public' not in own and 'private-login' not in own and '当前余额' not in own
        row=await cmd(env,'/成交',index=1)
        assert '买入 3 股 × 12' in body(env,row) and '卖出 1 股 × 14' in body(env,row)
        row=await cmd(env,'/成交',index=0)
        assert '认购 12 股 × 10' in body(env,row) and '卖出 3 股 × 12' in body(env,row)
        public=await cmd(env,'/股票 MD001',group=True)
        text_public=body(env,public)
        for word in ('成本','浮盈','已实现','冻结','private-login',env.uids[0]):assert word not in text_public
        await click(env,public,'chart','MD001')
        traded=env.photos[-1]
        assert traded['fields']['chat_id']==str(GROUP) and traded['fields']['message_thread_id']==27
        trades=env.db.query("SELECT * FROM market_trades WHERE code='MD001' ORDER BY id")
        assert len(trades)==2 and [t['price'] for t in trades]==[12,14]
        assert traded['bytes']==chart(service(env).company('MD001'),trades)
        row=await cmd(env,'/股票 MD060')
        await click(env,row,'chart','MD060')
        empty=env.photos[-1]
        assert empty['bytes']==chart(service(env).company('MD060'),[])
        if os.environ.get('GAMES_MARKET_ARTIFACTS'):
            path=Path(os.environ['GAMES_MARKET_ARTIFACTS'])
            (path/'market-real-trades.png').write_bytes(traded['bytes'])
            (path/'market-no-trades.png').write_bytes(empty['bytes'])
            (path/'market-effect.json').write_text(json.dumps({'public_company':text_public,'private_portfolio':own,'charts_actual_multipart':True,'two_actual_prices':[12,14],'no_trade_chart_no_fake_price':True},ensure_ascii=False,indent=2)+'\n')
    asyncio.run(run())


def test_button_numeric_conversation_duplicate_updates_and_original_confirmation_no_extra_charge(env):
    async def run():
        row=await cmd(env,'/股票 MD001')
        row=await click(env,row,'input','ipo_MD001')
        assert '请输入整数股数' in body(env,row)
        qmsg=message(env,'5',reply=row['message_id'])
        await env.bot._dispatch_update({'message':qmsg})
        row=env.bot._market_panel(row['nonce'])
        saved=card(env,row)
        assert '支付 51' in body(env,row)
        await env.bot._dispatch_update({'message':qmsg})
        assert env.db.one('SELECT COUNT(*) n FROM market_intents')['n']==1
        row=await click(env,row,'confirm')
        count=env.db.one('SELECT COUNT(*) n FROM points_ledger')['n']
        await click(env,row,'confirm',original=saved)
        assert env.db.one('SELECT COUNT(*) n FROM points_ledger')['n']==count
        assert service(env).position(env.uids[0],'MD001')['shares']==5
        row=await cmd(env,'/股票 MD002')
        row=await click(env,row,'input','buy_MD002')
        qmsg=message(env,'4',reply=row['message_id'])
        await env.bot._dispatch_update({'message':qmsg})
        assert '请输入整数每股限价' in body(env,row)
        await env.bot._dispatch_update({'message':qmsg})
        assert env.db.one("SELECT quantity FROM market_inputs WHERE phase='price'")['quantity']==4
        assert env.db.one('SELECT COUNT(*) n FROM market_intents')['n']==1
        row=await cmd(env,'9',reply=row['message_id'])
        assert '4 股 · 9' in body(env,row)
        row=await click(env,row,'confirm')
        assert '待成交 4 股' in body(env,row) and '已成交 0' not in body(env,row)
        assert env.db.one('SELECT COUNT(*) n FROM market_orders')['n']==1
    asyncio.run(run())


def test_repeated_financial_command_maps_to_same_intent_and_never_two_orders(env):
    async def run():
        msg=message(env,'/买入 MD001 3 12')
        await env.bot._dispatch_update({'message':msg})
        first=panel(env)
        await env.bot._dispatch_update({'message':msg})
        second=panel(env)
        assert json.loads(first['payload_json'])['intent']==json.loads(second['payload_json'])['intent']
        await click(env,first,'confirm')
        await click(env,second,'confirm')
        assert env.db.one('SELECT COUNT(*) n FROM market_orders')['n']==1
        assert env.db.one('SELECT COUNT(*) n FROM play_escrows')['n']==1
    asyncio.run(run())


@pytest.mark.parametrize('change',['actor','chat','mid','sender','not_bot','buttons','forward'])
def test_confirmation_personal_original_card_tg_bot_identity_cannot_be_spoofed(env,change):
    async def run():
        row=await cmd(env,'/认购 MD001 3')
        m=card(env,row);idx=0
        if change=='actor':idx=1
        elif change=='chat':m['chat']['id']=GROUP;m['chat']['type']='supergroup'
        elif change=='mid':m['message_id']+=1
        elif change=='sender':m['from']['id']+=1
        elif change=='not_bot':m['from']['is_bot']=False
        elif change=='buttons':m['reply_markup']={}
        else:m['forward_origin']={'type':'user'}
        before=assets(env)
        await click(env,row,'confirm',index=idx,original=m)
        assert assets(env)==before
        assert not env.db.query('SELECT * FROM market_subscriptions')
    asyncio.run(run())


@pytest.mark.parametrize('command',['/持仓','/委托','/成交','/自选','/认购 MD001 2','/买入 MD001 2 10','/卖出 MD001 2 10'])
def test_group_personal_commands_never_query_or_show_assets_and_only_link_private(env,command):
    async def run():
        before=assets(env)
        await cmd(env,command,group=True)
        payload=[p for method,p in env.tg.calls if method=='sendMessage'][-1]
        assert '本人私聊' in payload['text'] and payload['chat_id']==GROUP
        assert 'start=market' in payload['reply_markup']['inline_keyboard'][0][0]['url']
        for word in ('可用积分','成本','已实现','当前余额',env.uids[0]):assert word not in payload['text']
        assert assets(env)==before and not env.db.query('SELECT * FROM market_intents')
    asyncio.run(run())


def test_public_panel_cannot_be_tricked_into_personal_view_even_with_injected_button(env):
    async def run():
        row=await cmd(env,'/股票 MD001',group=True)
        m=card(env,row)
        m['reply_markup']['inline_keyboard'].append([{'text':'forged','callback_data':f'st:{row["nonce"]}:view:hold'}])
        old=env.tg.text(GROUP,row['message_id'])
        await click(env,row,'view','hold',original=m)
        assert env.tg.text(GROUP,row['message_id'])==old
    asyncio.run(run())


def test_old_company_actions_keep_named_target_and_wrong_numeric_reply_rejected(env):
    async def run():
        row=await cmd(env,'/股票 MD001')
        old=card(env,row)
        # Navigate the same panel to another company using a new command instead.
        other=await cmd(env,'/股票 MD002')
        await click(env,other,'input','buy_MD002')
        await cmd(env,'5',reply=row['message_id'])
        assert env.db.one('SELECT phase FROM market_inputs')['phase']=='quantity'
        await click(env,row,'input','buy_MD001',original=old)
        assert env.db.one('SELECT code FROM market_inputs')['code']=='MD001'
        await cmd(env,'取消')
        assert not env.db.query('SELECT * FROM market_inputs')
        assert not env.db.query('SELECT * FROM market_orders')
    asyncio.run(run())


def test_old_confirm_never_uses_new_panel_intent_and_cancelled_confirmation_stays_cancelled(env):
    async def run():
        row=await cmd(env,'/买入 MD001 2 10')
        old=card(env,row)
        intent_a=json.loads(row['payload_json'])['intent']
        row=await click(env,row,'view','search')
        row=await click(env,row,'company','MD002')
        row=await click(env,row,'input','buy_MD002')
        await cmd(env,'3',reply=row['message_id'])
        row=await cmd(env,'11',reply=row['message_id'])
        intent_b=json.loads(row['payload_json'])['intent']
        assert intent_b!=intent_a
        row=await click(env,row,'confirm',original=old)
        orders=env.db.query('SELECT * FROM market_orders')
        assert len(orders)==1 and orders[0]['code']=='MD001' and orders[0]['quantity']==2
        assert service(env).intent(intent_b)['state']=='preview'
        cancelled=await cmd(env,'/认购 MD003 2')
        original=card(env,cancelled)
        cancelled=await click(env,cancelled,'dismiss')
        before=assets(env)
        await click(env,cancelled,'confirm',original=original)
        assert assets(env)==before
        assert service(env).position(env.uids[0],'MD003')['shares']==0
    asyncio.run(run())


def test_inputs_expire_and_db_reopen_keeps_exact_private_confirmation(env,monkeypatch):
    clock=[time.time()]
    monkeypatch.setattr(time,'time',lambda:clock[0])
    async def run():
        row=await cmd(env,'/股票 MD001')
        row=await click(env,row,'input','buy_MD001')
        clock[0]+=600
        await cmd(env,'5')
        assert not env.db.query('SELECT * FROM market_inputs')
        row=await cmd(env,'/买入 MD001 4 12')
        db=Database(env.db.path)
        assert db.one('SELECT * FROM market_panels WHERE nonce=?',(row['nonce'],))==row
        assert db.one('SELECT * FROM market_intents WHERE nonce=?',(json.loads(row['payload_json'])['intent'],))['quantity']==4
        db.close()
        clock[0]+=120
        await click(env,row,'confirm')
        assert not env.db.query('SELECT * FROM market_orders')
    asyncio.run(run())


def test_all_60_search_pages_and_long_portfolio_history_orders_reachable(env):
    async def run():
        row=await cmd(env,'/start market')
        row=await click(env,row,'view','search')  # all 60 remain reachable from focused home
        names=[]
        for i in range(8):
            if i:row=await click(env,row,'page',i)
            b=body(env,row)
            names+=[f'MD{n:03}' for n in range(1,61) if f'MD{n:03}' in b]
            assert len(b.encode('utf-16-le'))//2<4096
        assert len(set(names))==60
        for n in range(1,26):await transact(env,0,'ipo',1,code=f'MD{n:03}')
        for view,expected in (('/持仓','MD025'),('/成交','MD001')):
            row=await cmd(env,view)
            seen=''
            for i in range(4):
                if i:row=await click(env,row,'page',i)
                seen+=body(env,row)
            assert expected in seen
        for n in range(1,18):await transact(env,0,'buy',1,1,code=f'MD{n:03}')
        row=await cmd(env,'/委托')
        for i in range(1,3):row=await click(env,row,'page',i)
        assert 'MD001' in body(env,row) and '3/3页' in body(env,row)
    asyncio.run(run())


def test_hour_news_idempotent_no_catchup_or_asset_price_effects_and_actual_plugin_summary(env):
    async def run():
        await transact(env,0,'ipo',4)
        before=assets(env)
        assert news_tick(env.db,now=2000000)
        assert not news_tick(env.db,now=2000001)
        assert news_tick(env.db,now=2000000+24*3600)
        assert env.db.one('SELECT COUNT(*) n FROM market_news')['n']==2
        assert assets(env)==before
        row=await cmd(env,'/股票资讯')
        assert '模拟资讯' in body(env,row)
        summary=await env.services.registry.get('stock_market').run({})
        assert summary['公司']==60 and summary['真实成交笔数']==0
        assert summary['发行资金回收']==40 and summary['手续费回收']==1
        assert assets(env)==before
    asyncio.run(run())


def test_one_actual_trade_is_one_chart_point_and_photo_failure_not_reported_as_success(env,monkeypatch):
    async def run():
        await transact(env,0,'ipo',2)
        await transact(env,0,'sell',1,12)
        await transact(env,1,'buy',1,12)
        row=await cmd(env,'/股票 MD001')
        await click(env,row,'chart','MD001')
        trades=env.db.query('SELECT * FROM market_trades ORDER BY id')
        assert len(trades)==1
        assert env.photos[-1]['bytes']==chart(service(env).company('MD001'),trades)
        for line in card(env,row)['reply_markup']['inline_keyboard']:
            for button in line:
                if button.get('callback_data'):assert len(button['callback_data'].encode())<=64
        original=assets(env)
        async def failed(method,fields,files,**kw):
            assert method=='sendPhoto'
        monkeypatch.setattr(env.bot,'_call_multipart',failed)
        await click(env,row,'chart','MD001')
        assert any('走势图未送达' in p.get('text','') for method,p in env.tg.calls if method=='answerCallbackQuery')
        assert assets(env)==original
    asyncio.run(run())


def test_backend_halt_config_fresh_reject_then_worker_refunds_without_overwriting_issuance(env):
    async def run():
        await transact(env,0,'ipo',5)
        await transact(env,0,'buy',2,1)
        before_inventory=service(env).company('MD001')['inventory']
        env.services.registry.save('stock_market',config={'halted_codes':'md001'})
        await cmd(env,'/买入 MD001 2 1')
        assert env.db.one('SELECT COUNT(*) n FROM market_orders')['n']==1
        await env.bot._market_tick()
        assert env.db.one('SELECT state FROM market_orders')['state']=='halted'
        assert service(env).company('MD001')['inventory']==before_inventory
        assert env.db.one("SELECT SUM(amount) n FROM play_escrows WHERE scope='market'")['n']==0
        with pytest.raises(ValueError):env.services.registry.save('stock_market',config={'halted_codes':'MD061'})
        with pytest.raises(ValueError):env.services.registry.save('stock_market',config={'fee_bps':101})
    asyncio.run(run())
