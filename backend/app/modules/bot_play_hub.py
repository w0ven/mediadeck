"""Short discovery menu; complete rules only when explicitly requested."""
from __future__ import annotations

from html import escape

from app.modules.blackwhite import DEFAULTS as BW_DEFAULTS
from app.modules.bot_poker import help_text as poker_help
from app.modules.market_views import market_help
from app.modules.play_money import PlayError
from app.modules.poker import DEFAULTS as POKER_DEFAULTS


class PlayHubBotMixin:
    def _play_hub_view(self,panel,p):
        token=panel['nonce'];private=panel['chat_id']==panel['tg_id']
        def button(label,op,arg):return {'text':label,'callback_data':f'st:{token}:{op}:{arg}'}
        rows=[]
        if p['view']=='playhub':
            body='🎲 <b>游戏与市场</b>'
            if self._plugin_on('blackwhite'):
                cfg={**BW_DEFAULTS,**self._plugins.config('blackwhite')}
                body+=f'\n黑白板 · 5人 · 默认 {cfg["default_stake"]} 积分/人'
                choices=[button('黑白板玩法','gamehelp','bw')]
                if not private:choices.insert(0,button('创建黑白板','launch','bw'))
                rows.append(choices)
            if self._plugin_on('poker'):
                cfg={**POKER_DEFAULTS,**self._plugins.config('poker')}
                body+=f'\n炸金花 · 2～5人 · 底注 {cfg["default_ante"]}'
                choices=[button('炸金花玩法','gamehelp','poker')]
                if not private:choices.insert(0,button('创建炸金花','launch','poker'))
                rows.append(choices)
            if self._plugin_on('stock_market'):rows.append([button('模拟股票','view','search'),button('股票玩法','gamehelp','stock')])
            if self._plugin_on('points_ranking'):rows.append([button('积分榜','view','leaderboard')])
            if private and (self._plugin_on('poker') or self._plugin_on('blackwhite')):body+='\n\n到授权群用 /黑白板 或 /炸金花 创建牌局。'
            if not rows:body+='\n\n暂未开放玩法'
        else:
            which=p['which']
            if which=='bw':
                cfg={**BW_DEFAULTS,**self._plugins.config('blackwhite')}
                body=('⚫⚪ <b>黑白板 · 玩法</b>\n\n5人同额押注，秘密选黑或白，选后不可更改。满5人自动开奖，少数方平分全部奖池，整数余数随机公平分配；同面全退，无抽成。\n'
                      f'当前可押 {cfg["min_stake"]}～{cfg["max_stake"]} 积分/人，默认 {cfg["default_stake"]}；{cfg["lobby_seconds"]} 秒未满5人全退。有效绑定群成员可参与，管理员同样扣本人积分。\n'
                      '开奖公开展示名、选择与本局所得，不公开余额。已创建的局按原参数执行。\n\n<code>/黑白板 50</code> 在授权群创建，然后点手心白板或手背黑板。')
            elif which=='poker':body=poker_help({**POKER_DEFAULTS,**self._plugins.config('poker')})
            elif which=='stock':body=market_help(self._plugins.config('stock_market'))
            else:raise PlayError('玩法页面不存在')
            rows.append([button('返回玩法','view','playhub')])
        return body,rows

    @staticmethod
    def _play_entry_valid(message):
        from app.modules.group_points import GroupPointsError, reliable_user
        try: tg=reliable_user(message)
        except GroupPointsError:return False
        chat=message.get('chat') or {}
        return (type(message.get('message_id')) is int and message['message_id']>0
                and not any(message.get(k) for k in ('forward_origin','forward_date','sender_chat','is_automatic_forward'))
                and (chat.get('type')!='private' or str(chat.get('id'))==tg))

    async def _play_hub_command(self,message):
        first=str(message.get('text') or '').strip().split()
        verb=first[0].lower() if first else ''
        if verb.split('@',1)[0] not in ('/游戏','/games','/玩法'):return False
        self._check_bot_identity()
        if '@' in verb and (not self._bot_username or verb.split('@',1)[1]!=self._bot_username.lower()):return True
        chat=message.get('chat') or {}
        if chat.get('type') not in ('private','group','supergroup') or chat.get('type')!='private' and not self._group_chat_allowed(chat):return True
        if not self._play_entry_valid(message):return True
        try:
            if len(first)!=1:raise PlayError('用法：/游戏')
            await self._market_new_panel(message,{'view':'playhub'})
        except ValueError as exc:
            await self.send_message(chat.get('id'),escape(str(exc)),thread_id=self._thread_id(message),reply_to_message_id=message.get('message_id'))
        return True

    async def _play_menu_open(self,data,chat_id,tg_id,tg_name,message):
        actor={'id':int(tg_id),'is_bot':False,'first_name':tg_name}
        source={**message,'from':actor}
        if source.get('chat') is None:raise PlayError('原菜单归属不明')
        view={'play_hub':'playhub','market_home':'search','points_board':'leaderboard'}[data]
        await self._market_new_panel(source,{'view':view,'page':0})

    def _play_remember(self,message,actor):
        from app.modules.group_points import GroupPointsError, reliable_user
        try:tg=reliable_user({'from':actor})
        except GroupPointsError:return
        chat=message.get('chat') or {}
        allowed=(chat.get('type')=='private' and str(chat.get('id'))==tg or chat.get('type') in ('group','supergroup') and self._group_chat_allowed(chat))
        if allowed and not any(message.get(k) for k in ('forward_origin','forward_date','sender_chat','is_automatic_forward')):
            self._remember_tg_user(actor)  # reuse the existing authoritative public-name column, no inferred names
