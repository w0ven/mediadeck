"""Versioned levels, effective base benefits and atomic thirty-day renewals."""
from __future__ import annotations

import json
import time
from contextlib import nullcontext

from app.modules.economy_rules import day_bounds, economy_write, encode, receipt, save_receipt
from app.modules.enforcement import desired_policy, fingerprint
from app.modules.groups import WHITELIST_GROUP_ID
from app.modules.watch_rewards import (
    LEVEL_ICONS,
    LEVEL_NAMES,
    LEVEL_PRICES,
    LEVEL_THRESHOLDS,
    LEVEL_TITLES,
    merge_intervals,
)

DAY = 86400


def defaults():
    rows = []
    for i in range(7):
        rows.append({"level": i+1, "name": LEVEL_NAMES[i], "title": LEVEL_TITLES[i],
                     "icon": LEVEL_ICONS[i], "threshold": LEVEL_THRESHOLDS[i], "price": LEVEL_PRICES[i],
                     "bandwidth_limit_kbps": (120, 120, 140, 160, 180, 220, 280)[i]*1000,
                     "max_streams": (2, 2, 2, 2, 3, 3, 4)[i], "request_quota": (3, 3, 4, 5, 6, 8, 10)[i]})
    rows.append(dict(rows[-1], level=8, name="道祖", title="一炁化三清", icon="🪷", threshold=None, price=1))
    return {"enabled": False, "delete_enabled": False, "levels": rows}


def validate(config):
    if not isinstance(config, dict) or set(config) != {"enabled", "delete_enabled", "levels"}:
        raise ValueError("等级配置字段无效")
    if type(config["enabled"]) is not bool or type(config["delete_enabled"]) is not bool:
        raise ValueError("等级与删除开关须为布尔值")
    levels = config["levels"]
    if not isinstance(levels, list) or len(levels) != 8:
        raise ValueError("需配置7个成长等级和1个专属等级")
    previous = -1
    keys = set(defaults()["levels"][0])
    for i, row in enumerate(levels):
        if not isinstance(row, dict) or set(row) - {"traffic_quota_bytes"} != keys or type(row["level"]) is not int or row["level"] != i+1:
            raise ValueError("等级顺序或字段无效")
        for key in ("name", "title", "icon"):
            if not isinstance(row[key], str) or not 1 <= len(row[key].strip()) <= 40:
                raise ValueError("等级名称、称号、图标须为1～40字符")
        if i < 7:
            value = row["threshold"]
            if type(value) is not int or not previous < value <= 10**9 or (i == 0 and value != 0):
                raise ValueError("成长门槛须从0开始严格递增")
            previous = value
        elif row["threshold"] is not None:
            raise ValueError("Lv8为专属授予，不设置成长门槛")
        for key, high in (("price", 10**9),
                          ("bandwidth_limit_kbps", 10000000), ("max_streams", 100), ("request_quota", 10000)):
            if type(row[key]) is not int or not 1 <= row[key] <= high:
                raise ValueError(f"{key}须为合法正整数")
    return config


def qualification(db, uid, now, days=30, required_seconds=4800, required_days=3):
    """Legacy verified-watch statistics for API compatibility, never a renewal gate."""
    start = now-days*DAY
    rows = db.query("SELECT started_at,ended_at FROM watch_verified_samples "
                    "WHERE emby_user_id=? AND ended_at>? AND started_at<?", (uid, start, now))
    # Settled unions also survive normal raw-proof retention. Union both sources.
    rows += db.query("SELECT started_at,ended_at FROM activity_watch_intervals "
                     "WHERE emby_user_id=? AND ended_at>? AND started_at<?", (uid, start, now))
    by_day = {}
    for a, b in merge_intervals((max(start, r['started_at']), min(now, r['ended_at'])) for r in rows):
        while a < b:
            day, _, midnight = day_bounds(a)
            stop = min(midnight, b)
            by_day[day] = by_day.get(day, 0) + stop-a
            a = stop
    seconds = sum(by_day.values())
    count = sum(value >= 600 for value in by_day.values())
    return {"seconds": seconds, "days": count, "required_seconds": required_seconds,
            "required_days": required_days, "eligible": seconds >= required_seconds and count >= required_days}


