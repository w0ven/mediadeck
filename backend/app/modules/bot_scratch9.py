"""One shared image message; owner confirms by clicking the same group cell twice."""
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
            service = self._scratch9_service()
            row = service.create(message)
            await self._scratch9_publish(row['nonce'])
            row = service.get(row['nonce'])  # initial/retried send may just have obtained its ID
            if row['command_message_id'] != message.get('message_id'):
                text = '🎟 本群已有一场刮刮乐，原卡发送尚未确认，不重复开场。'
                if row['card_message_id'] is not None:
                    text = '🎟 本群已有一场刮刮乐，请使用当前原卡，不重复开场。'
                    username = row['chat_username']
                    group = row['chat_id']
                    url = f'https://t.me/{username}/{row["card_message_id"]}' if username else f'https://t.me/c/{group[4:]}/{row["card_message_id"]}' if group.startswith('-100') else ''
                    if url:
                        text = '🎟 本群已有 <a href="'+escape(url, quote=True)+'">当前刮刮乐</a>，不重复开场。'
                # An earlier command may already have been deleted. This notice
                # stands alone in the requesting topic and links the original card.
                await self.send_message(chat['id'], text, thread_id=self._thread_id(message))
        except (PlayError, GroupPointsError) as exc:
            await self.send_message(chat.get('id'), '🍃 '+escape(str(exc)), thread_id=self._thread_id(message), reply_to_message_id=message.get('message_id'))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - no database/network internals in group feedback
            self._last_error = '刮刮乐开场未确认，请稍后重试'
            await self.send_message(chat.get('id'), '🍃 开场暂未确认，请稍后重试，不会重复开场。', thread_id=self._thread_id(message))
        return True

    async def _scratch9_callback(self, data, message, actor, callback_id):
        result = None
        try:
            service = self._scratch9_service()
            parts = data.split(':')
            if len(parts) != 3:
                raise PlayError('刮刮乐按钮无效')
            if parts[0] == 'ggc':
                # Old private v0.40.5 cards cannot bypass the new group binding.
                raise PlayError('确认方式已更新，请在原群原卡点击同一格两次确认；不会扣积分')
            if parts[0] != 'gg' or not parts[2].isdigit():
                raise PlayError('格子无效')
            intent = service.select(parts[1], int(parts[2]), actor, message, callback_id)
            if intent.get('_confirm'):
                result = service.confirm(intent['token'], actor, message, 'yes', request=callback_id)
                text = '🍃 确认已取消，未扣积分，请重新选格。' if result.get('cancelled') else f'🎟 第{result["cell"]}格已刮开，获得{result["reward"]}积分。'
            else:
                if intent['state'] != 'group_pending':
                    raise PlayError('确认已取消或失效，请重新选格；不会扣积分')
                cfg = json.loads(service.get(intent['nonce'])['config_json'])
                prefix = '上次确认已过期，未扣积分。\n' if intent.get('_expired') else ''
                text = prefix+f'再次点击原卡第{intent["cell"]}格，确认扣除{cfg["cost"]}积分（120秒内）。\n不再点击即不扣费；奖励可能为0或低于投入。'
        except (PlayError, GroupPointsError) as exc:
            text = '🍃 '+str(exc)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - transaction rolls back; never expose internals
            self._last_error = '刮刮乐操作未确认，请稍后重试'
            text = '🍃 操作暂未完成，请稍后重试；不会重复扣费。'
        # The FIRST and ONLY answer must contain the useful text: an early empty
        # ACK consumes the callback and hides later feedback in real Telegram.
        await self._call('answerCallbackQuery', {'callback_query_id': callback_id,
                         'text': text, 'show_alert': True}, timeout=10)
        if result is not None and not result.get('cancelled'):
            await self._scratch9_publish(result['nonce'])

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
