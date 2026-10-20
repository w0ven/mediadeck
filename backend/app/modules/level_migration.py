"""Read-only preview and explicit compare-and-apply migration; no production defaults."""
from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime

from app.modules.economy_rules import BEIJING, day_bounds, economy_write, encode
from app.modules.groups import WHITELIST_GROUP_ID
from app.modules.levels import DAY
from app.modules.watch_rewards import merge_intervals

MIGRATION_START = datetime(2026, 10, 1, tzinfo=BEIJING).timestamp()


def verification_cutover(db, now):
    epoch = db.one("SELECT value FROM meta WHERE key='watch_verified_since'")
    try:
        value = float(epoch['value']) if epoch else 0
        if math.isfinite(value) and 0 < value <= now:
            return value, 'watch_verified_since'
    except (ValueError, TypeError):
        pass
    first = db.one('SELECT MIN(started_at) AS at FROM watch_verified_samples')['at']
    if first is None or not math.isfinite(first) or first > now:
        raise ValueError('核验起点未知，不能决定旧采样的迁移适用范围')
    return first, 'first_verified_sample'


def migration_qualification(db, uid, now, cutoff):
    # Sole exception for this one-time classification: dated old samples before
    # strict verification began. Never use display totals or old samples after it.
    parts = []
    sources = {}
    for table, start, end in (('watch_samples', MIGRATION_START, min(cutoff, now)),
                              ('watch_verified_samples', max(MIGRATION_START, cutoff), now)):
        rows = db.query(f'SELECT started_at,ended_at FROM {table} WHERE emby_user_id=? AND ended_at>? AND started_at<?',
                        (uid, start, end)) if end > start else []
        intervals = merge_intervals((max(start, r['started_at']), min(end, r['ended_at'])) for r in rows
                                    if math.isfinite(r['started_at']) and math.isfinite(r['ended_at']))
        sources[table] = sum(b-a for a, b in intervals)
        parts += intervals
    by_day = {}
    for a, b in merge_intervals(parts):
        while a < b:
            day, _, midnight = day_bounds(a)
            stop = min(b, midnight)
            by_day[day] = by_day.get(day, 0)+stop-a
            a = stop
    seconds = sum(by_day.values())
    days = sum(value >= 600 for value in by_day.values())
    return {'seconds': seconds, 'days': days, 'eligible': seconds > 6000 and days >= 4,
            'required_seconds_exclusive': 6000, 'required_days': 4, 'sources': sources}


class LevelMigration:
    def __init__(self, levels):
        self.levels = levels
        self.db = levels.db

    def preview(self, as_of=None):
        now = int(time.time() if as_of is None else as_of)
        rows = []
        with self.db.snapshot():
            config = self.levels.config()
            since, cutoff_source = verification_cutover(self.db, now)
            for member in self.levels.members.list(limit=None):
                uid = member['emby_user_id']
                prior = self.db.one("SELECT * FROM activity_members WHERE emby_user_id=?", (uid,))
                active = migration_qualification(self.db, uid, now, since)
                balance = self.levels.points.balance(uid)
                held = self.db.one("SELECT COALESCE(SUM(amount),0) AS n FROM play_escrows WHERE user_id=?", (uid,))['n']
                raw = self.db.one("SELECT * FROM members WHERE emby_user_id=?", (uid,))
                growth = self.db.one('SELECT COALESCE(SUM(delta),0) AS n FROM growth_ledger WHERE emby_user_id=?', (uid,))['n']
                level = 8 if active['eligible'] else 1
                effective = member.get('expires_at_effective')
                reason = ''
                action = 'migrate'
                if prior:
                    action = 'already_migrated'
                elif member.get('group_id') == WHITELIST_GROUP_ID:
                    action = 'exempt'
                elif member.get('status') == 'pending':
                    action, reason = 'exception', '未开通，等待首次有效开通'
                elif (effective and effective > now+30*DAY):
                    action, reason = 'exception', '现有更长期限须人工确认，未缩短'
                elif member.get('card_group_unavailable'):
                    action, reason = 'exception', '原用户组不可用'
                # Null explicit personal expiry is a special permanent overlay,
                # not permission to silently take that right away.
                elif 'expires_at_override' in member.get('overrides', {}) and effective is None:
                    action, reason = 'exception', '个人永久期限须人工确认'
                rows.append({'user_id': uid, 'username': member.get('username', ''),
                             'action': action, 'reason': reason, 'level': level,
                             'growth_initial': 0, 'previous_growth': growth, 'qualification': active, 'balance': balance, 'held': held,
                             'cost': 0,
                             'previous_expires_at': effective,
                             'new_expires_at': now+30*DAY if action == 'migrate' else effective,
                             'status': member['status'], 'source_revision': hashlib.sha256(encode(raw).encode()).hexdigest()})
            result = {'as_of': now, 'window_since': MIGRATION_START, 'verified_since': since,
                      'cutoff_source': cutoff_source,
                      'coverage_complete': since <= MIGRATION_START,
                      'coverage_note': '核验起点前仅本次资历分类采用有日期旧采样；起点后只用严格核验。无记录不补造，不按旧采样发积分/成长。',
                      'config_revision': config['revision'], 'members': rows}
            result['hash'] = hashlib.sha256(encode(result).encode()).hexdigest()
            return result

    def apply(self, request_id, expected_hash, as_of):
        if not isinstance(request_id, str) or not request_id or len(request_id) > 120:
            raise ValueError('缺少迁移请求ID')
        if type(as_of) is not int or not isinstance(expected_hash, str):
            raise ValueError('缺少有效预览时间或摘要')
        with economy_write(self.db) as conn:
            prior = conn.execute('SELECT * FROM activity_migrations WHERE request_id=?', (request_id,)).fetchone()
            if prior:
                if prior['preview_hash'] != expected_hash:
                    raise ValueError('迁移请求ID已用于其他预览')
                return json.loads(prior['result_json'])
            if abs(time.time()-int(as_of)) > 600:
                raise ValueError('预览超过10分钟，请刷新')
            preview = self.preview(as_of)
            if preview['hash'] != expected_hash:
                raise ValueError('账号、余额或观影证据已变化，请重新预览')
            changed = []
            for row in preview['members']:
                if row['action'] != 'migrate':
                    continue
                uid = row['user_id']
                member = self.levels.members.get(uid)
                conn.execute('INSERT INTO activity_members(emby_user_id,special_level,enrolled_at) VALUES(?,?,?)',
                             (uid, 8 if row['level'] == 8 else 0, as_of))
                if row['previous_growth']:
                    conn.execute('INSERT INTO growth_ledger(emby_user_id,delta,reason,ref,created_at) VALUES(?,?,?,?,?)',
                                 (uid, -row['previous_growth'], 'activity.migrate', request_id, as_of))
                self.levels.write_expiry(conn, uid, member, row['new_expires_at'], as_of)
                self.levels.members.audit('activity', 'activity.migrate', uid, encode(row), conn=conn)
                changed.append(uid)
            result = {'ok': True, 'migrated': changed, 'preview': preview}
            conn.execute('INSERT INTO activity_migrations VALUES(?,?,?,?)',
                         (request_id, expected_hash, encode(result), as_of))
            return result
