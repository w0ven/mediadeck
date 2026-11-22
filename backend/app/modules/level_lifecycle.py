"""Expiry deletion uses durable exclusion, remote-first removal and retained ledgers."""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from datetime import datetime
from uuid import uuid4

from app.modules import member_ops
from app.modules.bot_activity import remain_label
from app.modules.economy_rules import BEIJING, economy_write
from app.modules.expiry_notices import ExpiryNotices
from app.modules.levels import DAY
from app.modules.members import MANUAL_STATES
from app.modules.plugins import Plugin, Spec
from app.modules.usage import run_usage_io


def _bj_time(ts):
    return datetime.fromtimestamp(ts, BEIJING).strftime('%Y-%m-%d %H:%M')


async def _send_private(bot, chat, body, keyboard):
    try:
        params = inspect.signature(bot.send).parameters
        supports_kb = 'keyboard' in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (TypeError, ValueError):
        supports_kb = True
    return await (bot.send(chat, body, keyboard=keyboard) if supports_kb else bot.send(chat, body))


def unsettled(conn, uid):
    queries = [
        ("SELECT 1 FROM play_escrows WHERE user_id=? AND amount>0", (uid,)),
        ("SELECT 1 FROM gc_seats WHERE user_id=?", (uid,)),
        ("SELECT 1 FROM activity_watch_pending WHERE emby_user_id=?", (uid,)),
        (("SELECT 1 FROM play_players p JOIN play_rounds r ON r.nonce=p.nonce "
          "WHERE p.user_id=? AND r.state IN ('lobby','running')"), (uid,)),
        (("SELECT 1 FROM niuniu_players p JOIN niuniu_rounds r ON r.nonce=p.nonce "
          "WHERE p.user_id=? AND r.state IN ('lobby','running')"), (uid,)),
        (("SELECT 1 FROM dice_bets b JOIN dice_rounds r ON r.nonce=b.nonce "
          "WHERE b.user_id=? AND r.state NOT IN ('settled','refunded','empty')"), (uid,)),
        ("SELECT 1 FROM dice_rounds WHERE actor_uid=? AND state NOT IN ('settled','refunded','empty')", (uid,)),
        (("SELECT 1 FROM red_packets WHERE actor_user_id=? AND status NOT IN ('settled','cancelled','expired') "
          "AND remaining>0"), (uid,)),
    ]
    return any(conn.execute(sql, args).fetchone() for sql, args in queries)


