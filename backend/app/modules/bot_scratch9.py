"""One shared image message and owner-bound private fee confirmation. No group award spam."""
from __future__ import annotations

import asyncio
import json
import secrets
from html import escape

from app.modules.group_points import GroupPointsError, reliable_user
from app.modules.play_money import PlayError
from app.modules.report_delivery import CALL_DELIVERY
from app.modules.scratch9 import Scratch9Service
from app.modules.scratch9_view import render, view


class Scratch9BotMixin:
    def _scratch9_service(self):
        if self._db is None or self._points is None or self._plugins is None:
            raise PlayError('积分服务暂不可用')
        self._check_bot_identity()
        return Scratch9Service(self._db, self._members, self._points,
                               lambda: self._plugins.config('scratch9'),
                               lambda: self._plugin_on('scratch9'), self._group_chat_allowed, self._active_bot_id)

    async def _scratch9_command(self, message):
        parts = str(message.get('text') or '').strip().split()
        first = parts[0].lower() if parts else ''
        if first.split('@', 1)[0] not in ('/刮刮乐', '/scratch9'):
            return False
        self._check_bot_identity()
        if '@' in first and (not self._bot_username or first.split('@', 1)[1] != self._bot_username.lower()):
            return True
        chat = message.get('chat') or {}
        if chat.get('type') not in ('group', 'supergroup') or not self._group_chat_allowed(chat):
            return True
        try:
            reliable_user(message)
            if len(parts) != 1:
                raise PlayError('用法：/刮刮乐（由绑定管理员开场）')
            row = self._scratch9_service().create(message)
            await self._scratch9_publish(row['nonce'])
            if row['command_message_id'] != message.get('message_id'):
                text = '🎟 本群已有一场刮刮乐，请使用当前原卡。'
                if row['card_message_id'] is not None:
                    username = row['chat_username']
                    group = row['chat_id']
                    url = f'https://t.me/{username}/{row["card_message_id"]}' if username else f'https://t.me/c/{group[4:]}/{row["card_message_id"]}' if group.startswith('-100') else ''
                    if url:
                        text = '🎟 本群已有 <a href="'+escape(url, quote=True)+'">当前刮刮乐</a>，不重复开场。'
                await self.send_message(chat['id'], text, thread_id=self._thread_id(message), reply_to_message_id=message.get('message_id'))
        except (PlayError, GroupPointsError) as exc:
            await self.send_message(chat.get('id'), '🍃 '+escape(str(exc)), thread_id=self._thread_id(message), reply_to_message_id=message.get('message_id'))
        return True

    async def _scratch9_callback(self, data, message, actor, callback_id):
        await self._answer_callback(callback_id)
        try:
            service = self._scratch9_service()
            parts = data.split(':')
            if len(parts) != 3:
                raise PlayError('刮刮乐按钮无效')
            if parts[0] == 'gg':
                if not parts[2].isdigit():
                    raise PlayError('格子无效')
                intent = service.select(parts[1], int(parts[2]), actor, message, callback_id)
                pending = service.begin_confirmation(intent['token'])
                if pending is None:
                    await self._answer_callback(callback_id, '确认已发本人私聊；取消或超时都不扣积分' if intent['state'] == 'pending' else '确认未送达或已失效，不会扣积分，请重新选格')
                    return
                row = service.get(intent['nonce'])
                cfg = json.loads(row['config_json'])
                text = f'🎟 <b>确认刮第{intent["cell"]}格？</b>\n将扣除 <b>{cfg["cost"]}</b> 积分。\n奖励可能为0或低于投入。\n\n<i>原格可能被抢先领取；只有成功刮开才扣费。</i>'
                keys = [[{'text': f'确认扣{cfg["cost"]}分', 'callback_data': f'ggc:{intent["token"]}:yes'},
                         {'text': '取消 · 不扣分', 'callback_data': f'ggc:{intent["token"]}:no'}]]
                CALL_DELIVERY.set(None)
                try:
                    sent = await self._call('sendMessage', {'chat_id': int(intent['tg_id']), 'text': text, 'parse_mode': 'HTML', 'reply_markup': {'inline_keyboard': keys}})
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - confirmation never commits fees on network failure
                    sent = None
                state = service.confirmation_sent(intent['token'], sent, CALL_DELIVERY.get() or {})
                await self._answer_callback(callback_id, '请在本人私聊确认扣费' if state == 'pending' else '私聊确认未送达，不会扣费；请先打开或解除屏蔽Bot')
            elif parts[0] == 'ggc':
                result = service.confirm(parts[1], actor, message, parts[2])
                text = '🍃 已取消，未扣积分。' if result.get('cancelled') else f'🎟 第{result["cell"]}格已揭晓 · 获得 <b>{result["reward"]}积分</b>。'
                await self._call('editMessageText', {'chat_id': (message.get('chat') or {}).get('id'), 'message_id': message.get('message_id'), 'text': text, 'parse_mode': 'HTML', 'reply_markup': {'inline_keyboard': []}})
                if not result.get('cancelled'):
                    await self._scratch9_publish(result['nonce'])
            else:
                raise PlayError('刮刮乐按钮无效')
        except (PlayError, GroupPointsError) as exc:
            await self._answer_callback(callback_id, '🍃 '+str(exc))

    async def _scratch9_publish(self, nonce):
        if not self.enabled:
            return
        if not hasattr(self, '_scratch9_publish_locks'):
            self._scratch9_publish_locks = {}
        async with self._scratch9_publish_locks.setdefault(nonce, asyncio.Lock()):
            service = self._scratch9_service()
            row = service.begin_publish(nonce)
            if row is None:
                return
            text, keys = view(row, row['_cells'])
            image = render(row, row['_cells'])
            fields = {'chat_id': row['chat_id'], 'reply_markup': {'inline_keyboard': keys}}
            initial = row['card_message_id'] is None
            if initial:
                fields.update(caption=text, parse_mode='HTML')
                if row['thread_id']:
                    fields['message_thread_id'] = row['thread_id']
                if row['command_message_id']:
                    fields['reply_parameters'] = json.dumps({'message_id': row['command_message_id'], 'allow_sending_without_reply': True})
            else:
                fields['message_id'] = row['card_message_id']
                fields['media'] = json.dumps({'type': 'photo', 'media': 'attach://photo', 'caption': text, 'parse_mode': 'HTML'}, ensure_ascii=False)
            CALL_DELIVERY.set(None)
            try:
                sent = await self._call_multipart('sendPhoto' if initial else 'editMessageMedia', fields, {'photo': ('scratch9.png', image, 'image/png')})
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - persist unknown instead of a second public card
                sent = None
            failure = CALL_DELIVERY.get() or {}
            if isinstance(sent, dict) and type(sent.get('message_id')) is int:
                service.published(row, sent['message_id'])
            elif not initial and (sent is True or failure.get('not_modified')):
                service.published(row, row['card_message_id'])
            else:
                service.publish_failed(row, failure)

    async def _scratch9_tick(self):
        if self._db is None or self._points is None or self._plugins is None:
            return
        service = self._scratch9_service()
        service.expire_due()
        if not hasattr(self, '_scratch9_boot_key'):
            self._scratch9_boot_key = secrets.token_hex(12)
        slots = service.schedule(self._group_allowlist(), self._scratch9_boot_key, bot_enabled=self.enabled)
        if not self.enabled:
            return
        for slot in slots:
            destination = slot['destination']
            chat = {'id': int(destination), 'type': 'supergroup'} if destination.lstrip('-').isdigit() else await self._call('getChat', {'chat_id': destination})
            if not isinstance(chat, dict):
                continue
            row = service.auto_create(slot, chat)
            if row:
                await self._scratch9_publish(row['nonce'])
        for pending in service.pending_cards():
            await self._scratch9_publish(pending['nonce'])
