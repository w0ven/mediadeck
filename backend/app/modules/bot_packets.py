"""Group-only red packets, independent durable cards (not personal menu panels)."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from html import escape

from app.modules.red_packets import COMMANDS, PacketError, PacketService
from app.modules.report_delivery import CALL_DELIVERY


class PacketBotMixin:
    def _packet_service(self):
        if self._db is None or self._points is None:
            raise PacketError('积分服务暂不可用')
        self._check_bot_identity()
        return PacketService(self._db, self._members, self._points,
                             lambda: self._plugins.config('red_packets') if self._plugins else {},
                             lambda: self._plugin_on('red_packets'),
                             self._group_chat_allowed, self.is_admin, self._active_bot_id)

    def _packet_view(self, row, service):
        nonce = row['nonce']
        mode = '等额' if row['mode'] == 'equal' else '拼手气'
        audience = '白名单专属' if row['audience'] == 'whitelist' else '全体有效成员'
        title = '🎁 <b>积分奖励红包</b>' if row['funding'] == 'reward' else '🧧 <b>积分红包</b>'
        body = (title + f"\n发起人：<b>{escape(row['actor_name'])}</b>"
                f"\n<b>{row['total']} 积分</b> · {row['parts']} 份 · {mode}"
                f'\n领取范围：{audience}')
        keyboard = []
        if row['status'] == 'draft':
            hours = json.loads(row['config_json'])['ttl_hours']
            body += (f'\n有效期：确认后 {hours} 小时\n'
                     + ('确认发出后从本人积分扣除总额，未领部分到期退回。' if row['funding'] == 'user'
                        else '确认后向符合条件的领取者发放奖励，未领部分到期结束。')
                     + '\n\n请选择参数，核对后发出；此确认10分钟有效。')
            def edit(field, value, text):
                return {'text': text, 'callback_data': f'rpedit:{nonce}:{field}:{value}'}
            keyboard.append([edit('mode', 'random', ('✓ ' if mode == '拼手气' else '') + '拼手气'),
                             edit('mode', 'equal', ('✓ ' if mode == '等额' else '') + '等额')])
            cfg = service._config()
            totals = [n for n in (100, 500, 1000) if row['parts'] <= n <= cfg['max_total']
                      and (row['mode'] == 'random' or n % row['parts'] == 0)]
            counts = [n for n in (1, 5, 10, 20) if n <= min(cfg['max_parts'], row['total'])
                      and (row['mode'] == 'random' or row['total'] % n == 0)]
            if totals:
                keyboard.append([edit('total', n, str(n) + '分') for n in totals])
            if counts:
                keyboard.append([edit('parts', n, str(n) + '份') for n in counts])
            keyboard.append([edit('audience', 'all', ('✓ ' if row['audience'] == 'all' else '') + '全体成员'),
                             edit('audience', 'whitelist', ('✓ ' if row['audience'] == 'whitelist' else '') + '白名单')])
            keyboard.append([{'text': '确认发出', 'callback_data': 'rpok:' + nonce},
                             {'text': '取消', 'callback_data': 'rpcancel:' + nonce}])
            return body, keyboard
        if row['status'] == 'cancelled':
            body += '\n\n已取消或确认过期，未扣费。'
        elif row['status'] == 'active' and row['expires_at'] > time.time():
            # Explicit timezone; the server's timezone need not be Beijing.
            deadline = datetime.fromtimestamp(row['expires_at'], timezone(timedelta(hours=8)))
            body += f"\n已领 <b>{row['claimed_count']} / {row['parts']}</b> 份"
            body += '\n截止：' + deadline.strftime('%m-%d %H:%M') + '（北京时间）'
            body += '\n\n每人一次，发起人不可领；领取结果仅本人可见。'
            keyboard = [[{'text': '🧧 领取红包', 'callback_data': 'rpclaim:' + nonce}]]
        elif row['status'] == 'exhausted':
            body += f"\n\n✨ 已全部领完 · {row['parts']} / {row['parts']} 份"
            best = service.best(nonce)
            if best:
                body += f"\n🏆 手气最佳：{escape(best['username'])} · {best['amount']} 积分"
        else:
            body += f"\n\n已到期 · 已领 {row['claimed_count']} / {row['parts']} 份"
            if row['status'] == 'expired':
                body += (f"\n未领 {row['refunded']} 积分已退回发起账号。" if row['funding'] == 'user'
                         else '\n未领部分已结束，已领取积分不受影响。')
            else:
                body += '\n到期结算待重试，已领取积分不受影响。'
        return body, keyboard

    async def _packet_command(self, message):
        parts = str(message.get('text') or '').strip().split()
        first = parts[0].lower() if parts else ''
        if first.split('@', 1)[0] not in COMMANDS:
            return False
        self._check_bot_identity()
        if '@' in first and (not self._bot_username or first.split('@', 1)[1] != self._bot_username.lower()):
            return True
        chat = message.get('chat') or {}
        if chat.get('type') not in ('group', 'supergroup') or not self._group_chat_allowed(chat):
            return True
        try:
            args = parts[1:]
            mode, audience = 'random', 'all'
            if args and args[0] in ('等额', 'equal', '拼手气', 'random'):
                mode = 'equal' if args.pop(0) in ('等额', 'equal') else 'random'
            if args and args[-1] in ('白名单', 'whitelist'):
                args.pop()
                audience = 'whitelist'
            if not args:
                args = ['100', '10']
            if len(args) != 2:
                raise PacketError('用法：/红包 100 10 或 /红包 等额 100 10；末尾加「白名单」可限定领取范围。')
            service = self._packet_service()
            row = service.prepare(message, args[0], args[1], mode, audience)
            if row['card_message_id']:
                return True  # duplicate update neither sends a new card nor changes parameters
            if not service.begin_card_send(row['nonce']):
                return True  # unknown ACK / interrupted sending: never blind resend
            body, keys = self._packet_view(row, service)
            payload = {'chat_id': chat['id'], 'text': body, 'parse_mode': 'HTML',
                       'reply_markup': {'inline_keyboard': keys}, 'disable_web_page_preview': True}
            if row['thread_id']:
                payload['message_thread_id'] = row['thread_id']
            CALL_DELIVERY.set(None)
            result = await self._call('sendMessage', payload)
            if not isinstance(result, dict) or not service.bind_card(row['nonce'], result.get('message_id')):
                service.card_send_failed(row['nonce'], (CALL_DELIVERY.get() or {}).get('state'))
                self._last_error = '红包确认卡未确认，未扣费；请发送新命令'
        except PacketError as exc:
            payload = {'chat_id': chat['id'], 'text': '⛔ ' + str(exc)}
            if message.get('message_thread_id'):
                payload['message_thread_id'] = message['message_thread_id']
            await self._call('sendMessage', payload)
        except (sqlite3.Error, OverflowError):
            self._last_error = '红包数据库暂不可用；原意图未重复执行'
        return True

    async def _packet_render(self, nonce):
        # Always reload after acquiring the lock: old claim cards cannot overwrite
        # newer progress. A crash leaves a durable dirty version for the worker.
        if not hasattr(self, '_packet_render_locks'):
            self._packet_render_locks = {}
        lock = self._packet_render_locks.setdefault(nonce, asyncio.Lock())
        async with lock:
            service = self._packet_service()
            row = service.get(nonce)
            if not row or not row['card_message_id']:
                return
            body, keys = self._packet_view(row, service)
            CALL_DELIVERY.set(None)
            result = await self._call('editMessageText', {'chat_id': int(row['chat_id']),
                                      'message_id': row['card_message_id'], 'text': body,
                                      'parse_mode': 'HTML', 'reply_markup': {'inline_keyboard': keys},
                                      'disable_web_page_preview': True})
            success = bool(result) or bool((CALL_DELIVERY.get() or {}).get('not_modified'))
            service.rendered(nonce, row['render_version'], success)
            if row['status'] in ('expired', 'exhausted', 'cancelled') and success:
                self._packet_render_locks.pop(nonce, None)

    async def _packet_callback(self, data, message, actor, callback_id):
        try:
            service = self._packet_service()
            tokens = data.split(':')
            nonce = tokens[1]
            if tokens[0] == 'rpclaim' and len(tokens) == 2:
                result = service.claim(nonce, actor, message)
                text = ('已领取过：' if result['already'] else '🧧 领取成功：') + str(result['amount']) + ' 积分'
            elif tokens[0] == 'rpedit' and len(tokens) == 4:
                service.edit_draft(nonce, actor, message, tokens[2], tokens[3])
                text = '参数已更新，请核对后确认'
            elif tokens[0] in ('rpok', 'rpcancel') and len(tokens) == 2:
                row = service.confirm(nonce, actor, message, cancel=tokens[0] == 'rpcancel')
                text = '已取消，未扣费' if row['status'] == 'cancelled' else '红包已确认，群卡正在更新'
            else:
                raise PacketError('红包操作无效')
        except (PacketError, ValueError, TypeError, IndexError) as exc:
            await self._call('answerCallbackQuery', {'callback_query_id': callback_id,
                             'text': str(exc) if isinstance(exc, PacketError) else '红包参数无效，未执行', 'show_alert': True})
            return
        except (sqlite3.Error, OverflowError):
            await self._call('answerCallbackQuery', {'callback_query_id': callback_id,
                             'text': '积分数据库暂不可用，请稍后重试原卡片', 'show_alert': True})
            return
        await self._call('answerCallbackQuery', {'callback_query_id': callback_id, 'text': text, 'show_alert': True})
        await self._packet_render(nonce)

    async def _packet_tick(self):
        service = self._packet_service()
        service.expire_due()  # independent of the issuing switch/Bot network availability
        if self.enabled:
            for row in service.pending_renders():
                await self._packet_render(row['nonce'])

    async def _packet_worker(self):
        while True:
            try:
                await self._packet_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - durable money/UI states stay available for next tick
                self._last_error = '红包到期或群卡同步待重试'
            await asyncio.sleep(10)