class LevelLifecycle:
    def __init__(self, levels, emby, *, receipts=None):
        self.levels, self.db, self.emby = levels, levels.db, emby
        self.receipts = receipts

    def candidate(self, uid, now):
        member = self.levels.members.get(uid)
        if not member or not member.get('activity_managed'):
            return None
        expiry = member.get('expires_at_effective')
        if (member.get('activity_exempt') or member.get('whitelist_card_expires_at')
                and member['whitelist_card_expires_at'] > now or not expiry
                or now < expiry+3*DAY or member.get('status') in MANUAL_STATES
                or 'admin' in member.get('roles', [])):
            return None
        return member

    def preview(self, now=None):
        now = time.time() if now is None else now
        rows = []
        with self.db.snapshot():
            for row in self.db.query('SELECT emby_user_id FROM activity_members'):
                uid = row['emby_user_id']
                member = self.candidate(uid, now)
                if member:
                    pending = unsettled(self.db._conn, uid)
                    rows.append({'user_id': uid, 'username': member['username'],
                                 'expires_at': member['expires_at_effective'],
                                 'delete_at': member['expires_at_effective']+3*DAY,
                                 'blocked': pending, 'reason': '有未结算资金或玩法' if pending else '',
                                 'lifecycle': member['activity_lifecycle']})
        return rows

    def claim(self, uid, now):
        with economy_write(self.db) as conn:
            if not self.levels.config()['config']['delete_enabled']:
                raise ValueError('自动删除未启用')
            member = self.candidate(uid, now)
            if not member or unsettled(conn, uid):
                raise ValueError('删除条件已变化或仍有未结算资金')
            state = conn.execute('SELECT * FROM activity_members WHERE emby_user_id=?', (uid,)).fetchone()
            if state['deletion_lease'] > now:
                raise ValueError('删除请求正在执行，稍后重试')
            token = uuid4().hex
            conn.execute("UPDATE activity_members SET lifecycle='deleting',deletion_token=?,deletion_updated_at=?,deletion_lease=? "
                         "WHERE emby_user_id=?", (token, now, now+120, uid))
            return token

    async def recover(self, uid):
        """Explicit operator recovery after a failed/unknown delete; never resurrect."""
        now = int(time.time())
        users = await asyncio.wait_for(self.emby.list_users(), 30)
        if not isinstance(users, list) or not any(isinstance(u, dict) and str(u.get('Id')) == uid for u in users):
            raise ValueError('远端账号存在未确认，不能解除删除互斥；请重试删除核验')
        with economy_write(self.db) as conn:
            row = conn.execute('SELECT * FROM activity_members WHERE emby_user_id=?', (uid,)).fetchone()
            if not row or row['lifecycle'] != 'deleting' or row['deletion_lease'] > now:
                raise ValueError('删除仍在执行或状态已变化')
            conn.execute("UPDATE activity_members SET lifecycle='ready',deletion_error='',deletion_token='' WHERE emby_user_id=?", (uid,))
            self.levels.members.audit('operator', 'activity.delete.recover', uid, '确认远端仍存在，解除删除互斥；期限不变', conn=conn)
        return {'ok': True, 'recovered': True}

    async def delete(self, uid, now=None):
        now = int(time.time() if now is None else now)
        # A current remote administrator cannot be deleted by a timer. If the
        # read is unknown, do not claim or send a delete request.
        users = await asyncio.wait_for(self.emby.list_users(), 30)
        if not isinstance(users, list) or any(not isinstance(u, dict) or not u.get('Id') for u in users):
            raise ValueError('远端用户状态未知')
        remote = next((u for u in users if str(u['Id']) == uid), None)
        if remote is not None and (remote.get('Policy') or {}).get('IsAdministrator') is not False:
            raise ValueError('远端管理员或权限未知，豁免自动删除')
        token = await run_usage_io(self.claim, uid, now)
        # Existing deletion path removes local membership only after a confirmed
        # remote success. No SQLite lock is held over network I/O.
        def authorize():
            row = self.db.one('SELECT * FROM activity_members WHERE emby_user_id=?', (uid,))
            return bool(row and row['deletion_token'] == token and row['lifecycle'] == 'deleting'
                        and self.candidate(uid, now) and self.levels.config()['config']['delete_enabled'])
        emby = self.emby
        # Capture public identity before deletion removes the member. Only a
        # confirmed DELETE (not allow_absent recovery) can enqueue this event.
        member = self.levels.members.get(uid)
        on_deleted = (lambda: self.receipts.stage(member)) if self.receipts else None
        class CheckedDeletion:
            async def list_users(self):
                return await emby.list_users()

            async def delete_user(self, user_id):
                # Existing self-service adapter rechecks remote administrator
                # identity immediately before DELETE and local exclusion after I/O.
                receipt = {'on_deleted': on_deleted} if on_deleted is not None else {}
                confirmed = await emby.delete_selfservice_user(user_id, authorize=authorize, allow_absent=True, **receipt)
                if not confirmed:
                    raise ValueError('远端删除即时复核未确认，保留本地账号')
                return True
        try:
            result = await asyncio.wait_for(member_ops.execute_delete(self.levels.members, CheckedDeletion(), uid,
                actor='activity.expiry', cascade=False, delete_emby=True, authorize=authorize), 60)
        except BaseException:
            # Unknown result keeps exclusion through restart; a retry rechecks
            # remote absence and resumes the exact existing removal operation.
            with economy_write(self.db) as conn:
                conn.execute('UPDATE activity_members SET deletion_lease=0,deletion_error=? WHERE emby_user_id=? AND deletion_token=?',
                             ('远端结果未确认，等待重试；未清除业务账本', uid, token))
            raise
        with economy_write(self.db) as conn:
            conn.execute('UPDATE activity_members SET deletion_lease=0,lifecycle=?,deletion_error=?,deletion_updated_at=? '
                         'WHERE emby_user_id=? AND deletion_token=?',
                         ('deleted' if result.get('deleted') else 'deleting',
                          '' if result.get('deleted') else '删除未确认，等待重试；账号及账本保留', now, uid, token))
        return result


