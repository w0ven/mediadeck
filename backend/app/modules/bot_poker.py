"""Chinese original group game cards + authenticated private PNG delivery."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from html import escape

from app.modules.economy_rules import BEIJING, economy_write
from app.modules.group_points import GroupPointsError, reliable_user
from app.modules.play_money import PlayError
from app.modules.poker import DEFAULTS, PokerService
from app.modules.poker_image import render
from app.modules.report_delivery import CALL_DELIVERY


def name(value):
    return escape(str(value or '成员')[:40])


def help_text(cfg):
    return ('🃏 <b>炸金花 · 玩法</b>\n\n'
            '2～5人，每人3张52张标准扑克，无大小王、不重复。\n'
            '豹子＞顺金＞金花＞顺子＞对子＞单张。A23为最小顺子，QKA最大；不采用235吃豹子。同级比点数，不比花色。\n\n'
            '发起人报名后，2人起可开始，5人自动尝试开局。报名不扣分；开局全员一次冻结预算并投入底注，任一成员不足就不开局。\n'
            '未看牌为闷牌；点“看牌”私聊收到牌图，此后跟注为闷注2倍，私聊失败也视为已看，可启动Bot后重试原牌。看牌不延长倒计时。\n'
            '轮到你可跟注、加底注一档或翻倍加注、弃牌、与在局对手比牌。比牌付本人当前跟注额2倍；同牌力发起者出局。\n'
            '任何人达到预算上限，或下一次付费超过其未用预算，所有仍在局的玩家立即摊牌，不追加扣款。最高牌力平分全池，整数余数随机公平分配，无抽成；未用预算全退。只剩一人即获全池。\n'
            '结束群里只公开仍在局的牌图，不公开已弃牌手牌。\n\n'
            f'底注 {cfg["min_ante"]}～{cfg["max_ante"]} 积分；本局每人预算 {(cfg["budget"] if "budget_limit" in cfg else cfg.get("default_budget", cfg["budget"]))}（可选至 {cfg.get("budget_limit", cfg["budget"])}）。'
            '\n默认 /炸金花；指定 /炸金花 10 30（底注、每人共同预算）。'
            f'组局 {cfg["lobby_seconds"]} 秒未开始取消（报名无扣款）；每步 {cfg["step_seconds"]} 秒未操作自动弃牌。'
            '\n有效绑定成员可参与，管理员同样扣本人积分；旧局使用开局时规则。')


class PokerBotMixin:
    def _poker_service(self):
        if self._db is None or self._points is None: raise PlayError('积分服务暂不可用')
        self._check_bot_identity()
        return PokerService(self._db, self._members, self._points,
                            lambda: self._plugins.config('poker') if self._plugins else {},
                            lambda: self._plugin_on('poker'), self._group_chat_allowed, self._active_bot_id)

    def _poker_view(self, row, service):
        players = row['_players'] if '_players' in row else service.players(row['nonce'])
        cfg = json.loads(row['config_json'])
        nonce, turn = row['nonce'], row['turn']
        keyboard = []
        text = f'🃏 <b>炸金花</b>\n{name(row["actor_name"])} · 底注 {row["stake"]} · 开局冻结 {cfg["budget"]}/人'
        if row['state'] == 'lobby':
            text += f'\n\n已报名 <b>{len(players)}/5</b>'
            text += ''.join('\n'+name(p['display_name']) for p in players)
            if row['start_error']: text += '\n\n'+row['start_error']
            keyboard += [[{'text': '参加', 'callback_data': f'pg:{nonce}:0:join'},
                          {'text': '开始', 'callback_data': f'pg:{nonce}:0:start'},
                          {'text': '退出', 'callback_data': f'pg:{nonce}:0:leave'}]]
        elif row['state'] == 'running':
            current = next(p for p in players if p['seat'] == row['current_seat'])
            text += f'\n\n第 {turn} 步 · 轮到 <b>{name(current["display_name"])}</b>'
            for p in players:
                state = '已弃牌' if p['folded'] else '已看牌' if p['seen'] else '闷牌'
                text += f'\n{name(p["display_name"])} · {state} · 已押 {p["invested"]}'
            text += f'\n\n池 {sum(p["invested"] for p in players)} 积分'
            follow = row['blind']*(2 if current['seen'] else 1)
            keyboard += [[{'text': f'跟注 {follow}', 'callback_data': f'pg:{nonce}:{turn}:follow'},
                          {'text': '加一档', 'callback_data': f'pg:{nonce}:{turn}:raise'},
                          {'text': '翻倍加注', 'callback_data': f'pg:{nonce}:{turn}:double'}],
                         [{'text': '私下看牌 / 重试', 'callback_data': f'pgl:{nonce}'},
                          {'text': '弃牌', 'callback_data': f'pg:{nonce}:{turn}:fold'}]]
            compare = [{'text': '比牌 · '+str(p['display_name'] or '成员')[:12],
                        'callback_data': f'pg:{nonce}:{turn}:compare:{p["seat"]}'} for p in players if not p['folded'] and p['seat'] != current['seat']]
            keyboard += [compare[i:i+2] for i in range(0, len(compare), 2)]
        elif row['state'] == 'settled':
            text += f'\n\n已结算 · 池 {json.loads(row["result_json"])["pot"]} 积分'
            for p in players:
                outcome = f'获得 {p["result_amount"]} 积分' if p['result_amount'] else '已弃牌' if p['folded'] else '未获奖池'
                text += f'\n{name(p["display_name"])} · {outcome}'
        else:
            text += '\n\n组局超时 · 未扣款' if row['state'] == 'expired' else '\n\n已取消 · 未扣款'
        if row['state'] in ('lobby', 'running'):
            text += '\n<i>截止 '+datetime.fromtimestamp(row['expires_at'], BEIJING).strftime('%H:%M:%S')+'</i>'
        keyboard.append([{'text': '玩法', 'callback_data': f'pgh:{nonce}'}])
        return text, keyboard

    async def _poker_command(self, message):
        parts = str(message.get('text') or '').strip().split()
        first = parts[0].lower() if parts else ''
        verb = first.split('@', 1)[0]
        if verb not in ('/炸金花', '/poker', '/zjh', '/看牌', '/炸金花帮助'): return False
        self._check_bot_identity()
        if '@' in first and (not self._bot_username or first.split('@', 1)[1] != self._bot_username.lower()): return True
        chat = message.get('chat') or {}
        try:
            tg = reliable_user(message)
            if verb in ('/看牌', '/炸金花帮助'):
                if chat.get('type') != 'private' or str(chat.get('id')) != tg: return True
                if message.get('sender_chat') or message.get('forward_origin') or message.get('forward_date'): return True
                if len(parts) > 2: raise PlayError('用法：/看牌 或 /看牌 局号')
                service = self._poker_service()
                if verb == '/炸金花帮助':
                    await self.send_message(tg, help_text({**DEFAULTS, **service.config()}))
                    return True
                nonce = parts[1] if len(parts) == 2 else None
                if nonce is None:
                    row = self._db.one("SELECT r.nonce FROM play_rounds r JOIN play_players p ON p.nonce=r.nonce WHERE r.kind='poker' AND r.bot_id=? AND p.tg_id=? AND r.state='running' ORDER BY r.created_at DESC LIMIT 1", (self._active_bot_id, tg))
                    if not row: raise PlayError('没有进行中的牌局；可用 /看牌 局号 重试原牌')
                    nonce = row['nonce']
                service.look(nonce, message['from'])
                await self._poker_photos()
                job = self._db.one("SELECT state FROM play_photos WHERE bot_id=? AND nonce=? AND recipient=? AND mode='private'", (self._active_bot_id, nonce, tg))
                if not job or job['state'] != 'sent':
                    await self.send_message(tg, '牌图未送达，原牌已保留；请稍后用 /看牌 重试。')
                await self._poker_publish(nonce)
                return True
            if chat.get('type') not in ('group', 'supergroup') or not self._group_chat_allowed(chat): return True
            if len(parts) > 3 or any(not re.fullmatch(r'[0-9]{1,6}', part) for part in parts[1:]):
                raise PlayError('用法：/炸金花 或 /炸金花 10 30（底注、每人局预算）')
            await self.send_message(chat.get('id'), '🐂 新局已改为五张牛牛：用 /牛牛 或 /牛牛 100（每人投入）。', thread_id=self._thread_id(message), reply_to_message_id=message.get('message_id'))
        except (PlayError, GroupPointsError) as exc:
            await self.send_message(chat.get('id'), escape(str(exc)), thread_id=self._thread_id(message), reply_to_message_id=message.get('message_id'))
        return True

    async def _poker_callback(self, data, message, actor, callback_id):
        await self._answer_callback(callback_id)
        try:
            tg = reliable_user({'from': actor})
            service = self._poker_service()
            parts = data.split(':')
            if data.startswith(('pgl:', 'pgh:')):
                if len(parts) != 2: raise PlayError('游戏按钮无效')
                nonce = parts[1]
                if parts[0] == 'pgh':
                    with economy_write(self._db) as conn:
                        row = service.get(nonce)
                        if not row: raise PlayError('游戏不存在')
                        service.context(conn, row, message, data)
                    sent = await self.send_message(tg, help_text(json.loads(row['config_json'])))
                    await self._answer_callback(callback_id, '玩法已发私聊' if sent else '私聊不可达，请先打开Bot并 /start，再点玩法')
                    return
                row = service.look(nonce, actor, message, data)
                await self._poker_photos()
                job = self._db.one("SELECT state FROM play_photos WHERE bot_id=? AND nonce=? AND recipient=? AND mode='private'", (self._active_bot_id, nonce, tg))
                await self._answer_callback(callback_id, '牌图已发私聊' if job and job['state'] == 'sent' else '私聊未送达，请先打开Bot /start，再点看牌重试原牌')
            else:
                if len(parts) not in (4, 5) or not parts[2].isascii() or not parts[2].isdecimal(): raise PlayError('游戏按钮无效')
                nonce, turn, op = parts[1], int(parts[2]), parts[3]
                if turn == 0:
                    if len(parts) != 4: raise PlayError('游戏按钮无效')
                    row = service.lobby(nonce, actor, message, op)
                else:
                    if (op == 'compare') != (len(parts) == 5): raise PlayError('比牌按钮无效')
                    if len(parts) == 5 and not re.fullmatch('[1-5]', parts[4]): raise PlayError('比牌对手无效')
                    row = service.action(nonce, turn, actor, message, op, int(parts[4]) if len(parts) == 5 else 0)
                await self._answer_callback(callback_id, '已更新')
            await self._poker_publish(row['nonce'])
            await self._poker_photos()
        except (PlayError, GroupPointsError) as exc:
            await self._answer_callback(callback_id, str(exc))

    async def _poker_publish(self, nonce):
        from app.modules.telegram import _CALL_ERROR
        if not hasattr(self, '_play_locks'): self._play_locks = {}
        async with self._play_locks.setdefault(nonce, asyncio.Lock()):
            service = self._poker_service()
            row = service.begin_publish(nonce)
            if row is None: return
            text, keyboard = self._poker_view(row, service)
            body = {'chat_id': row['chat_id'], 'text': text, 'parse_mode': 'HTML', 'reply_markup': {'inline_keyboard': keyboard}}
            if row['thread_id']: body['message_thread_id'] = row['thread_id']
            initial = row['card_message_id'] is None
            if initial: body['reply_parameters'] = {'message_id': row['command_message_id']}
            else: body['message_id'] = row['card_message_id']
            _CALL_ERROR.set(None)
            CALL_DELIVERY.set(None)
            response = await self._call('sendMessage' if initial else 'editMessageText', body)
            failure = CALL_DELIVERY.get() or {}
            error = _CALL_ERROR.get()
            error = str(self._last_error if error is None else error).lower()
            if isinstance(response, dict) and type(response.get('message_id')) is int: service.published(row, response['message_id'])
            elif not initial and (response is True or failure.get('not_modified') or 'not modified' in error): service.published(row, row['card_message_id'])
            else: service.publish_failed(row, failure)

    async def _poker_photos(self, limit=20):
        from app.modules.telegram import _CALL_ERROR
        if not self.enabled: return
        service = self._poker_service()
        for _ in range(limit):
            job = service.claim_photo()
            if not job: break
            if job.get('skip'): continue
            content = json.loads(job['content_json'])
            image = render(content, private=job['mode'] == 'private')
            fields = {'chat_id': job['recipient'], 'caption': '🃏 我的手牌 · 局号 '+job['nonce'] if job['mode'] == 'private' else '🃏 摊牌结果', 'parse_mode': 'HTML'}
            if job['thread_id']: fields['message_thread_id'] = job['thread_id']
            _CALL_ERROR.set(None)
            CALL_DELIVERY.set(None)
            result = await self._call_multipart('sendPhoto', fields, {'photo': ('poker.png', image, 'image/png')})
            failure = CALL_DELIVERY.get() or {}
            error = _CALL_ERROR.get()
            error = str(self._last_error if error is None else error).lower()
            blocked = failure.get('state') == 'failed' or any(v in error for v in ('forbidden', 'chat not found', 'blocked by', 'not enough rights'))
            service.photo_finished(job, result.get('message_id') if isinstance(result, dict) else None, blocked=blocked, retry_after=int(failure.get('retry_after') or 0))

    async def _poker_tick(self):
        if self._db is None or self._points is None or self._plugins is None: return
        service = self._poker_service()
        service.expire_due()
        if self.enabled:
            for row in service.pending_cards('poker'): await self._poker_publish(row['nonce'])
            await self._poker_photos()
