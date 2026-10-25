"""Expiry deletion uses durable exclusion, remote-first removal and retained ledgers."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from uuid import uuid4

from app.modules import member_ops
from app.modules.economy_rules import BEIJING, economy_write
from app.modules.expiry_notices import ExpiryNotices
from app.modules.levels import DAY
from app.modules.members import MANUAL_STATES
from app.modules.plugins import Plugin, Spec
from app.modules.usage import run_usage_io


def unsettled(conn, uid):
    queries = [
        ("SELECT 1 FROM play_escrows WHERE user_id=? AND amount>0", (uid,)),
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
                description='用户主动开启才自动续费；过期72小时后只预览，真实删除还需等级配置单独启用。')

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
            body = (f"保号提醒：续费30天需{info['renewal_price']}积分，余额缺口{info['shortfall']}。\n"
                    f"到期：{datetime.fromtimestamp(expiry, BEIJING).strftime('%Y-%m-%d %H:%M 北京时间')}\n"
                    f"删除候选时间：{datetime.fromtimestamp(expiry+3*DAY, BEIJING).strftime('%Y-%m-%d %H:%M 北京时间')}\n"
                    '到期停止播放，保留期间仍可查看/积分续费。/activity 查看，/renew30 续30天；自动保号需主动开启。')
            try:
                acknowledged = await bot.send(chat, body)
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
            if not member or not member.get('expires_at_effective') or member['expires_at_effective']-now > DAY:
                continue
            try:
                await run_usage_io(levels.renew, uid, f"auto:{member['expires_at_effective']}", now=now, automatic=True)
                renewed += 1
            except ValueError as exc:
                errors.append({'user_id': uid, 'reason': str(exc)})
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
