"""Five-card public reveal on the original group card, without betting controls."""
from __future__ import annotations

import asyncio
import json
import re
import time
from html import escape

from app.modules.economy_rules import economy_write
from app.modules.group_points import GroupPointsError, reliable_user
from app.modules.niuniu import CATEGORIES, DEFAULTS, NiuniuService, card_text, strength
from app.modules.play_money import PlayError
from app.modules.poker_image import render
from app.modules.report_delivery import CALL_DELIVERY


def help_text(cfg):
    return ('🐂 <b>牛牛 · 五张定胜负</b>\n\n'
            '2～5人，同额投入，满5人自动开始；2人起房主可点开始。加入即冻结，开始前退出退回。\n'
            '每人5张牌，JQK计10。任意3张凑10的倍数，余2张定牛几；牛牛最高，无牛最低，不设特殊牌型。\n'
            '同级比最大牌面（A低、K高），再比花色：♠＞♥＞♣＞♦。赢家收全池，无倍率、无抽成。\n\n'
            f'默认每人 {cfg["default_stake"]} 积分；<code>/牛牛 100</code> 为每人100积分。\n'
            '一次公开手牌与结果，不跟注、不加注、不自动续局。等人超时或牌局无法安全恢复，全额退回。')


class NiuniuBotMixin:
    def _niuniu_service(self):
        if self._db is None or self._points is None:raise PlayError('积分服务暂不可用')
        self._check_bot_identity()
        return NiuniuService(self._db,self._members,self._points,lambda:self._plugins.config('niuniu') if self._plugins else {},lambda:self._plugin_on('niuniu'),self._group_chat_allowed,self._active_bot_id)

    def _niuniu_view(self,row,service):
        players=row.get('_players') if '_players' in row else service.players(row['nonce'])
        n=row['nonce'];keys=[]
        body=f'🐂 <b>牛牛 · 五张定胜负</b>\n每人投入 <b>{row["stake"]}</b> 积分'
        if row['state']=='lobby':
            body+=f'\n\n🪑 已就座 <b>{len(players)}/5</b> · 满员即揭晓'
            body+=''.join('\n'+escape(p['display_name'][:40]) for p in players)
            body+='\n\n<i>2人起房主可开始，开始前退出退回。</i>'
            keys=[[{'text':f'🐂 入座 · {row["stake"]}分','callback_data':f'nn:{n}:join'},{'text':'🎴 开始','callback_data':f'nn:{n}:start'}],[{'text':'退回离座','callback_data':f'nn:{n}:leave'}]]
        elif row['state']=='settled':
            body+=f'\n\n🎉 本局揭晓 · 总池 <b>{json.loads(row["result_json"])["pot"]}</b> 积分'
            for p in players:
                cards=json.loads(p['cards_json'])
                body+='\n\n'+escape(p['display_name'][:40])+f' · <b>{CATEGORIES[strength(cards)[0]]}</b>'
                body+='\n'+'  '.join(card_text(c) for c in cards)
                if p['result_amount']:body+=f'\n🏆 获得 <b>{p["result_amount"]}</b> 积分'
        else:
            body+='\n\n🍃 本局已结束，投入已退回'
            body+=''.join('\n'+escape(p['display_name'][:40])+f' · 退回 {p["result_amount"]} 积分' for p in players)
        keys.append([{'text':'怎么比大小？','callback_data':f'nnh:{n}'}])
        return body,keys

    async def _niuniu_command(self,message):
        parts=str(message.get('text') or '').strip().split();first=parts[0].lower() if parts else ''
        if first.split('@',1)[0] not in ('/牛牛','/niuniu','/牛牛帮助'):return False
        self._check_bot_identity()
        if '@' in first and (not self._bot_username or first.split('@',1)[1]!=self._bot_username.lower()):return True
        chat=message.get('chat') or {}
        try:
            reliable_user(message)
            if first.split('@',1)[0]=='/牛牛帮助':
                if not self._play_entry_valid(message):return True
                if chat.get('type')!='private' and not self._group_chat_allowed(chat):return True
                await self.send_message(chat.get('id'),help_text({**DEFAULTS,**self._niuniu_service().config()}),thread_id=self._thread_id(message))
                return True
            if chat.get('type') not in ('group','supergroup') or not self._group_chat_allowed(chat):return True
            if len(parts)>2 or len(parts)==2 and not re.fullmatch('[0-9]{1,6}',parts[1]):raise PlayError('用法：/牛牛 或 /牛牛 100（每人投入）')
            row=self._niuniu_service().create(message,int(parts[1]) if len(parts)==2 else None)
            await self._niuniu_publish(row['nonce'])
        except (PlayError,GroupPointsError) as exc:
            await self.send_message(chat.get('id'),'🍃 '+escape(str(exc)),thread_id=self._thread_id(message),reply_to_message_id=message.get('message_id'))
        return True

    async def _niuniu_callback(self,data,message,actor,callback_id):
        await self._answer_callback(callback_id)
        try:
            service=self._niuniu_service();tokens=data.split(':')
            if tokens[0]=='nnh' and len(tokens)==2:
                with economy_write(self._db) as conn:
                    row=service.get(tokens[1])
                    if not row:raise PlayError('这局牛牛不存在')
                    service.context(conn,row,message,data)
                tg=reliable_user({'from':actor})
                sent=await self.send_message(tg,help_text(json.loads(row['config_json'])))
                await self._answer_callback(callback_id,'玩法已发私聊' if sent else '请先打开Bot，再点玩法')
                return
            if len(tokens)!=3 or tokens[0]!='nn':raise PlayError('牛牛按钮无效')
            row=service.lobby(tokens[1],actor,message,tokens[2])
            await self._niuniu_publish(row['nonce'])
            await self._niuniu_photos()
        except (PlayError,GroupPointsError) as exc:
            await self._answer_callback(callback_id,'🍃 '+str(exc))

    async def _niuniu_publish(self,nonce):
        if not self.enabled:return
        if not hasattr(self,'_niuniu_publish_locks'):self._niuniu_publish_locks={}
        async with self._niuniu_publish_locks.setdefault(nonce,asyncio.Lock()):
            service=self._niuniu_service();row=service.begin_publish(nonce)
            if not row:return
            text,keys=self._niuniu_view(row,service)
            payload={'chat_id':int(row['chat_id']),'text':text,'parse_mode':'HTML','reply_markup':{'inline_keyboard':keys}}
            initial=row['card_message_id'] is None
            if initial:
                payload['reply_parameters']={'message_id':row['command_message_id'],'allow_sending_without_reply':True}
                if row['thread_id']:payload['message_thread_id']=row['thread_id']
            else:payload['message_id']=row['card_message_id']
            CALL_DELIVERY.set(None)
            result=await self._call('sendMessage' if initial else 'editMessageText',payload)
            failure=CALL_DELIVERY.get() or {}
            if isinstance(result,dict) and type(result.get('message_id')) is int:service.published(row,result['message_id'])
            elif not initial and (result is True or failure.get('not_modified')):service.published(row,row['card_message_id'])
            else:service.publish_failed(row,failure)

    async def _niuniu_photos(self):
        if not self.enabled:return
        service=self._niuniu_service();clock=time.time()
        with economy_write(self._db) as conn:
            conn.execute("UPDATE niuniu_rounds SET photo_state='unknown',photo_error='牌图发送确认中断，未重复发送' WHERE photo_state='sending' AND photo_lease<=?",(clock,))
            rows=self._db.query("SELECT * FROM niuniu_rounds WHERE bot_id=? AND state='settled' AND photo_state IN ('pending','retry') AND photo_due<=? LIMIT 10",(self._active_bot_id,clock))
            for row in rows:
                conn.execute("UPDATE niuniu_rounds SET photo_state='sending',photo_lease=?,photo_attempts=photo_attempts+1 WHERE nonce=?",(clock+90,row['nonce']))
        for row in rows:
            players=service.players(row['nonce'])
            image=render([{'name':p['display_name'],'cards':json.loads(p['cards_json']),'award':p['result_amount']} for p in players],niuniu=True)
            fields={'chat_id':int(row['chat_id']),'caption':'🐂 牛牛 · 五张揭晓'}
            if row['thread_id']:fields['message_thread_id']=row['thread_id']
            CALL_DELIVERY.set(None)
            result=await self._call_multipart('sendPhoto',fields,{'photo':('niuniu.png',image,'image/png')})
            failure=CALL_DELIVERY.get() or {};mid=result.get('message_id') if isinstance(result,dict) else None
            state='sent' if type(mid) is int and mid>0 else 'retry' if failure.get('state')=='retry' and row['photo_attempts']<2 else 'failed' if failure.get('state') in ('retry','failed') else 'unknown'
            self._db.execute("UPDATE niuniu_rounds SET photo_state=?,photo_message_id=?,photo_error=?,photo_due=? WHERE nonce=? AND photo_state='sending' AND photo_lease=?",(state,mid if state=='sent' else None,'' if state=='sent' else '牌图未确认；文字手牌与结果保留',time.time()+max(10,int(failure.get('retry_after') or 0)),row['nonce'],clock+90))

    async def _niuniu_tick(self):
        if self._db is None or self._points is None or self._plugins is None:return
        service=self._niuniu_service();service.expire_due()
        if self.enabled:
            for row in service.pending_cards('niuniu'):await self._niuniu_publish(row['nonce'])
            await self._niuniu_photos()
