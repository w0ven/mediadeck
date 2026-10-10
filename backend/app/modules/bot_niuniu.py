"""Original picture during play; close it and deliver one independent final result."""
from __future__ import annotations

import asyncio
import json
import re
import time
from html import escape

from app.modules.economy_rules import economy_write
from app.modules.game_ui import defer_ui, render_image
from app.modules.group_points import GroupPointsError, reliable_user
from app.modules.niuniu import CATEGORIES, DEFAULTS, NiuniuService, card_text, strength
from app.modules.niuniu_delivery import NiuniuDelivery
from app.modules.niuniu_image import net_label, render_room
from app.modules.play_money import PlayError
from app.modules.report_delivery import CALL_DELIVERY
from app.modules.report_delivery import failure as delivery_failure


def help_text(cfg):
    legacy = cfg.get('game') == 'niuniu5-v1'
    room = ('这张旧局按原同额投入、赢家收全池规则收尾。' if legacy else
            '创建者坐庄，固定担保4份底注；最多4位闲家各投1份。每位闲家各自对庄，输赢都是1份，无倍率、无抽成。')
    return ('🐂 <b>牛牛 · 五张定胜负</b>\n\n'+room+'\n'
            '至少1位闲家时房主可开始，满4位闲家自动开牌；入座后不主动退桌。\n'
            '每人5张牌，JQK计10。任意3张凑10的倍数，余2张定牛几；牛牛最高，无牛最低，不设特殊牌型。\n'
            '同级比最大牌面（A低、K高），再比花色：♠＞♥＞♣＞♦。\n\n'
            f'默认底注 {cfg["default_stake"]} 积分；<code>/牛牛 100</code> 为闲家每位100、庄家担保400积分。\n'
            '未使用的担保退回。一次公开手牌与结果，不跟注、不加注、不自动续局。等人超时或牌局无法安全恢复，全额退回。')


