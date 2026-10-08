"""Only current spendable ledger points, effective members and TG public display names."""
from __future__ import annotations

import time
import unicodedata
from html import escape

from app.modules.plugins import Field, Plugin, Spec


class PointsRankingService:
    def __init__(self, members, points):
        self.members,self.points=members,points

    def rows(self,*,now=None):
        clock=time.time() if now is None else float(now)
        db=self.points._db
        with db._lock:
            own=not db._conn.in_transaction
            if own:db._conn.execute('BEGIN')
            try:return self._snapshot(clock)
            finally:
                if own:db._conn.rollback()  # close our read-only snapshot, never another owner's transaction

    def _snapshot(self,clock):
        balances=self.points.balances()
        out=[]
        for member in self.members.list(limit=None):
            tg=str(member.get('tg_user_id') or '')
            expiry=member.get('expires_at_effective')
            if (not tg.isascii() or not tg.isdecimal() or int(tg)<=0 or member.get('state')!='active'
                    or member.get('emby_missing_since') or expiry is not None and expiry<=clock):continue
            uid=member['emby_user_id']
            raw=str(member.get('tg_display_name') or '')
            label=' '.join(''.join(c for c in raw if unicodedata.category(c) not in ('Cc','Cf')).split()) or '成员'
            out.append({'_uid':uid,'name':label,
                        'points':balances.get(uid,0)})
        return sorted(out,key=lambda r:(-r['points'],r['_uid']))


class PointsRankingPlugin(Plugin):
    spec=Spec(id='points_ranking',name='积分排行榜',icon='🏆',category='points',
              description='仅有效绑定成员，按当前可用积分排序。',
              fields=[Field('page_size','每页人数',kind='int',default=10,min=5,max=20)])

    def status(self):
        return {'有效绑定成员':len(PointsRankingService(self.ctx.members,self.ctx.points).rows())}

    readonly_status = status

    async def run(self,config):return self.status()


class PointsRankingBotMixin:
    def _ranking_view(self,panel,p):
        from app.modules.play_money import PlayError
        if not self._plugin_on('points_ranking'):raise PlayError('积分榜暂未开放')
        page_size=self._plugins.config('points_ranking').get('page_size',10)
        rows=PointsRankingService(self._members,self._points).rows()
        pages=max(1,(len(rows)+page_size-1)//page_size)
        page=max(0,min(int(p.get('page',0)),pages-1))
        body=f'🏆 <b>积分榜</b> · 当前可用\n{page+1}/{pages}页'
        if not rows:body+='\n\n暂无有效绑定成员'
        for index,row in enumerate(rows[page*page_size:(page+1)*page_size],page*page_size+1):
            badge=('🥇','🥈','🥉')[index-1] if index<=3 else str(index)+'.'
            label=row['name'][:32]+('…' if len(row['name'])>32 else '')
            body+=f'\n{badge} {escape(label)} · <b>{row["points"]}</b>'
        nav=[];token=panel['nonce']
        if page:nav.append({'text':'‹ 上一页','callback_data':f'st:{token}:page:{page-1}'})
        if page+1<pages:nav.append({'text':'下一页 ›','callback_data':f'st:{token}:page:{page+1}'})
        keyboard=[nav] if nav else []
        keyboard.append([{'text':'刷新','callback_data':f'st:{token}:page:{page}'}])
        return body,keyboard

    async def _ranking_command(self,message):
        import re

        from app.modules.play_money import PlayError
        parts=str(message.get('text') or '').strip().split();first=parts[0].lower() if parts else ''
        if first.split('@',1)[0] not in ('/积分榜','/pointsrank'):return False
        self._check_bot_identity()
        if '@' in first and (not self._bot_username or first.split('@',1)[1]!=self._bot_username.lower()):return True
        chat=message.get('chat') or {}
        if chat.get('type') not in ('private','group','supergroup') or chat.get('type')!='private' and not self._group_chat_allowed(chat):return True
        if not self._play_entry_valid(message):return True
        try:
            if len(parts)>2 or len(parts)==2 and not re.fullmatch('[0-9]{1,7}',parts[1]):raise PlayError('用法：/积分榜 或 /积分榜 2')
            page=int(parts[1])-1 if len(parts)==2 else 0
            if page<0:raise PlayError('页码从1开始')
            if not self._plugin_on('points_ranking'):raise PlayError('积分榜暂未开放')
            await self._market_new_panel(message,{'view':'leaderboard','page':page})
        except ValueError as exc:
            await self.send_message(chat.get('id'),escape(str(exc)),thread_id=self._thread_id(message),reply_to_message_id=message.get('message_id'))
        return True