class LevelService:
    def __init__(self, db, members, points, activity):
        self.db, self.members, self.points, self.activity = db, members, points, activity

    async def sync_effects(self, callback):
        now = int(time.time())
        synced = 0
        for member in self.members.list(limit=None):
            if not member.get('activity_managed'):
                continue
            uid = member['emby_user_id']
            fp = fingerprint(desired_policy(member))
            old = self.db.one('SELECT * FROM activity_effect_sync WHERE user_id=?', (uid,))
            if old and (old['fingerprint'] == fp or old['retry_at'] > now):
                continue
            try:
                result = await callback(uid, old['bandwidth'] if old else None)
                confirmed = result.get('remote_ok') is True
            except Exception:  # noqa: BLE001 - do not mark unknown remote sync as delivered
                confirmed = False
            self.db.execute('INSERT INTO activity_effect_sync VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE '
                            'SET fingerprint=excluded.fingerprint,bandwidth=excluded.bandwidth,retry_at=excluded.retry_at,error=excluded.error',
                            (uid, fp if confirmed else '', member.get('bandwidth_limit_kbps'),
                             0 if confirmed else now+60, '' if confirmed else '远端权益同步未确认'))
            synced += confirmed
        return synced

    def config(self):
        row = self.db.one("SELECT * FROM activity_config WHERE id=1")
        return {"revision": row["revision"] if row else 0,
                "config": json.loads(row["config_json"]) if row else defaults()}

    def save_config(self, config, revision):
        validate(config)
        with economy_write(self.db) as conn:
            current = self.config()
            if revision != current["revision"]:
                raise ValueError("等级配置已变化，请刷新后重试")
            conn.execute("INSERT INTO activity_config VALUES(1,?,?) ON CONFLICT(id) DO UPDATE "
                         "SET revision=excluded.revision,config_json=excluded.config_json",
                         (revision+1, encode(config)))
        return self.config()

    def project(self, member, group):
        """Called before personal overrides and active-card additions, never writes."""
        config = self.config()
        enrollment = self.db.one("SELECT * FROM activity_members WHERE emby_user_id=?", (member['emby_user_id'],))
        if not config['config']['enabled']:
            return group
        if not enrollment:
            if member.get('group_id') != WHITELIST_GROUP_ID:
                return group
            enrollment = {'special_level': 0, 'auto_renew': 0, 'lifecycle': 'ready'}
        uid = member['emby_user_id']
        growth = self.db.one("SELECT COALESCE(SUM(delta),0) AS n FROM growth_ledger WHERE emby_user_id=?", (uid,))["n"]
        rows = config['config']['levels']
        index = 7 if enrollment['special_level'] == 8 else max(i for i in range(7) if growth >= rows[i]['threshold'])
        level = dict(rows[index], growth=growth, next_threshold=rows[index+1]['threshold'] if index < 6 else None)
        member['activity_level'] = level
        member['activity_managed'] = True
        member['auto_renew'] = bool(enrollment['auto_renew'])
        member['activity_lifecycle'] = enrollment['lifecycle']
        member['activity_exempt'] = member.get('group_id') == WHITELIST_GROUP_ID
        if member['activity_exempt']:
            return group  # every whitelist/card right remains authoritative
        return dict(group or {}, name=level['name'], billing_mode='time', duration_days=30,
                    **{key: level[key] for key in ('bandwidth_limit_kbps', 'max_streams', 'request_quota')})

    def status(self, uid, now=None):
        now = time.time() if now is None else now
        with self.db.snapshot():
            member = self.members.get(uid)
            if not member:
                raise ValueError("账号不存在")
            result = self.activity.summary(uid, now)
            level = member.get('activity_level') or result['level']
            price = level.get('price', level.get('renewal_price', 80))
            expiry = member.get('expires_at_effective')
            balance = self.points.balance(uid)
            worn = self.db.one('SELECT tag FROM member_titles WHERE emby_user_id=? AND worn=1 AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?)', (uid, now))
            result.update(worn_title=worn['tag'] if worn else None, level=level, managed=bool(member.get('activity_managed')),
                          exempt=bool(member.get('activity_exempt')), auto_renew=bool(member.get('auto_renew')),
                          expires_at=expiry, delete_at=expiry+3*DAY if expiry else None,
                          balance=balance, renewal_price=price, shortfall=max(0, price-balance),
                          window_open=bool(expiry and expiry-now <= 3*DAY),
                          qualification=qualification(self.db, uid, now),
                          lifecycle=member.get('activity_lifecycle', 'ready'))
            return result

    def enroll_new(self, uid, now, conn=None):
        if not self.config()['config']['enabled']:
            return
        with (nullcontext(conn) if conn is not None else economy_write(self.db)) as tx:
            member = self.members.get(uid)
            if (not member or member['status'] != 'active' or member['group_id'] == WHITELIST_GROUP_ID
                    or tx.execute('SELECT 1 FROM activity_members WHERE emby_user_id=?', (uid,)).fetchone()):
                return
            tx.execute('INSERT INTO activity_members(emby_user_id,enrolled_at) VALUES(?,?)', (uid, now))
            expiry = max(now+30*DAY, int(member.get('expires_at_effective') or 0))
            self.write_expiry(tx, uid, member, expiry, now)

    def grant_special(self, uid, actor):
        with economy_write(self.db) as conn:
            member = self.members.get(uid)
            if not member or member.get('activity_lifecycle') == 'deleting':
                raise ValueError('账号不存在或删除结果待确认')
            conn.execute('INSERT INTO activity_members(emby_user_id,special_level,enrolled_at) VALUES(?,8,?) '
                         'ON CONFLICT(emby_user_id) DO UPDATE SET special_level=8', (uid, int(time.time())))
            self.members.audit(actor, 'activity.grant_lv8', uid, '管理员明确授予专属Lv8，期限与余额不变', conn=conn)
        return self.members.get(uid)

    def set_auto(self, uid, enabled, *, tg_uid=None, binding_revision=None):
        if type(enabled) is not bool:
            raise ValueError("自动保号开关无效")
        with economy_write(self.db) as conn:
            if tg_uid is not None:
                owner = conn.execute('SELECT tg_user_id FROM members WHERE emby_user_id=?', (uid,)).fetchone()
                if not owner or str(owner[0]) != str(tg_uid) or self.binding_revision(uid) != binding_revision:
                    raise ValueError('账号绑定已变化，请重新确认')
            row = conn.execute("SELECT * FROM activity_members WHERE emby_user_id=?", (uid,)).fetchone()
            if not row or row['lifecycle'] != 'ready':
                raise ValueError("账号尚未转入等级体系或删除结果待确认")
            conn.execute("UPDATE activity_members SET auto_renew=? WHERE emby_user_id=?", (int(enabled), uid))
        return {"ok": True, "auto_renew": enabled}

    def binding_revision(self, uid):
        return self.db.one("SELECT MAX(id) AS revision FROM audit_log WHERE subject=? AND action IN "
                           "('member.telegram.bind','member.telegram.unbind','telegram.rebind.approved')", (uid,))['revision']

    def renew(self, uid, request_id, *, now=None, automatic=False, bot_intent=None):
        now = int(time.time() if now is None else now)
        request = {"days": 30, "automatic": automatic}
        with economy_write(self.db) as conn:
            intent = None
            if bot_intent is not None:
                nonce, tg_uid, chat_id = bot_intent
                intent = conn.execute('SELECT * FROM activity_renew_intents WHERE nonce=?', (nonce,)).fetchone()
                binding = conn.execute('SELECT tg_user_id FROM members WHERE emby_user_id=?', (uid,)).fetchone()
                if (not intent or intent['user_id'] != uid or intent['tg_user_id'] != str(tg_uid)
                        or intent['chat_id'] != str(chat_id) or not binding or str(binding[0]) != str(tg_uid)
                        or request_id != 'bot-renew:'+nonce):
                    raise ValueError('续费确认不属于当前账号，请重新预览')
                if json.loads(intent['snapshot_json']).get('binding') != self.binding_revision(uid):
                    raise ValueError('账号绑定已变化，请重新预览确认')
                if intent['result_json']:
                    return json.loads(intent['result_json'])
                if intent['expires_at'] <= now:
                    raise ValueError('续费确认已过期，请重新预览')
            cached = receipt(conn, 'activity.renew', request_id, uid, request)
            if cached is not None:
                return cached
            member = self.members.get(uid)
            if not member or not member.get('activity_managed') or member.get('activity_exempt'):
                raise ValueError("账号未启用普通等级续费或属于白名单豁免")
            if member.get('activity_lifecycle') != 'ready':
                raise ValueError("删除结果待确认，不能续费")
            if automatic and not member.get('auto_renew'):
                raise ValueError("用户未开启自动保号")
            if automatic and member.get('status') in ('suspended', 'pending'):
                raise ValueError('人工禁用或未开通账号不自动扣费')
            expiry = member.get('expires_at_effective')
            if not expiry or expiry-now > (DAY if automatic else 3*DAY):
                raise ValueError("仅到期前72小时内可续30天，自动保号在24小时内检查")
            # Viewing earns rewards independently; renewal spends the current level price.
            level = member['activity_level']
            cost = level['price']
            if intent is not None:
                snapshot = json.loads(intent['snapshot_json'])
                if snapshot != {'expiry': expiry, 'price': cost, 'level': level['level'],
                                'revision': self.config()['revision'], 'binding': self.binding_revision(uid)}:
                    raise ValueError('期限或等级价格已变化，请重新预览确认')
            new_expiry = max(now, int(expiry))+30*DAY
            balance = self.points._apply(conn, uid, -cost, 'activity.renew', request_id, 'activity', now)
            self.write_expiry(conn, uid, member, new_expiry, now)
            result = {"ok": True, "expires_at": new_expiry, "previous_expires_at": expiry,
                      "cost": cost, "balance": balance, "days": 30, "level": level['level'],
                      "config_revision": self.config()['revision'], "remote_ok": None}
            save_receipt(conn, 'activity.renew', request_id, uid, request, result)
            if intent is not None:
                conn.execute('UPDATE activity_renew_intents SET result_json=? WHERE nonce=?', (encode(result), nonce))
            self.members.audit('activity', 'activity.renew', uid, encode(result), conn=conn)
            return result

    @staticmethod
    def write_expiry(conn, uid, member, expiry, now):
        overrides = dict(member.get('overrides') or {})
        if 'expires_at_override' in overrides:
            overrides['expires_at_override'] = expiry
            conn.execute("UPDATE members SET overrides_json=?,updated_at=? WHERE emby_user_id=?",
                         (encode(overrides), now, uid))
        else:
            conn.execute("UPDATE members SET expires_at=?,updated_at=? WHERE emby_user_id=?", (expiry, now, uid))
        # Intentionally do not reset status, traffic, request usage, or restrictions.
