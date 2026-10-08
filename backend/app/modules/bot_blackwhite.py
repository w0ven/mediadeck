"""Short original group card; choices remain private until the full-table draw."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from html import escape

from app.modules.blackwhite import BlackwhiteService, results
from app.modules.economy_rules import BEIJING, economy_write
from app.modules.group_points import GroupPointsError, reliable_user
from app.modules.play_money import PlayError
from app.modules.red_packets import public_name
from app.modules.report_delivery import CALL_DELIVERY


class BlackwhiteBotMixin:
    def _blackwhite_service(self):
        if self._db is None or self._points is None:
            raise PlayError('积分服务暂不可用')
        self._check_bot_identity()
        return BlackwhiteService(self._db, self._members, self._points,
                                 lambda: self._plugins.config('blackwhite') if self._plugins else {},
                                 lambda: self._plugin_on('blackwhite'), self._group_chat_allowed,
                                 self._active_bot_id)

    def _blackwhite_view(self, row, service):
        players = row.get('_players') if '_players' in row else service.players(row['nonce'])
        def name(value):
            return escape(public_name({'first_name': value})[:40])
        text = f"⚫⚪ <b>黑白板</b>\n{name(row['actor_name'])} · {row['stake']} 积分/人"
        nonce = row['nonce']
        keyboard = []
        if row['state'] == 'lobby':
            text += f"\n\n已选择 <b>{len(players)}/5</b>"
            for p in players:
                text += '\n' + name(p['display_name'])
            text += '\n\n<i>截止 ' + datetime.fromtimestamp(row['expires_at'], BEIJING).strftime('%H:%M') + '</i>'
            keyboard.append([{'text': '手心 · 白板', 'callback_data': f'bw:{nonce}:white'},
                             {'text': '手背 · 黑板', 'callback_data': f'bw:{nonce}:black'}])
        else:
            mode = results(row)['mode']
            if mode in ('timeout', 'disabled'):
                text += '\n\n' + ('已过期' if mode == 'timeout' else '已取消') + ' · 全额退回'
                for p in players:
                    text += f"\n{name(p['display_name'])} · 已退 {p['result_amount']} 积分"
            else:
                text += '\n\n' + ('同面 · 全额退回' if mode == 'same' else ('黑板胜' if mode == 'black' else '白板胜'))
                for p in players:
                    color = '黑板' if p['choice'] == 'black' else '白板'
                    amount = f"{'已退 ' if mode == 'same' else ''}{p['result_amount']} 积分" if p['result_amount'] else '未中奖'
                    text += f"\n{name(p['display_name'])} · {color} · {amount}"
        keyboard.append([{'text': '玩法', 'callback_data': 'bwh:' + nonce}])
        return text, keyboard

    async def _blackwhite_command(self, message):
        parts = str(message.get('text') or '').strip().split()
        first = parts[0].lower() if parts else ''
        if first.split('@', 1)[0] not in ('/黑白板', '/blackwhite'):
            return False
        self._check_bot_identity()
        if '@' in first and (not self._bot_username or first.split('@', 1)[1] != self._bot_username.lower()):
            return True
        chat = message.get('chat') or {}
        if chat.get('type') not in ('group', 'supergroup') or not self._group_chat_allowed(chat):
            return True
        try:
            if len(parts) > 2 or (len(parts) == 2 and not re.fullmatch(r'[0-9]{1,6}', parts[1])):
                raise PlayError('用法：/黑白板 50')
            stake = int(parts[1]) if len(parts) == 2 else None
            row = self._blackwhite_service().create(message, stake)
        except (PlayError, ValueError) as exc:
            await self.send_message(chat['id'], escape(str(exc)), thread_id=self._thread_id(message),
                                    reply_to_message_id=message.get('message_id'))
            return True
        await self._blackwhite_publish(row['nonce'])
        return True

    async def _blackwhite_callback(self, data, message, actor, callback_id):
        await self._answer_callback(callback_id)
        try:
            reliable_user({'from': actor})
            parts = data.split(':')
            service = self._blackwhite_service()
            if data.startswith('bwh:'):
                if len(parts) != 2:
                    raise PlayError('游戏按钮无效')
                with economy_write(self._db) as conn:
                    row = service.get(parts[1])
                    if not row or row['kind'] != 'blackwhite':
                        raise PlayError('游戏不存在')
                    service.context(conn, row, message, data)
                ttl = json.loads(row['config_json'])['lobby_seconds']
                timer = '10分钟' if ttl == 600 else f'{ttl}秒'
                help_text = f'5人各押{row["stake"]}积分，秘密选黑或白，选后不可改。满5人少数方平分总池，余数随机公平分配；同面全退。{timer}未满全退。管理员同样扣本人积分。'
                await self._answer_callback(callback_id, help_text)
                return
            if len(parts) != 3:
                raise PlayError('游戏按钮无效')
            row = service.join(parts[1], actor, message, parts[2])['row']
            await self._answer_callback(callback_id, '已选择' + ('白板' if parts[2] == 'white' else '黑板'))
            await self._blackwhite_publish(row['nonce'])
        except (PlayError, GroupPointsError) as exc:
            await self._answer_callback(callback_id, str(exc))

    async def _blackwhite_publish(self, nonce):
        from app.modules.telegram import _CALL_ERROR
        if not hasattr(self, '_play_locks'):
            self._play_locks = {}
        async with self._play_locks.setdefault(nonce, asyncio.Lock()):
            service = self._blackwhite_service()
            row = service.begin_publish(nonce)
            if row is None:
                return
            text, keyboard = self._blackwhite_view(row, service)
            body = {'chat_id': row['chat_id'], 'text': text, 'parse_mode': 'HTML',
                    'reply_markup': {'inline_keyboard': keyboard}, 'disable_web_page_preview': True}
            if row['thread_id']:
                body['message_thread_id'] = row['thread_id']
            initial = row['card_message_id'] is None
            if initial:
                body['reply_parameters'] = {'message_id': row['command_message_id']}
            else:
                body['message_id'] = row['card_message_id']
            _CALL_ERROR.set(None)
            CALL_DELIVERY.set(None)
            response = await self._call('sendMessage' if initial else 'editMessageText', body)
            failure = CALL_DELIVERY.get() or {}
            error = _CALL_ERROR.get()
            error = str(self._last_error if error is None else error).lower()
            if isinstance(response, dict) and type(response.get('message_id')) is int:
                service.published(row, response['message_id'])
            elif not initial and (response is True or failure.get('not_modified') or 'not modified' in error):
                service.published(row, row['card_message_id'])
            else:
                service.publish_failed(row, failure)

    async def _blackwhite_tick(self):
        if self._db is None or self._points is None or self._plugins is None:
            return  # startup registry has not been bound yet; do not cancel existing money
        service = self._blackwhite_service()
        service.expire_due()  # refunds are independent of Bot network/enabled state
        if self.enabled:
            for row in service.pending_cards('blackwhite'):
                await self._blackwhite_publish(row['nonce'])
