"""Original public game/help and points-board panels, without any trading service."""
from __future__ import annotations

import json
import re
import secrets
import time
from html import escape

from app.modules.economy_rules import encode
from app.modules.group_points import GroupPointsError, reliable_user
from app.modules.play_money import PlayError
from app.modules.play_rounds import PlayAccess


def migrate(db):
    db._conn.execute('''CREATE TABLE IF NOT EXISTS play_panels (
        nonce TEXT PRIMARY KEY, bot_id TEXT NOT NULL, user_id TEXT NOT NULL, tg_id TEXT NOT NULL,
        chat_id TEXT NOT NULL, thread_id INTEGER NOT NULL DEFAULT 0, message_id INTEGER,
        payload_json TEXT NOT NULL, created_at REAL NOT NULL)''')


class PlayPanelsBotMixin:
    def _play_panel(self,token):return self._db.one('SELECT * FROM play_panels WHERE nonce=?',(token,))

    def _play_access(self):
        if self._db is None or self._points is None:raise PlayError('积分服务暂不可用')
        self._check_bot_identity()
        return PlayAccess(self._db,self._members,self._group_chat_allowed,self._active_bot_id)

    def _play_panel_context(self,panel,message,actor,data):
        service=self._play_access();tg=reliable_user({'from':actor})
        member=self._members.find_by_telegram(tg)
        chat,sender=message.get('chat') or {},message.get('from') or {}
        buttons=[b.get('callback_data') for line in (message.get('reply_markup') or {}).get('inline_keyboard',[]) for b in line]
        if (not panel or panel['bot_id']!=self._active_bot_id or str(chat.get('id'))!=panel['chat_id']
                or type(message.get('message_id')) is not int or message['message_id']!=panel['message_id'] or panel['message_id'] is None
                or sender.get('is_bot') is not True or str(sender.get('id'))!=self._active_bot_id or data not in buttons
                or any(message.get(k) for k in ('forward_origin','forward_date','sender_chat','is_automatic_forward'))):
            raise PlayError('请使用原Bot玩法卡片')
        if chat.get('type')=='private':
            if str(chat.get('id'))!=tg or not member or panel['tg_id']!=tg or panel['user_id']!=member['emby_user_id']:raise PlayError('仅本人私聊可操作')
        else:
            group,thread=service.group(message)
            if group!=panel['chat_id'] or thread!=panel['thread_id']:raise PlayError('请使用原群原话题玩法卡')
            service.actor(actor)
        return tg

    async def _play_new_panel(self,message,payload):
        service=self._play_access();tg,member=service.actor(message.get('from') or {})
        chat=message.get('chat') or {}
        if chat.get('type')=='private':
            if str(chat.get('id'))!=tg:raise PlayError('仅本人私聊可操作')
            thread=0
        else:_,thread=service.group(message)
        nonce=secrets.token_hex(12)
        self._db.execute('INSERT INTO play_panels(nonce,bot_id,user_id,tg_id,chat_id,thread_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)',(nonce,self._active_bot_id,member['emby_user_id'],tg,str(chat['id']),thread,encode(payload),time.time()))
        await self._play_panel_draw(self._play_panel(nonce),reply_to=message.get('message_id'))
        return self._play_panel(nonce)

    async def _play_panel_draw(self,panel,*,reply_to=None):
        payload=json.loads(panel['payload_json'])
        if payload['view']=='leaderboard':body,keys=self._ranking_view(panel,payload)
        else:body,keys=self._play_hub_view(panel,payload)
        fields={'chat_id':panel['chat_id'],'text':body,'parse_mode':'HTML','reply_markup':{'inline_keyboard':keys},'disable_web_page_preview':True}
        if panel['thread_id']:fields['message_thread_id']=panel['thread_id']
        initial=panel['message_id'] is None
        if initial:
            if reply_to:fields['reply_parameters']={'message_id':reply_to}
        else:fields['message_id']=panel['message_id']
        response=await self._call('sendMessage' if initial else 'editMessageText',fields)
        if initial and isinstance(response,dict) and type(response.get('message_id')) is int:
            self._db.execute('UPDATE play_panels SET message_id=? WHERE nonce=? AND message_id IS NULL',(response['message_id'],panel['nonce']))

    async def _play_panel_set(self,panel,payload):
        if payload.get('view') not in ('playhub','gamehelp','leaderboard'):raise PlayError('玩法页面无效')
        self._db.execute('UPDATE play_panels SET payload_json=? WHERE nonce=?',(encode(payload),panel['nonce']))
        await self._play_panel_draw(self._play_panel(panel['nonce']))

    async def _play_panel_callback(self,data,message,actor,callback_id):
        await self._answer_callback(callback_id)
        try:
            parts=data.split(':')
            if len(parts)!=4 or parts[0]!='pp':raise PlayError('玩法按钮无效')
            panel=self._play_panel(parts[1]);self._play_panel_context(panel,message,actor,data)
            op,arg=parts[2:];payload=json.loads(panel['payload_json'])
            if op=='view':await self._play_panel_set(panel,{'view':arg,'page':0})
            elif op=='gamehelp':await self._play_panel_set(panel,{'view':'gamehelp','which':arg})
            elif op=='page':
                if payload['view']!='leaderboard' or not re.fullmatch('[0-9]{1,7}',arg):raise PlayError('页码无效')
                await self._play_panel_set(panel,dict(payload,page=int(arg)))
            elif op=='launch':
                if message.get('chat',{}).get('type') not in ('group','supergroup'):raise PlayError('请在授权群创建牌局')
                source=dict(message,**{'from':actor})
                if arg=='bw':
                    row=self._blackwhite_service().create(source);await self._blackwhite_publish(row['nonce'])
                elif arg=='niuniu':
                    row=self._niuniu_service().create(source);await self._niuniu_publish(row['nonce'])
                else:raise PlayError('游戏操作无效')
            else:raise PlayError('玩法操作无效')
        except (PlayError,GroupPointsError) as exc:
            await self._answer_callback(callback_id,escape(str(exc)))
