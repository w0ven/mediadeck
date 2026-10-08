"""Group-only red packets, independent durable cards (not personal menu panels)."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from html import escape

from app.modules.red_packets import (
    COMMANDS,
    RESULTS_PER_PAGE,
    PacketError,
    PacketService,
    public_name,
)
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
        def name(value):
            value = public_name({'first_name': value or ''})
            return escape(value[:40] + ('…' if len(value) > 40 else ''))
        title = f'🎁 <b>{mode}红包</b>'
        if row['audience'] == 'whitelist':
            title += ' · 白名单'
        body = (title + f"\n<b>{name(row.get('public_actor_name'))}</b>"
                f" · {row['total']} 积分 · {row['parts']} 份")
        keyboard = []
        if row['status'] == 'draft':
            hours = json.loads(row['config_json'])['ttl_hours']
            body += f'\n\n有效 {hours} 小时 · 待确认'
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
            return body + '\n\n已取消', keyboard
        active = row['status'] == 'active' and row['expires_at'] > time.time()
        state = '已领取' if active else ('已领完' if row['status'] == 'exhausted' else '已过期')
        body += f"\n\n{state} <b>{row['claimed_count']}/{row['parts']}</b>"
        results = service.claims(nonce, row['claimed_count'])
        pages = max(1, (len(results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
        page = min(max(0, row.get('result_page', 0)), pages - 1)
        for claim in results[page * RESULTS_PER_PAGE:(page + 1) * RESULTS_PER_PAGE]:
            body += f"\n{name(claim['display_name'])} · <b>{claim['amount']}</b> 积分"
        if row['status'] == 'exhausted' and results:
            best = min(results, key=lambda c: (-c['amount'], c['slot']))
            body += f"\n\n🏆 手气最佳 <b>{name(best['display_name'])}</b> · {best['amount']} 积分"
        if active:
            deadline = datetime.fromtimestamp(row['expires_at'], timezone(timedelta(hours=8)))
            body += '\n\n<i>截止 ' + deadline.strftime('%m-%d %H:%M') + '</i>'
            keyboard.append([{'text': '领取红包', 'callback_data': 'rpclaim:' + nonce}])
        if pages > 1:
            def turn(number, text):
                return {'text': text, 'callback_data': f'rppage:{nonce}:{number}'}
            keys = [turn(page, f'{page + 1}/{pages}')]
            if page > 0:
                keys.insert(0, turn(page - 1, '‹'))
            if page < pages - 1:
                keys.append(turn(page + 1, '›'))
            keyboard.append(keys)
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
            # Terminal cards remain pageable. Keep the same lock while older
            # waiters exist, so a new page cannot race them on a different lock.

    async def _packet_callback(self, data, message, actor, callback_id):
        try:
            service = self._packet_service()
            tokens = data.split(':')
            nonce = tokens[1]
            if tokens[0] == 'rpclaim' and len(tokens) == 2:
                result = service.claim(nonce, actor, message)
                text = ('已领取过：' if result['already'] else '🧧 领取成功：') + str(result['amount']) + ' 积分'
            elif tokens[0] == 'rppage' and len(tokens) == 3:
                service.page(nonce, actor, message, int(tokens[2]))
                text = '领取记录'
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
