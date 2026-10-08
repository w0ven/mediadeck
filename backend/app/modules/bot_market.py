"""Dedicated original market panels. Private economics never render in a group."""
from __future__ import annotations

import json
import math
import re
import secrets
import time
from html import escape

from app.modules.economy_rules import economy_write, encode
from app.modules.group_points import GroupPointsError, reliable_user
from app.modules.market import MarketService
from app.modules.market_data import DEFAULTS, fee
from app.modules.market_views import chart, market_help, news_tick
from app.modules.play_money import PlayError, integer

PAGE = 8


def text(value): return escape(str(value))


class MarketBotMixin:
    def _market_service(self):
        if self._db is None or self._points is None: raise PlayError('积分服务暂不可用')
        self._check_bot_identity()
        return MarketService(self._db, self._members, self._points,
                             lambda: self._plugins.config('stock_market') if self._plugins else {},
                             lambda: self._plugin_on('stock_market'), self._active_bot_id)

    def _market_link(self):
        return [[{'text': '私聊股票', 'url': f'https://t.me/{self._bot_username}'+ '?start=market'}]] if self._bot_username else []

    def _market_panel(self, token): return self._db.one('SELECT * FROM market_panels WHERE nonce=?', (token,))

    def _market_context(self, panel, message, actor, data):
        service = self._market_service()
        tg = reliable_user({'from': actor})
        member = self._members.find_by_telegram(tg)
        chat, sender = message.get('chat') or {}, message.get('from') or {}
        actions = [b.get('callback_data') for line in (message.get('reply_markup') or {}).get('inline_keyboard', []) for b in line]
        if (not panel or panel['bot_id'] != self._active_bot_id or str(chat.get('id')) != panel['chat_id']
                or type(message.get('message_id')) is not int or message.get('message_id') != panel['message_id'] or panel['message_id'] is None
                or sender.get('is_bot') is not True or str(sender.get('id')) != self._active_bot_id
                or data not in actions or message.get('forward_origin') or message.get('forward_date')
                or message.get('sender_chat') or message.get('is_automatic_forward')):
            raise PlayError('请使用原Bot股票卡片')
        if chat.get('type') == 'private':
            if str(chat.get('id')) != tg or not member or panel['tg_id'] != tg or panel['user_id'] != member['emby_user_id']:
                raise PlayError('仅本人私聊可操作')
        else:
            if not self._group_chat_allowed(chat): raise PlayError('原群已不在授权范围')
            group, thread = service.group(message)
            if group != panel['chat_id'] or thread != panel['thread_id']: raise PlayError('请使用原群原话题股票卡')
            service.actor(actor)
        return tg

    def _market_owned(self, panel):
        member = self._members.find_by_telegram(panel['tg_id'])
        if not member or member['emby_user_id'] != panel['user_id']: raise PlayError('原账号绑定已变化')
        return {'id': int(panel['tg_id']), 'is_bot': False}

    @staticmethod
    def _market_private(panel):
        if panel['chat_id'] != panel['tg_id']: raise PlayError('请在本人私聊操作')

    async def _market_new_panel(self, message, payload):
        service = self._market_service()
        tg, member = service.actor(message.get('from') or {})
        chat = message.get('chat') or {}
        if chat.get('type') == 'private':
            if str(chat.get('id')) != tg: raise PlayError('仅本人私聊可操作')
            thread = 0
        else: _, thread = service.group(message)
        token = secrets.token_hex(12)
        self._db.execute('INSERT INTO market_panels(nonce,bot_id,user_id,tg_id,chat_id,thread_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
                         (token, self._active_bot_id, member['emby_user_id'], tg, str(chat['id']), thread, encode(payload), time.time()))
        panel = self._market_panel(token)
        await self._market_draw(panel, reply_to=message.get('message_id'))
        return self._market_panel(token)

    def _market_view(self, panel):
        service, p = self._market_service(), json.loads(panel['payload_json'])
        private = panel['chat_id'] == panel['tg_id']
        view, page = p.get('view', 'search'), int(p.get('page', 0))
        keyboard = []
        def button(label, op, arg=''):
            return {'text': label, 'callback_data': f'st:{panel["nonce"]}:{op}'+(':'+str(arg) if arg != '' else '')}
        def pager(rows):
            nonlocal page
            pages=max(1,math.ceil(len(rows)/PAGE));page=max(0,min(page,pages-1))
            nav=[]
            if page: nav.append(button('‹ 上一页','page',page-1))
            if page+1<pages: nav.append(button('下一页 ›','page',page+1))
            if nav: keyboard.append(nav)
            return rows[page*PAGE:(page+1)*PAGE],f'{page+1}/{pages}页'
        if view=='leaderboard': return self._ranking_view(panel,p)
        if view in ('playhub','gamehelp'): return self._play_hub_view(panel,p)
        title='📈 <b>模拟股票</b>'
        if view in ('search','watch'):
            keyword=str(p.get('keyword') or '')[:80]
            if view=='watch':
                self._market_private(panel)
                rows=self._db.query('SELECT c.* FROM market_companies c JOIN market_watch w ON w.code=c.code WHERE w.user_id=? ORDER BY c.code', (panel['user_id'],))
            else:
                like='%'+keyword+'%'
                rows=self._db.query('SELECT * FROM market_companies WHERE code LIKE ? OR name LIKE ? OR sector LIKE ? ORDER BY code',(like,like,like))
            chosen,number=pager(rows)
            body=title+' · '+('自选' if view=='watch' else '公司')+'\n'+number
            if keyword: body+=' · '+text(keyword)
            if not rows: body+='\n\n暂无匹配公司' if view=='search' else '\n\n还没有自选公司'
            buttons=[]
            for c in chosen:
                last=self._db.one('SELECT price FROM market_trades WHERE code=? ORDER BY id DESC LIMIT 1',(c['code'],))
                body+='\n'+text(c['code']+' '+c['name'])+' · '+(str(last['price']) if last else '暂无成交')
                buttons.append(button(c['code']+' · '+c['name'],'company',c['code']))
            keyboard[:0]=[buttons[i:i+2] for i in range(0,len(buttons),2)]
        elif view=='company':
            c=service.company(p['code'])
            if not c: raise PlayError('公司不存在')
            last=self._db.one('SELECT price FROM market_trades WHERE code=? ORDER BY id DESC LIMIT 1',(c['code'],))
            body=title+'\n\n<b>'+text(c['name'])+'</b> · '+c['code']+'\n'+text(c['sector'])+' · 虚构公司'
            body+='\n'+('最新成交 '+str(last['price'])+' 积分/股' if last else '暂无成交')
            body+=f'\n固定认购价 {c["issue_price"]} · 库存 {c["inventory"]} 股'
            bid=self._db.one("SELECT MAX(price) price FROM market_orders WHERE code=? AND side='buy' AND state='open'",(c['code'],))['price']
            ask=self._db.one("SELECT MIN(price) price FROM market_orders WHERE code=? AND side='sell' AND state='open'",(c['code'],))['price']
            body+='\n买 '+(str(bid) if bid is not None else '—')+' / 卖 '+(str(ask) if ask is not None else '—')
            if c['halted']:body+='\n已暂停'
            event=self._db.one('SELECT * FROM market_news WHERE code=? ORDER BY slot DESC LIMIT 1',(c['code'],))
            if event:body+='\n\n模拟资讯 · '+text(event['title'])+'\n'+text(event['body'])
            keyboard.append([button('真实走势','chart',c['code']),button('资讯','view','news')])
            if private:
                watched=self._db.one('SELECT 1 n FROM market_watch WHERE user_id=? AND code=?',(panel['user_id'],c['code']))
                keyboard.append([button('移出自选' if watched else '加入自选','favorite',c['code']+('_off' if watched else '_on'))])
                keyboard.append([button('认购','input','ipo_'+c['code']),button('买入','input','buy_'+c['code']),button('卖出','input','sell_'+c['code'])])
        elif view=='input':
            self._market_private(panel)
            state=self._db.one('SELECT * FROM market_inputs WHERE bot_id=? AND tg_id=? AND panel=?',(self._active_bot_id,panel['tg_id'],panel['nonce']))
            if not state or state['expires_at']<=time.time():raise PlayError('输入已过期，请重新选择操作')
            operation={'ipo':'认购','buy':'买入','sell':'卖出'}[state['operation']]
            body=title+'\n\n'+operation+' '+state['code']+'\n'+('请输入整数股数' if state['phase']=='quantity' else f'股数 {state["quantity"]} · 请输入整数每股限价')
            keyboard.append([button('取消输入','stop')])
        elif view=='confirm':
            self._market_private(panel)
            intent=service.intent(p['intent'])
            if not intent or intent['user_id']!=panel['user_id'] or intent['tg_id']!=panel['tg_id']:raise PlayError('原确认不可用')
            cfg=json.loads(intent['config_json']);op=intent['operation'];gross=intent['quantity']*intent['price'];charge=fee(gross,cfg['fee_bps']) if op!='sell' else 0
            body=title+'\n\n<b>确认'+{'ipo':'认购','buy':'买入','sell':'卖出'}[op]+'</b> · '+intent['code']+f'\n{intent["quantity"]} 股 · {intent["price"]} 积分/股'
            body+='\n'+(f'支付 {gross+charge} 积分' if op=='ipo' else f'最多冻结 {gross+charge} 积分' if op=='buy' else f'冻结 {intent["quantity"]} 股')
            if charge:body+=f' · 含费 {charge}'
            keyboard.append([button('确认','confirm',intent['nonce']),button('取消','dismiss',intent['nonce'])])
        elif view in ('hold','orders','history'):
            self._market_private(panel)
            actor=self._market_owned(panel)
            if view=='hold':
                rows=service.portfolio(actor);chosen,number=pager(rows);body=title+' · 我的持仓\n'+number
                if not rows:body+='\n\n暂无持仓'
                for r in chosen:
                    floating='暂无成交' if r['floating'] is None and r['shares'] else f'{r["floating"]:+d}' if r['floating'] is not None else '—'
                    body+='\n\n'+text(r['name'])+' '+r['code']+f'\n{r["shares"]} 股 · 可卖 {r["shares"]-r["locked"]} · 成本 {r["cost"]}'
                    body+=f'\n已实现 {r["realized"]:+d} · 浮盈 {floating}'
                    keyboard.insert(0,[button(r['code']+' 公司','company',r['code'])])
            elif view=='orders':
                rows=service.orders(actor,include_closed=True);chosen,number=pager(rows);body=title+' · 我的委托\n'+number
                if not rows:body+='\n\n暂无委托'
                states={'open':'待成交','filled':'已成交','cancelled':'已撤单','expired':'已过期','invalid':'已退回','closed':'停市退回','halted':'暂停退回'}
                for r in chosen:
                    body+='\n\n'+r['code']+' · '+('买' if r['side']=='buy' else '卖')+f' {r["price"]} · {r["quantity"]} 股\n'+states[r['state']]+f' · 剩余 {r["remaining"]} 股'
                    if r['state']=='open':keyboard.insert(0,[button('撤单 '+r['code'],'cancel',r['nonce'])])
            else:
                trades=service.trades(actor)
                ipos=self._db.query('SELECT s.*,c.issue_price price_dummy FROM market_subscriptions s JOIN market_companies c ON c.code=s.code WHERE s.user_id=?',(panel['user_id'],))
                rows=sorted([dict(r,kind='trade') for r in trades]+[dict(r,kind='ipo',price=r['price_dummy']) for r in ipos],key=lambda r:(r['created_at'],r['id']),reverse=True)
                chosen,number=pager(rows);body=title+' · 我的成交\n'+number
                if not rows:body+='\n\n暂无成交或认购'
                for r in chosen:
                    label='认购' if r['kind']=='ipo' else '买入' if r['buyer']==panel['user_id'] else '卖出'
                    body+='\n\n'+r['code']+' · '+label+f' {r["quantity"]} 股 × {r["price"]}'
                    if r['fee'] and label!='卖出':body+=f' · 费 {r["fee"]}'
        elif view=='news':
            rows=self._db.query('SELECT n.*,c.name FROM market_news n JOIN market_companies c ON c.code=n.code ORDER BY slot DESC');chosen,number=pager(rows);body=title+' · 模拟资讯\n'+number
            if not rows:body+='\n\n暂无资讯'
            for r in chosen:body+='\n\n'+text(r['name'])+' · '+text(r['title'])+'\n'+text(r['body'])
        elif view=='help':body=market_help(service.config())
        elif view=='done':body=p['text']
        else:raise PlayError('股票页面无效')
        menu=[button('公司','view','search'),button('资讯','view','news'),button('玩法','view','help')]
        keyboard.append(menu)
        if private:keyboard.append([button('持仓','view','hold'),button('委托','view','orders'),button('成交','view','history'),button('自选','view','watch')])
        else:keyboard+=self._market_link()
        return body,keyboard

    async def _market_draw(self,panel,*,reply_to=None):
        body,keyboard=self._market_view(panel)
        fields={'chat_id':panel['chat_id'],'text':body,'parse_mode':'HTML','reply_markup':{'inline_keyboard':keyboard},'disable_web_page_preview':True}
        if panel['thread_id']:fields['message_thread_id']=panel['thread_id']
        initial=panel['message_id'] is None
        if initial:
            if reply_to:fields['reply_parameters']={'message_id':reply_to}
        else:fields['message_id']=panel['message_id']
        result=await self._call('sendMessage' if initial else 'editMessageText',fields)
        if initial and isinstance(result,dict) and type(result.get('message_id')) is int:
            self._db.execute('UPDATE market_panels SET message_id=? WHERE nonce=? AND message_id IS NULL',(result['message_id'],panel['nonce']))
        # Never send a second card after an uncertain edit/send. A fresh command is harmless.

    async def _market_set(self,panel,payload):
        if payload.get('view') in ('watch','input','confirm','hold','orders','history','done'): self._market_private(panel)
        self._db.execute('UPDATE market_panels SET payload_json=? WHERE nonce=?',(encode(payload),panel['nonce']))
        await self._market_draw(self._market_panel(panel['nonce']))

    async def _market_command(self,message):
        parts=str(message.get('text') or '').strip().split();first=parts[0].lower() if parts else '';verb=first.split('@',1)[0]
        if verb=='/stock':verb='/股票'
        commands={'/股票','/认购','/买入','/卖出','/持仓','/委托','/成交','/自选','/股市帮助','/股票资讯'}
        deep=verb=='/start' and len(parts)==2 and parts[1]=='market'
        numeric=bool(parts and (re.fullmatch('[0-9]{1,9}',parts[0]) or parts[0]=='取消'))
        if verb not in commands and not deep and not numeric:return False
        self._check_bot_identity()
        if '@' in first and (not self._bot_username or first.split('@',1)[1]!=self._bot_username.lower()):return True
        chat=message.get('chat') or {}
        if chat.get('type') not in ('private','group','supergroup'):return True
        if chat.get('type')!='private' and not self._group_chat_allowed(chat):return True
        try:
            tg=reliable_user(message)
            if message.get('forward_origin') or message.get('forward_date') or message.get('sender_chat') or message.get('is_automatic_forward'):return True
            integer(message.get('message_id'),'消息ID')
            private=chat.get('type')=='private' and str(chat.get('id'))==tg
            if chat.get('type')=='private' and not private:return True
            service=self._market_service()
            if numeric:
                if not private:return False
                if self._db.one('SELECT 1 n FROM market_input_messages WHERE bot_id=? AND tg_id=? AND message_id=?',(self._active_bot_id,tg,message['message_id'])): return True
                state=self._db.one('SELECT * FROM market_inputs WHERE bot_id=? AND tg_id=?',(self._active_bot_id,tg))
                if not state:return False
                if state['expires_at']<=time.time():
                    self._db.execute('DELETE FROM market_inputs WHERE bot_id=? AND tg_id=?',(self._active_bot_id,tg));raise PlayError('输入已过期，请重新下单')
                panel=self._market_panel(state['panel']);actor=self._market_owned(panel)
                if actor['id']!=message['from']['id']:raise PlayError('原输入本人绑定已变化')
                reply=(message.get('reply_to_message') or {}).get('message_id')
                if reply and reply!=panel['message_id']:raise PlayError('请回复当前股票输入卡')
                if parts[0]=='取消':
                    self._db.execute('DELETE FROM market_inputs WHERE bot_id=? AND tg_id=? AND panel=?',(self._active_bot_id,tg,panel['nonce']))
                    await self._market_set(panel,{'view':'company','code':state['code']});return True
                if len(parts)!=1:raise PlayError('请输入一个整数')
                value=int(parts[0]);cfg={**DEFAULTS,**service.config()}
                integer(value,'股数' if state['phase']=='quantity' else '价格',1,cfg['ipo_limit'] if state['operation']=='ipo' else cfg['order_quantity'] if state['phase']=='quantity' else cfg['max_price'])
                if state['phase']=='quantity' and state['operation']!='ipo':
                    with economy_write(self._db) as conn:
                        conn.execute("UPDATE market_inputs SET quantity=?,phase='price' WHERE bot_id=? AND tg_id=? AND panel=?",(value,self._active_bot_id,tg,panel['nonce']))
                        conn.execute('INSERT OR IGNORE INTO market_input_messages(bot_id,tg_id,message_id) VALUES(?,?,?)',(self._active_bot_id,tg,message['message_id']))
                    await self._market_draw(panel);return True
                intent=service.preview(message['from'],state['operation'],state['code'],value if state['operation']=='ipo' else state['quantity'],None if state['operation']=='ipo' else value,request_key=f'{tg}:{message["message_id"]}')
                with economy_write(self._db) as conn:
                    conn.execute('DELETE FROM market_inputs WHERE bot_id=? AND tg_id=? AND panel=?',(self._active_bot_id,tg,panel['nonce']))
                    conn.execute('INSERT OR IGNORE INTO market_input_messages(bot_id,tg_id,message_id) VALUES(?,?,?)',(self._active_bot_id,tg,message['message_id']))
                await self._market_set(panel,{'view':'confirm','intent':intent['nonce']});return True
            if not private and verb not in ('/股票','/股市帮助','/股票资讯'):
                await self.send_message(chat['id'],'📈 交易和持仓请在本人私聊查看。',self._market_link(),thread_id=self._thread_id(message),reply_to_message_id=message['message_id']);return True
            if verb in ('/认购','/买入','/卖出'):
                expected=3 if verb=='/认购' else 4
                if len(parts)!=expected or any(not re.fullmatch('[0-9]{1,9}',x) for x in parts[2:]):raise PlayError('用法：/认购 MD001 5 或 /买入 MD001 5 10、/卖出 MD001 5 12')
                intent=service.preview(message['from'],{'/认购':'ipo','/买入':'buy','/卖出':'sell'}[verb],parts[1],int(parts[2]),int(parts[3]) if expected==4 else None,request_key=f'{tg}:{message["message_id"]}')
                await self._market_new_panel(message,{'view':'confirm','intent':intent['nonce']});return True
            view={'/持仓':'hold','/委托':'orders','/成交':'history','/自选':'watch','/股市帮助':'help','/股票资讯':'news'}.get(verb,'search')
            keyword=' '.join(parts[1:]) if verb=='/股票' else ''
            c=service.company(keyword) if keyword else None
            await self._market_new_panel(message,{'view':'company','code':c['code']} if c else {'view':view,'keyword':keyword,'page':0})
        except (PlayError,GroupPointsError) as exc:
            await self.send_message(chat.get('id'),text(str(exc)),thread_id=self._thread_id(message),reply_to_message_id=message.get('message_id'))
        return True

    async def _market_callback(self,data,message,actor,callback_id):
        await self._answer_callback(callback_id)
        try:
            parts=data.split(':')
            if len(parts) not in (3,4):raise PlayError('股票按钮无效')
            panel=self._market_panel(parts[1]);tg=self._market_context(panel,message,actor,data);service=self._market_service();p=json.loads(panel['payload_json']);op=parts[2];arg=parts[3] if len(parts)==4 else None
            if op=='view':await self._market_set(panel,{'view':arg,'page':0})
            elif op=='gamehelp':await self._market_set(panel,{'view':'gamehelp','which':arg})
            elif op=='launch':
                if message.get('chat',{}).get('type') not in ('group','supergroup'): raise PlayError('请在授权群创建牌局')
                source={**message,'from':actor}
                if arg=='bw':
                    row=self._blackwhite_service().create(source)
                    await self._blackwhite_publish(row['nonce'])
                elif arg=='poker':
                    row=self._poker_service().create(source)
                    await self._poker_publish(row['nonce'])
                else:raise PlayError('游戏操作无效')
            elif op=='page':
                if not arg or not re.fullmatch('[0-9]{1,7}',arg): raise PlayError('页码无效')
                integer(int(arg),'页码',0,1000000);p['page']=int(arg);await self._market_set(panel,p)
            elif op=='company':await self._market_set(panel,{'view':'company','code':arg})
            elif op=='favorite':
                self._market_private(panel);service.actor(actor)
                if not arg or not re.fullmatch(r'MD[0-9]{3}_(on|off)',arg): raise PlayError('自选操作无效')
                code,wanted=arg.split('_')
                if not service.company(code): raise PlayError('公司不存在')
                with economy_write(self._db) as conn:
                    if wanted=='off':conn.execute('DELETE FROM market_watch WHERE user_id=? AND code=?',(panel['user_id'],code))
                    else:conn.execute('INSERT OR IGNORE INTO market_watch(user_id,code) VALUES(?,?)',(panel['user_id'],code))
                await self._market_draw(panel)
            elif op=='input':
                self._market_private(panel);service.actor(actor)
                if not arg or not re.fullmatch(r'(ipo|buy|sell)_MD[0-9]{3}',arg): raise PlayError('下单操作无效')
                operation,code=arg.split('_')
                if not service.company(code): raise PlayError('公司不存在')
                self._db.execute('INSERT INTO market_inputs(bot_id,tg_id,user_id,panel,operation,code,phase,expires_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(bot_id,tg_id) DO UPDATE SET user_id=excluded.user_id,panel=excluded.panel,operation=excluded.operation,code=excluded.code,phase=excluded.phase,quantity=0,expires_at=excluded.expires_at',
                                 (self._active_bot_id,tg,panel['user_id'],panel['nonce'],operation,code,'quantity',time.time()+600))
                await self._market_set(panel,{'view':'input'})
            elif op=='stop':
                self._market_private(panel);state=self._db.one('SELECT code FROM market_inputs WHERE bot_id=? AND tg_id=? AND panel=?',(self._active_bot_id,tg,panel['nonce']))
                if not state:raise PlayError('原输入已结束')
                self._db.execute('DELETE FROM market_inputs WHERE bot_id=? AND tg_id=? AND panel=?',(self._active_bot_id,tg,panel['nonce']))
                await self._market_set(panel,{'view':'company','code':state['code']})
            elif op=='confirm':
                self._market_private(panel)
                if not arg or not re.fullmatch('[0-9a-f]{24}',arg): raise PlayError('原确认已不可用')
                result=service.confirm(arg,actor)
                if result['operation']=='ipo':body='📈 <b>认购成功</b>\n'+result['code']+f' · {result["quantity"]} 股'
                else:
                    row=self._db.one('SELECT * FROM market_orders WHERE nonce=?',(result['order_nonce'],));filled=row['quantity']-row['remaining']
                    body='📈 <b>委托已提交</b>\n'+row['code']+' · '+('买入' if row['side']=='buy' else '卖出')
                    if filled:body+=f'\n已成交 {filled} 股'
                    if row['remaining']:body+=f'\n待成交 {row["remaining"]} 股'
                await self._market_set(panel,{'view':'done','text':body,'intent':arg})
            elif op=='dismiss':
                self._market_private(panel)
                with economy_write(self._db) as conn:
                    intent=conn.execute('SELECT * FROM market_intents WHERE nonce=?',(arg,)).fetchone()
                    if not intent or intent['bot_id']!=self._active_bot_id or intent['tg_id']!=tg or intent['user_id']!=panel['user_id']: raise PlayError('原确认已不可用')
                    if intent['state']=='done': raise PlayError('已提交的委托请在“委托”中撤单')
                    conn.execute("UPDATE market_intents SET state='expired' WHERE nonce=? AND state='preview'",(arg,))
                await self._market_set(panel,{'view':'company','code':intent['code']})
            elif op=='cancel':
                self._market_private(panel);service.cancel(arg,actor);await self._market_set(panel,{'view':'orders','page':p.get('page',0)})
            elif op=='chart':
                c=service.company(arg)
                if not c: raise PlayError('公司不存在')
                trades=self._db.query('SELECT * FROM market_trades WHERE code=? ORDER BY id DESC LIMIT 200',(c['code'],));trades.reverse()
                fields={'chat_id':panel['chat_id'],'caption':'📈 '+c['name']+' · 实际成交走势'}
                if panel['thread_id']:fields['message_thread_id']=panel['thread_id']
                result=await self._call_multipart('sendPhoto',fields,{'photo':('market.png',chart(c,trades),'image/png')})
                if not isinstance(result,dict) or not result.get('message_id'):raise PlayError('走势图未送达，请稍后重试')
            else:raise PlayError('股票操作无效')
        except (PlayError,GroupPointsError) as exc:await self._answer_callback(callback_id,str(exc))

    async def _market_tick(self):
        if self._db is None or self._points is None or self._plugins is None:return
        service=self._market_service();cfg={**DEFAULTS,**service.config()}
        codes={s.strip().upper() for s in str(cfg.get('halted_codes') or '').split(',') if s.strip()}
        with economy_write(self._db) as conn:
            for c in conn.execute('SELECT code,halted FROM market_companies').fetchall():
                wanted=int(c['code'] in codes)
                if c['halted']!=wanted:conn.execute('UPDATE market_companies SET halted=? WHERE code=?',(wanted,c['code']))
        service.maintain()
        if service.enabled() and cfg.get('news_enabled',True):news_tick(self._db)