class NiuniuBotMixin:
    def _niuniu_service(self):
        if self._db is None or self._points is None:
            raise PlayError('积分服务暂不可用')
        self._check_bot_identity()
        return NiuniuService(self._db, self._members, self._points,
                             lambda: self._plugins.config('niuniu') if self._plugins else {},
                             lambda: self._plugin_on('niuniu'), self._group_chat_allowed, self._active_bot_id)

    def _niuniu_view(self, row, service):
        if row['state'] in ('settled', 'cancelled') and row.get('result_state'):
            return '🐂 <b>牛牛 · 本局已结束</b>\n结果另发一条消息', []
        players = row.get('_players') if '_players' in row else service.players(row['nonce'])
        n = row['nonce']
        bank_mode = json.loads(row['config_json']).get('game') == 'niuniu-banker-v1'
        keys = []
        body = '🐂 <b>牛牛 · '+('庄闲对决' if bank_mode else '旧局原规则')+'</b>'
        body += f'\n闲家每位 <b>{row["stake"]}</b> 积分 · 庄家担保 <b>{4*row["stake"]}</b> 积分' if bank_mode else f'\n每人投入 <b>{row["stake"]}</b> 积分'
        if row['state'] == 'lobby':
            body += f'\n\n🪑 闲家已就座 <b>{len(players)-1}/4</b> · 满员即揭晓' if bank_mode else f'\n\n🪑 已就座 <b>{len(players)}/5</b> · 满员即揭晓'
            for p in players:
                role = ('庄 · ' if p['user_id'] == row['actor_user_id'] else '闲 · ') if bank_mode else ''
                body += '\n'+role+escape(p['display_name'][:20])
            body += '\n\n<i>一位闲家起房主可开始，入座后不退桌。</i>' if bank_mode else '\n\n<i>2人起房主可开始，入座后不退桌。</i>'
            keys = [[{'text': f'🐂 闲家入座 · {row["stake"]}分' if bank_mode else f'🐂 入座 · {row["stake"]}分', 'callback_data': f'nn:{n}:join'},
                     {'text': '🎴 开始', 'callback_data': f'nn:{n}:start'}]]
        elif row['state'] == 'settled':
            result = json.loads(row['result_json'])
            body += '\n\n🎉 五张揭晓 · 各自对庄' if bank_mode else f'\n\n🎉 本局揭晓 · 总池 <b>{result["pot"]}</b> 积分'
            for p in players:
                cards = json.loads(p['cards_json'])
                bank = bank_mode and p['user_id'] == row['actor_user_id']
                role = ('庄 · ' if bank else '闲 · ') if bank_mode else ''
                body += '\n\n'+role+escape(p['display_name'][:20])+f' · <b>{CATEGORIES[strength(cards)[0]]}</b>'
                body += '\n'+'  '.join(card_text(c) for c in cards)
                if bank_mode:
                    body += '\n'+net_label(p['result_amount']-row['stake']*(4 if bank else 1))
                elif p['result_amount']:
                    body += f'\n🏆 获得 <b>{p["result_amount"]}</b> 积分'
        else:
            body += '\n\n🍃 本局已结束，投入已退回'
            body += ''.join('\n'+escape(p['display_name'][:20])+f' · 退回 {p["result_amount"]} 积分' for p in players)
        keys.append([{'text': '怎么比大小？', 'callback_data': f'nnh:{n}'}])
        return body, keys

    async def _niuniu_command(self, message):
        parts = str(message.get('text') or '').strip().split()
        first = parts[0].lower() if parts else ''
        if first.split('@', 1)[0] not in ('/牛牛', '/niuniu', '/牛牛帮助'):
            return False
        self._check_bot_identity()
        if '@' in first and (not self._bot_username or first.split('@', 1)[1] != self._bot_username.lower()):
            return True
        chat = message.get('chat') or {}
        try:
            reliable_user(message)
            if first.split('@', 1)[0] == '/牛牛帮助':
                if not self._play_entry_valid(message):
                    return True
                if chat.get('type') != 'private' and not self._group_chat_allowed(chat):
                    return True
                await self.send_message(chat.get('id'), help_text({**DEFAULTS, **self._niuniu_service().config()}), thread_id=self._thread_id(message))
                return True
            if chat.get('type') not in ('group', 'supergroup') or not self._group_chat_allowed(chat):
                return True
            if len(parts) > 2 or len(parts) == 2 and not re.fullmatch('[0-9]{1,6}', parts[1]):
                raise PlayError('用法：/牛牛 或 /牛牛 100（闲家底注，创建者坐庄担保4份）')
            row = self._niuniu_service().create(message, int(parts[1]) if len(parts) == 2 else None)
            defer_ui(self, ('niuniu', row['nonce']), lambda: self._niuniu_open(row['nonce'], message))
        except (PlayError, GroupPointsError) as exc:
            await self.send_message(chat.get('id'), '🍃 '+escape(str(exc)), thread_id=self._thread_id(message), reply_to_message_id=message.get('message_id'))
        return True

    async def _niuniu_open(self, nonce, message):
        # Initial response is deferred too; retain trusted original-command cleanup.
        with self._group_command_context(message):
            await self._niuniu_publish(nonce)
        await self._drain_group_commands()

    async def _niuniu_callback(self, data, message, actor, callback_id):
        text, show_alert, publish_nonce = '', False, None
        try:
            service = self._niuniu_service()
            tokens = data.split(':')
            if tokens[0] == 'nnh' and len(tokens) == 2:
                with economy_write(self._db) as conn:
                    row = service.get(tokens[1])
                    if not row:
                        raise PlayError('这局牛牛不存在')
                    service.context(conn, row, message, data)
                tg = reliable_user({'from': actor})
                sent = await self.send_message(tg, help_text(json.loads(row['config_json'])))
                text, show_alert = ('玩法已发私聊', False) if sent else ('请先打开Bot，再点玩法', True)
            else:
                if len(tokens) != 3 or tokens[0] != 'nn':
                    raise PlayError('牛牛按钮无效')
                row = service.lobby(tokens[1], actor, message, tokens[2])
                publish_nonce = row['nonce']
        except (PlayError, GroupPointsError) as exc:
            text, show_alert = '🍃 '+str(exc), True
        # First and only response carries the business result; a rejected join
        # needs a visible alert, not a second toast after an empty acknowledgement.
        await self._call('answerCallbackQuery', {'callback_query_id': callback_id,
                         'text': text, 'show_alert': show_alert}, timeout=10)
        if publish_nonce is not None:
            defer_ui(self, ('niuniu', publish_nonce), lambda: self._niuniu_publish(publish_nonce))

    def _niuniu_transport_allowed(self, row):
        self._check_bot_identity()
        return (self.enabled and row['bot_id'] == self._active_bot_id
                and self._group_chat_allowed({'id': int(row['chat_id']), 'type': 'supergroup'}))

    async def _niuniu_publish(self, nonce):
        if not self.enabled:
            return
        if not hasattr(self, '_niuniu_publish_locks'):
            self._niuniu_publish_locks = {}
        async with self._niuniu_publish_locks.setdefault(nonce, asyncio.Lock()):
            service = self._niuniu_service()
            row = service.begin_publish(nonce)
            if not row:
                await self._niuniu_result(nonce)
                return
            text, keys = self._niuniu_view(row, service)
            initial = row['card_message_id'] is None
            photo = row.get('card_format') == 'photo'
            payload = {'chat_id': int(row['chat_id']), 'reply_markup': {'inline_keyboard': keys}}
            if initial:
                payload['reply_parameters'] = {'message_id': row['command_message_id'], 'allow_sending_without_reply': True}
                if row['thread_id']:
                    payload['message_thread_id'] = row['thread_id']
            else:
                payload['message_id'] = row['card_message_id']
            CALL_DELIVERY.set(None)
            network_started = False
            async def deliver(method, fields, files=None):
                nonlocal network_started
                if not self._niuniu_transport_allowed(row):
                    CALL_DELIVERY.set({'state': 'failed'})
                    return None
                network_started = True
                return await self._call_multipart(method, fields, files) if files else await self._call(method, fields)
            try:
                if not initial and row.get('card_delete_state') == 'waiting':
                    # Keep the meaningful original card until the new result is ACKed;
                    # only remove its buttons, never add an end-placeholder message.
                    result = await deliver('editMessageReplyMarkup', payload)
                elif photo and not initial and row.get('result_state'):
                    payload.update(caption=text, parse_mode='HTML')
                    result = await deliver('editMessageCaption', payload)
                elif photo:
                    image = await render_image(self, render_room, row, row['_players'])
                    if initial:
                        payload.update(caption=text, parse_mode='HTML')
                        payload['reply_parameters'] = json.dumps(payload['reply_parameters'])
                    else:
                        payload['media'] = json.dumps({'type': 'photo', 'media': 'attach://photo', 'caption': text, 'parse_mode': 'HTML'}, ensure_ascii=False)
                    result = await deliver('sendPhoto' if initial else 'editMessageMedia', payload, {'photo': ('niuniu.png', image, 'image/png')})
                else:
                    payload.update(text=text, parse_mode='HTML')
                    result = await deliver('sendMessage' if initial else 'editMessageText', payload)
            except asyncio.CancelledError:
                if not network_started:service.publish_failed(row, {'state': 'retry'})
                raise
            except Exception:  # noqa: BLE001 - finance is already durable; no second result card
                result = None
            failure = CALL_DELIVERY.get() or {}
            if isinstance(result, dict) and type(result.get('message_id')) is int:
                service.published(row, result['message_id'])
            elif not initial and (result is True or failure.get('not_modified')):
                service.published(row, row['card_message_id'])
            else:
                service.publish_failed(row, failure)
                if initial and failure.get('state') == 'failed':
                    with economy_write(self._db) as conn:
                        current = service.get(nonce)
                        if current['card_message_id'] is None:
                            service._refund(conn, current, '原卡发送被拒绝', time.time())
            await self._niuniu_result(nonce)

    async def _niuniu_result(self, nonce):
        if not self.enabled:return
        delivery = NiuniuDelivery(self._niuniu_service())
        row = delivery.claim(nonce)
        if not row:
            await self._niuniu_delete_card(nonce)
            return
        if not self._group_chat_allowed({'id': int(row['chat_id']), 'type': 'supergroup'}):
            delivery.finish(row, None, {'state': 'failed'})
            return
        snapshot = json.loads(row['result_payload'])
        CALL_DELIVERY.set(None)
        try:
            image = await render_image(self, render_room, snapshot['row'], snapshot['players'])
        except asyncio.CancelledError:
            delivery.finish(row, None, {'state': 'retry'})  # No API send was attempted.
            raise
        except Exception:  # noqa: BLE001 - known local failure, no send attempted
            delivery.finish(row, None, {'state': 'retry'})
            return
        if not self._niuniu_transport_allowed(row):
            delivery.finish(row, None, {'state': 'failed'})
            return
        payload = {'chat_id': int(row['chat_id']), 'caption': snapshot['caption'], 'parse_mode': 'HTML',
                   'reply_parameters': json.dumps({'message_id': row['card_message_id'], 'allow_sending_without_reply': True})}
        if row['thread_id']:payload['message_thread_id'] = row['thread_id']
        try:
            result = await self._call_multipart('sendPhoto', payload, {'photo': ('niuniu.png', image, 'image/png')})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never replay finance on transport errors
            result = None
            CALL_DELIVERY.set(delivery_failure(exc=exc))
        delivery.finish(row, result, CALL_DELIVERY.get() or {})
        await self._niuniu_delete_card(nonce)

    async def _niuniu_delete_card(self, nonce):
        from app.modules.telegram import _CALL_ERROR, _QUIET_CALL
        if not self.enabled:return
        delivery = NiuniuDelivery(self._niuniu_service())
        row = delivery.claim_delete(nonce)
        if not row:return
        if not self._niuniu_transport_allowed(row):
            delivery.finish_delete(row, False, {'state': 'failed'})
            return
        quiet, error, receipt = _QUIET_CALL.set(True), _CALL_ERROR.set(None), CALL_DELIVERY.set(None)
        try:
            try:
                result = await self._call('deleteMessage', {'chat_id': int(row['chat_id']), 'message_id': row['card_message_id']}, timeout=10)
            except asyncio.CancelledError:
                raise  # Exact-target deletion may safely resume after its lease.
            except Exception as exc:  # noqa: BLE001 - never resend the result or money
                result = None
                CALL_DELIVERY.set(delivery_failure(exc=exc))
            missing = str(_CALL_ERROR.get() or '').lower()
            success = result is True or 'message to delete not found' in missing or 'message_id_invalid' in missing
            delivery.finish_delete(row, success, CALL_DELIVERY.get() or {})
        finally:
            _QUIET_CALL.reset(quiet)
            _CALL_ERROR.reset(error)
            CALL_DELIVERY.reset(receipt)

    async def _niuniu_photos(self):
        # Historical sent/unknown receipts remain intact; unsent old picture jobs are
        # superseded by the original card. No independent sendPhoto result is permitted.
        self._db.execute("UPDATE niuniu_rounds SET photo_state='cancelled',photo_error='结果保留在原卡，不另发图片' WHERE bot_id=? AND photo_state IN ('pending','retry')", (self._active_bot_id,))

    async def _niuniu_tick(self):
        if self._db is None or self._points is None or self._plugins is None:
            return
        service = self._niuniu_service()
        service.expire_due()
        if self.enabled:
            for row in service.pending_cards('niuniu'):
                await self._niuniu_publish(row['nonce'])
            await self._niuniu_photos()
            for row in self._db.query("SELECT nonce FROM niuniu_rounds WHERE bot_id=? AND result_state IN ('pending','retry','sending') ORDER BY created_at LIMIT 50", (self._active_bot_id,)):
                await self._niuniu_result(row['nonce'])
            for row in self._db.query("SELECT nonce FROM niuniu_rounds WHERE bot_id=? AND card_delete_state IN ('pending','retry','deleting') ORDER BY created_at LIMIT 50", (self._active_bot_id,)):
                await self._niuniu_delete_card(row['nonce'])
