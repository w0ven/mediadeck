"""Only scheduled deliveries to authorized interaction group IDs; no notification channel reuse."""
from __future__ import annotations

import json
import time

from app.modules.group_points import GroupPointsError, reliable_user
from app.modules.market_digest import DigestService, in_window, times
from app.modules.play_money import PlayError
from app.modules.report_delivery import CALL_DELIVERY
from app.modules.settings import parse_group_interaction_chats


class MarketDigestBotMixin:
    async def _market_digest_tick(self, cfg, *, now=None):
        clock = time.time() if now is None else float(now)
        self._check_bot_identity()
        groups = [str(x) for x in parse_group_interaction_chats(self._cfg().get('group_interaction_chats'))
                  if str(x).startswith('-') and str(x)[1:].isdecimal()]
        enabled = bool(self.enabled and self._plugin_on('stock_market') and cfg['digest_enabled'])
        service = DigestService(self._db,self._active_bot_id)
        startup = not getattr(self,'_market_digest_booted',False)
        service.sync(groups,times(cfg['digest_times']),enabled,startup=startup,now=clock)
        self._market_digest_booted=True
        if not enabled:return
        for _ in range(20):
            clock = time.time() if now is None else float(now)
            job=service.claim(groups,now=clock)
            if not job:break
            # Revalidate active config and group authorization immediately before transport.
            current=self._plugins.config('stock_market')
            clock = time.time() if now is None else float(now)
            if (not in_window(clock) or clock>=job['expires_at'] or not self.enabled
                    or not self._plugin_on('stock_market') or not current.get('digest_enabled')
                    or current.get('digest_times')!=cfg['digest_times']
                    or not self._group_chat_allowed({'id':int(job['chat_id']),'type':'supergroup'})):
                service.finish(job,None,{'state':'failed','reason':'发送前时段、开关或群授权已变化'},now=clock);continue
            CALL_DELIVERY.set(None)
            result=await self._call('sendMessage',dict(json.loads(job['payload_json']),chat_id=int(job['chat_id'])),timeout=min(20,max(1,job['expires_at']-clock)))
            service.finish(job,result,CALL_DELIVERY.get() or {},now=time.time() if now is None else clock)

    async def _market_digest_callback(self,data,message,actor,callback_id):
        await self._answer_callback(callback_id)
        try:
            reliable_user({'from':actor})
            self._check_bot_identity()
            chat=message.get('chat') or {};sender=message.get('from') or {}
            actions=[b.get('callback_data') for line in (message.get('reply_markup') or {}).get('inline_keyboard',[]) for b in line]
            if (data not in ('std:home','std:book','std:help') or data not in actions
                    or chat.get('type') not in ('group','supergroup') or not self._group_chat_allowed(chat)
                    or sender.get('is_bot') is not True or str(sender.get('id'))!=self._active_bot_id
                    or type(message.get('message_id')) is not int
                    or any(message.get(k) for k in ('sender_chat','is_automatic_forward','forward_origin','forward_date'))):
                raise PlayError('请使用原Bot授权群资讯卡')
            job=self._db.one("SELECT thread_id FROM market_digest_jobs WHERE bot_id=? AND chat_id=? AND message_id=? AND state='sent'", (self._active_bot_id,str(chat.get('id')),message['message_id']))
            if not job or job['thread_id']!=(self._thread_id(message) or 0):raise PlayError('请使用原群原话题资讯卡')
            await self._market_new_panel(dict(message,**{'from':actor}),{'view':data.split(':',1)[1],'page':0})
        except (PlayError,GroupPointsError) as exc:
            await self._answer_callback(callback_id,str(exc))
