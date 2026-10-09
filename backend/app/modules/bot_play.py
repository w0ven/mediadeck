"""Dedicated durable play cards and check-in deletion, not personal menu state."""
from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager

from app.modules.bot_blackwhite import BlackwhiteBotMixin
from app.modules.bot_niuniu import NiuniuBotMixin
from app.modules.bot_play_hub import PlayHubBotMixin
from app.modules.bot_play_panels import PlayPanelsBotMixin
from app.modules.bot_poker import PokerBotMixin
from app.modules.checkin_cleanup import CHECKIN_TARGET, CleanupService, payload
from app.modules.points_ranking import PointsRankingBotMixin
from app.modules.report_delivery import CALL_DELIVERY
from app.modules.shop_notices import ShopNoticeBotMixin

PLAY_CALLBACKS = ('bw:', 'bwh:', 'pg:', 'pgl:', 'pgh:', 'nn:', 'nnh:', 'pp:')


class PlayBotMixin(BlackwhiteBotMixin, NiuniuBotMixin, PokerBotMixin, PlayPanelsBotMixin, PointsRankingBotMixin, PlayHubBotMixin, ShopNoticeBotMixin):
    async def _play_command(self, message):
        first=str(message.get('text') or '').strip().split()
        verb=first[0].lower().split('@',1)[0] if first else ''
        if verb in ('/blackwhite','/黑白板','/炸金花','/poker','/zjh','/看牌','/炸金花帮助','/牛牛','/niuniu','/牛牛帮助',
                    '/积分榜','/pointsrank','/游戏','/games','/玩法'):
            self._play_remember(message,message.get('from') or {})
        return (await self._blackwhite_command(message) or await self._niuniu_command(message) or await self._poker_command(message)
                or await self._ranking_command(message) or await self._play_hub_command(message))

    async def _play_callback(self, data, message, actor, callback_id):
        self._play_remember(message,actor)
        if data.startswith(('bw:', 'bwh:')):
            await self._blackwhite_callback(data, message, actor, callback_id)
        elif data.startswith(('nn:', 'nnh:')):
            await self._niuniu_callback(data, message, actor, callback_id)
        elif data.startswith('pp:'):
            await self._play_panel_callback(data, message, actor, callback_id)
        else:
            await self._poker_callback(data, message, actor, callback_id)

    @contextmanager
    def _checkin_cleanup_context(self, message, *, command=False, actor=None):
        chat = message.get('chat') or {}
        target = None
        if (self._db is not None and self._plugin_on('checkin_cleanup')
                and chat.get('type') in ('group', 'supergroup') and self._group_chat_allowed(chat)):
            self._check_bot_identity()
            tg = str(actor if actor is not None else (message.get('from') or {}).get('id') or '')
            if self._active_bot_id.isdecimal() and tg.isdecimal() and int(tg) > 0:
                target = {'bot_id': self._active_bot_id, 'chat_id': chat['id'],
                          'thread_id': self._thread_id(message) or 0, 'actor_tg_id': tg}
                if command:
                    CleanupService(self._db).enqueue(target['bot_id'], target['chat_id'], target['thread_id'],
                                                     message['message_id'], 'command', tg)
        token = CHECKIN_TARGET.set(target)
        try:
            yield
        finally:
            CHECKIN_TARGET.reset(token)

    def _capture_checkin_feedback(self, sent, result):
        target = CHECKIN_TARGET.get()
        if (target and isinstance(result, dict) and type(result.get('message_id')) is int
                and str(sent.get('chat_id')) == str(target['chat_id'])
                and int(sent.get('message_thread_id') or 0) == target['thread_id']):
            CleanupService(self._db).enqueue(target['bot_id'], target['chat_id'], target['thread_id'],
                                             result['message_id'], 'feedback', target['actor_tg_id'])

    async def _drain_checkin_deletes(self, *, now=None, limit=50):
        if self._db is None or not self.enabled:
            return 0
        # Access the native task-local error at runtime, avoiding an import cycle.
        from app.modules.telegram import _CALL_ERROR
        self._check_bot_identity()
        service = CleanupService(self._db)
        processed = 0
        for _ in range(limit):
            clock = time.time() if now is None else float(now)
            job = service.claim(self._active_bot_id, now=clock)
            if job is None:
                break
            processed += 1
            if job.get('skip'):
                continue
            target = payload(job)
            _CALL_ERROR.set(None)
            CALL_DELIVERY.set(None)
            try:
                result = await self._call('deleteMessage', {'chat_id': target['chat_id'],
                                                           'message_id': target['message_id']})
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - only exact durable deletion target is retried
                # Deletion is idempotent; a lost ACK may safely retry exactly this target.
                result = None
            error = _CALL_ERROR.get()
            error = str(self._last_error if error is None else error).lower()
            failure = CALL_DELIVERY.get() or {}
            finished = time.time() if now is None else float(now)
            if result is True:
                service.finish(job, 'deleted', now=finished)
            elif any(word in error for word in ('message to delete not found', 'message_id_invalid', 'message identifier is not specified')):
                service.finish(job, 'gone', '消息已不存在', now=finished)
            elif failure.get('state') == 'failed' or any(word in error for word in (
                    "can't be deleted", 'cannot be deleted', 'not enough rights', 'forbidden', 'administrator rights')):
                service.finish(job, 'blocked', '删除被拒绝，请检查群删除权限；未删除', now=finished)
            elif job['attempts'] >= 20:
                service.finish(job, 'failed', '删除未确认，已达自动重试上限', now=finished)
            else:
                delay = max(int(failure.get('retry_after') or 0), min(300, 5 * 2 ** min(job['attempts'], 6)))
                service.finish(job, 'queued', '删除未确认，等待重试', retry_after=delay, now=finished)
        if processed and self._plugins and self._plugins.get('checkin_cleanup'):
            summary = service.summary()
            ok = not (summary['权限或归属待处理'] or summary['失败或超期未删'])
            self._plugins._record('checkin_cleanup', ok, summary, time.time(), 'worker')
        return processed

    async def _play_worker(self):
        while True:
            for work in (self._drain_checkin_deletes, self._blackwhite_tick, self._niuniu_tick, self._poker_tick, self._shop_notice_tick):
                try:
                    await work()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one durable job must not block other refunds
                    self._last_error = '游戏或清理任务异常，状态保留待重试'
            await asyncio.sleep(5)
