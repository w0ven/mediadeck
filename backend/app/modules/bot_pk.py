"""/pk in groups: one challenge card, edited in place to the dice result."""
from __future__ import annotations

import asyncio
import re
from html import escape

from app.modules.game_mentions import result_mention
from app.modules.game_ui import defer_ui
from app.modules.group_points import GroupPointsError, reliable_user
from app.modules.pk import DICE, PkService, result_of
from app.modules.play_money import PlayError
from app.modules.report_delivery import CALL_DELIVERY

FACES = {1: '⚀', 2: '⚁', 3: '⚂', 4: '⚃', 5: '⚄', 6: '⚅'}
USAGE = '用法：回复对方消息发 /pk 或 /pk 100，也可以 /pk @对方 100（数字是押注积分，不写就是不押积分的友谊赛）'


def pk_args(message):
    """(target, stake) from a /pk message: a mention entity, else the replied-to sender."""
    text = str(message.get('text') or '')
    units = text.encode('utf-16-le')
    target, pieces = None, []
    for entity in message.get('entities') or []:
        kind, start, length = entity.get('type'), entity.get('offset'), entity.get('length')
        if kind not in ('mention', 'text_mention') or type(start) is not int or type(length) is not int:
            continue
        piece = units[start * 2:(start + length) * 2].decode('utf-16-le', 'ignore')
        if target is not None:
            raise PlayError('一次只能和一个人 PK')
        pieces.append(piece)
        user = entity.get('user') if kind == 'text_mention' else None
        target = {'tg_id': str(user.get('id') or ''), 'user': user} if isinstance(user, dict) else {'username': piece}
    rest = text
    for piece in pieces:
        rest = rest.replace(piece, ' ', 1)
    words = rest.split()[1:]
    if len(words) > 1 or words and not re.fullmatch('[0-9]{1,19}', words[0]):
        raise PlayError(USAGE)
    if target is None:
        reply = message.get('reply_to_message') or {}
        # In forum topics every message carries the topic opener as reply_to_message.
        topic_root = message.get('is_topic_message') and reply.get('message_id') == message.get('message_thread_id')
        sender = reply.get('from')
        if reply and not topic_root and isinstance(sender, dict) and not reply.get('sender_chat'):
            target = {'tg_id': str(sender.get('id') or ''), 'user': sender}
    if target is None:
        raise PlayError(USAGE)
    return target, int(words[0]) if words else 0