class LevelLifecyclePlugin(Plugin):
    spec = Spec(id='activity_lifecycle', name='等级自动保号与到期检查', icon='⏳', interval=60,
                description='用户主动开启后到期前72小时内自动续费；过期72小时后只预览，真实删除还需等级配置单独启用。')

    async def _deliver_auto_receipts(self, levels, now):
        bot = self.ctx.telegram
        if not bot or not getattr(bot, 'enabled', False):
            return 0
        sent = 0
        pending = levels.db.query(
            "SELECT user_id, expiry FROM activity_notices WHERE stage='auto_renewed' AND sent_at IS NULL AND retry_at<=?",
            (now,),
        )
        for item in pending:
            uid, prev_expiry = item['user_id'], item['expiry']
            member = levels.members.get(uid)
            chat = (member or {}).get('tg_user_id')
            rec = levels.db.one(
                "SELECT result_json FROM economy_receipts WHERE scope='activity.renew' AND request_id=? AND user_id=?",
                (f'auto:{prev_expiry}', uid),
            )
            if not chat or not rec:
                levels.db.execute(
                    "UPDATE activity_notices SET sent_at=? WHERE user_id=? AND expiry=? AND stage='auto_renewed'",
                    (now, uid, prev_expiry),
                )
                continue
            with economy_write(levels.db) as conn:
                cur = conn.execute(
                    "SELECT sent_at, retry_at FROM activity_notices WHERE user_id=? AND expiry=? AND stage='auto_renewed'",
                    (uid, prev_expiry),
                ).fetchone()
                if not cur or cur['sent_at'] or cur['retry_at'] > now:
                    continue
                conn.execute(
                    "UPDATE activity_notices SET retry_at=? WHERE user_id=? AND expiry=? AND stage='auto_renewed'",
                    (now + 300, uid, prev_expiry),
                )
            data = json.loads(rec['result_json'])
            suffix = '顺延 30 天' if data['previous_expires_at'] >= now - DAY else '有效期 30 天'
            body = (
                "✅ <b>自动保号续期成功</b>\n\n"
                f"💰 本次扣除：{data['cost']} 积分（剩余 {data['balance']} 积分）\n"
                f"⏪ 原到期：{_bj_time(data['previous_expires_at'])}\n"
                f"⏩ 新到期：{_bj_time(data['expires_at'])}（{suffix}）"
            )
            keyboard = [[{'text': '🌱 等级与权益', 'callback_data': 'activity:status'},
                         {'text': '👤 个人信息', 'callback_data': 'me'}]]
            try:
                acknowledged = await _send_private(bot, chat, body, keyboard)
            except Exception:  # noqa: BLE001 - retain retry without claiming delivery
                acknowledged = False
            if acknowledged:
                levels.db.execute(
                    "UPDATE activity_notices SET sent_at=? WHERE user_id=? AND expiry=? AND stage='auto_renewed'",
                    (now, uid, prev_expiry),
                )
                sent += 1
        return sent

    async def notices(self, levels, now):
        bot = self.ctx.telegram
        if not bot or not getattr(bot, 'enabled', False):
            return 0
        sent = 0
        for member in levels.members.list(limit=None):
            expiry = member.get('expires_at_effective')
            uid, chat = member['emby_user_id'], member.get('tg_user_id')
            if not chat or not member.get('activity_managed') or member.get('activity_exempt') or not expiry or expiry-now > 3*DAY:
                continue
            stage = 'expired' if expiry <= now else 'due'
            with economy_write(levels.db) as conn:
                row = conn.execute('SELECT * FROM activity_notices WHERE user_id=? AND expiry=? AND stage=?', (uid, expiry, stage)).fetchone()
                if row and (row['sent_at'] or row['retry_at'] > now):
                    continue
                conn.execute('INSERT INTO activity_notices VALUES(?,?,?,NULL,?) ON CONFLICT(user_id,expiry,stage) DO UPDATE SET retry_at=excluded.retry_at',
                             (uid, expiry, stage, now+300))
            info = levels.status(uid, now)
            price = info['renewal_price']
            shortfall = info['shortfall']
            balance = info.get('balance', max(0, price - shortfall))
            auto_renew = bool(info.get('auto_renew', member.get('auto_renew')))
            exp_str = _bj_time(expiry)
            del_str = _bj_time(expiry + 3 * DAY)
            held = 0
            if shortfall:
                held = levels.db.one(
                    "SELECT COALESCE(SUM(amount),0) n FROM play_escrows WHERE user_id=? AND amount>0",
                    (uid,),
                )['n']
            if stage == 'expired':
                bal_desc = f"当前余额 {balance} 积分，还差 {shortfall} 积分" if shortfall else f"当前余额 {balance} 积分，可直接续期"
                body = (
                    "🛑 <b>账号已到期 · 保号期提醒</b>\n\n"
                    f"账号已于 {exp_str} 到期，当前已暂停播放。\n"
                    f"续期 30 天需 {price} 积分（{bal_desc}）。"
                )
                if held:
                    body += f"\n另有 {held} 积分正冻结在牌桌。"
                body += f"\n\n请在 {del_str} 前完成积分续期以恢复使用，超期未续账号可能被清理。"
                keyboard = [[{'text': '⏳ 续期 30 天', 'callback_data': 'activity:renew'},
                             {'text': '🌱 等级与权益', 'callback_data': 'activity:status'}]]
            elif auto_renew:
                remain_str = remain_label(expiry - now)
                gap_desc = (f"当前余额 {balance} 积分（还差 {shortfall} 积分），自动保号暂无法扣费。"
                            if shortfall else f"当前余额 {balance} 积分，账号当前状态暂不自动扣费。")
                body = (
                    "⚠️ <b>自动保号积分不足提醒</b>\n\n"
                    f"账号将于 {exp_str} 到期（{remain_str}）。\n"
                    f"续期 30 天需 {price} 积分，{gap_desc}"
                )
                if held:
                    body += f"\n另有 {held} 积分正冻结在牌桌。"
                body += (
                    "\n\n到期后暂停播放，72 小时保留期内补足积分后仍会自动/手动续期；"
                    f"超期未续可能被清理（清理候选：{del_str}）。"
                )
                keyboard = [[{'text': '⏳ 续期 30 天', 'callback_data': 'activity:renew'},
                             {'text': '🌱 等级与权益', 'callback_data': 'activity:status'}]]
            else:
                remain_str = remain_label(expiry - now)
                bal_desc = f"当前余额 {balance} 积分，还差 {shortfall} 积分" if shortfall else f"当前余额 {balance} 积分"
                body = (
                    "⏳ <b>账号即将到期提醒</b>\n\n"
                    f"账号将于 {exp_str} 到期（{remain_str}）。\n"
                    f"续期 30 天需 {price} 积分（{bal_desc}）。\n"
                    "🤖 自动保号当前为「未开启」，可点击下方按钮手动续期或开启自动保号。"
                )
                if held:
                    body += f"\n另有 {held} 积分正冻结在牌桌。"
                body += (
                    "\n\n到期后暂停播放，72 小时保留期内仍可积分续期；"
                    f"超期未续可能被清理（清理候选：{del_str}）。"
                )
                keyboard = [[{'text': '⏳ 续期 30 天', 'callback_data': 'activity:renew'},
                             {'text': '🤖 开启自动保号', 'callback_data': 'activity:auto:on'}]]
            try:
                acknowledged = await _send_private(bot, chat, body, keyboard)
            except Exception:  # noqa: BLE001 - retain retry without claiming delivery
                acknowledged = False
            if acknowledged:
                levels.db.execute('UPDATE activity_notices SET sent_at=? WHERE user_id=? AND expiry=? AND stage=?', (now, uid, expiry, stage))
                sent += 1
        return sent

    async def run(self, config):
        levels = self.ctx.levels
        if not levels or not levels.config()['config']['enabled'] or not self.ctx.registry.enabled(self.spec.id):
            return {'ok': True, '状态': '等级生命周期未启用'}
        now = int(time.time())
        renewed = 0
        errors = []
        for row in levels.db.query("SELECT emby_user_id FROM activity_members WHERE auto_renew=1 AND lifecycle='ready'"):
            uid = row['emby_user_id']
            member = levels.members.get(uid)
            if not member or not member.get('expires_at_effective') or member['expires_at_effective']-now > 3*DAY:
                continue
            prev_expiry = member['expires_at_effective']
            try:
                await run_usage_io(levels.renew, uid, f"auto:{prev_expiry}", now=now, automatic=True)
                if member.get('tg_user_id'):
                    levels.db.execute(
                        "INSERT OR IGNORE INTO activity_notices VALUES(?,?,'auto_renewed',NULL,0)",
                        (uid, prev_expiry),
                    )
                renewed += 1
            except ValueError as exc:
                errors.append({'user_id': uid, 'reason': str(exc)})
        await self._deliver_auto_receipts(levels, now)
        notices = await self.notices(levels, now)
        receipts = ExpiryNotices(levels.db, lambda: self.ctx.store.section('telegram'))
        service = LevelLifecycle(levels, self.ctx.emby, receipts=receipts)
        candidates = await run_usage_io(service.preview, now)
        deleted = 0
        if levels.config()['config']['delete_enabled']:
            for row in candidates:
                if row['blocked']:
                    continue
                try:
                    result = await service.delete(row['user_id'], now)
                    deleted += bool(result.get('deleted'))
                except Exception:  # noqa: BLE001 - retain unknown remote result, retry without losing history
                    errors.append({'user_id': row['user_id'], 'reason': '远端删除未确认，保留重试'})
        # Delivery/retry is independent of account cleanup, including when the
        # delete switch is now off or no member remains after a process restart.
        broadcasts = await receipts.deliver(self.ctx.telegram)
        return {'ok': True, '已续期': renewed, '已提醒': notices, '删除候选': len(candidates), '已删除': deleted,
                '已播报': broadcasts, '待处理': errors[:30]}