class PkBotMixin:
    def _pk_service(self):
        if self._db is None or self._points is None:
            raise PlayError('积分服务暂不可用')
        self._check_bot_identity()
        return PkService(self._db, self._members, self._points,
                         lambda: self._plugins.config('pk') if self._plugins else {},
                         lambda: self._plugin_on('pk'), self._group_chat_allowed, self._active_bot_id)

    def _pk_view(self, row):
        a = result_mention(row['actor_tg_id'], row['actor_name'], row['actor_username'])
        b = result_mention(row['target_tg_id'], row['target_name'], row['target_username'])
        stake = row['stake']
        bet = f'押 <b>{stake:,}</b> 积分' if stake else '友谊赛 · 不押积分'
        refunded = '，押注已退回' if stake else ''
        result = result_of(row)
        if row['state'] == 'pending':
            minutes = max(1, round((row['expires_at'] - row['created_at']) / 60))
            outcome = '点数大的赢走对方押注，平局各自退回' if stake else '点数大的获胜，平局不分胜负'
            text = (f'🎲 <b>骰子 PK</b>\n\n{a} 向 {b} 发起挑战 · {bet}\n'
                    f'双方各掷{"三" if DICE == 3 else DICE}颗骰子，{outcome}。\n\n'
                    f'⏳ 等 {b} 在 {minutes} 分钟内应战')
            n = row['nonce']
            return text, [[{'text': '✅ 应战', 'callback_data': f'pk:{n}:accept'},
                           {'text': '❌ 拒绝', 'callback_data': f'pk:{n}:decline'}]]
        if row['state'] == 'settled':
            dice, totals, winner = result['dice'], result['totals'], result['winner']

            def line(who, side):
                return f'{who}  {" ".join(FACES[v] for v in dice[side])}  = <b>{totals[side]}</b>'
            text = f'🎲 <b>骰子 PK · 开骰</b> · {bet}\n\n{line(a, "actor")}\n{line(b, "target")}\n\n'
            if winner is None:
                text += '🤝 平局' + ('，押注各自退回' if stake else '')
            else:
                text += f'🏆 {a if winner == "actor" else b} 获胜' + (f'，赢得 <b>{stake:,}</b> 积分' if stake else '')
            return text, []
        if row['state'] == 'declined':
            return f'🎲 骰子 PK\n\n{b} 拒绝了 {a} 的挑战{refunded}', []
        if row['state'] == 'withdrawn':
            return f'🎲 骰子 PK\n\n{a} 撤回了对 {b} 的挑战{refunded}', []
        if row['state'] == 'expired':
            return f'🎲 骰子 PK\n\n⌛ {b} 没有应战，挑战已取消{refunded}', []
        reason = result.get('reason')
        return f'🎲 骰子 PK\n\n挑战已取消{"：" + escape(reason) if reason else ""}{refunded}', []

    async def _pk_command(self, message):
        parts = str(message.get('text') or '').strip().split()
        first = parts[0].lower() if parts else ''
        if first.split('@', 1)[0] != '/pk':
            return False
        self._check_bot_identity()
        if '@' in first and (not self._bot_username or first.split('@', 1)[1] != self._bot_username.lower()):
            return True
        chat = message.get('chat') or {}
        if chat.get('type') not in ('group', 'supergroup') or not self._group_chat_allowed(chat):
            return True
        try:
            reliable_user(message)
            target, stake = pk_args(message)
            row = self._pk_service().create(message, target, stake)
            defer_ui(self, ('pk', row['nonce']), lambda: self._pk_open(row['nonce'], message))
        except (PlayError, GroupPointsError) as exc:
            await self.send_message(chat.get('id'), '🍃 ' + escape(str(exc)), thread_id=self._thread_id(message),
                                    reply_to_message_id=message.get('message_id'))
        return True

    async def _pk_open(self, nonce, message):
        with self._group_command_context(message):
            await self._pk_publish(nonce)
        await self._drain_group_commands()

    async def _pk_callback(self, data, message, actor, callback_id):
        text, show_alert, publish_nonce = '', False, None
        try:
            tokens = data.split(':')
            if len(tokens) != 3 or tokens[0] != 'pk':
                raise PlayError('PK 按钮无效')
            row = self._pk_service().respond(tokens[1], actor, message, tokens[2])
            publish_nonce = row['nonce']
            text = {'settled': '🎲 已开骰', 'declined': '已拒绝', 'withdrawn': '已撤回'}.get(row['state'], '')
        except (PlayError, GroupPointsError) as exc:
            text, show_alert = '🍃 ' + str(exc), True
        await self._call('answerCallbackQuery', {'callback_query_id': callback_id,
                         'text': text, 'show_alert': show_alert}, timeout=10)
        if publish_nonce is not None:
            defer_ui(self, ('pk', publish_nonce), lambda: self._pk_publish(publish_nonce))

    def _pk_transport_allowed(self, row):
        self._check_bot_identity()
        return (self.enabled and row['bot_id'] == self._active_bot_id
                and self._group_chat_allowed({'id': int(row['chat_id']), 'type': 'supergroup'}))

    async def _pk_publish(self, nonce):
        if not self.enabled:
            return
        locks = self.__dict__.setdefault('_pk_publish_locks', {})
        async with locks.setdefault(nonce, asyncio.Lock()):
            service = self._pk_service()
            row = service.begin_publish(nonce)
            if not row:
                return
            text, keys = self._pk_view(row)
            initial = row['card_message_id'] is None
            payload = {'chat_id': int(row['chat_id']), 'text': text, 'parse_mode': 'HTML',
                       'reply_markup': {'inline_keyboard': keys}}
            if initial:
                payload['reply_parameters'] = {'message_id': row['command_message_id'], 'allow_sending_without_reply': True}
                if row['thread_id']:
                    payload['message_thread_id'] = row['thread_id']
            else:
                payload['message_id'] = row['card_message_id']
            CALL_DELIVERY.set(None)
            network_started = False
            try:
                if not self._pk_transport_allowed(row):
                    CALL_DELIVERY.set({'state': 'failed'})
                    result = None
                else:
                    network_started = True
                    result = await self._call('sendMessage' if initial else 'editMessageText', payload)
            except asyncio.CancelledError:
                if not network_started:
                    service.publish_failed(row, {'state': 'retry'})
                raise
            except Exception:  # noqa: BLE001 - money is already durable; never post a second card
                result = None
            failure = CALL_DELIVERY.get() or {}
            if isinstance(result, dict) and type(result.get('message_id')) is int:
                service.published(row, result['message_id'])
            elif not initial and (result is True or failure.get('not_modified')):
                service.published(row, row['card_message_id'])
            else:
                service.publish_failed(row, failure)
                if initial and failure.get('state') == 'failed':
                    service.cancel_unsent(nonce, '挑战卡发送失败')

    async def _pk_tick(self):
        if self._db is None or self._points is None or self._plugins is None:
            return
        service = self._pk_service()
        service.expire_due()
        if self.enabled:
            for row in service.pending_cards('pk'):
                await self._pk_publish(row['nonce'])
