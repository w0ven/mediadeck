"""Consented device check for strict lotteries: one physical device, several TG accounts.

Purpose-bound and opt-in per entry. The Mini App reads system/hardware level
values (ux.js) ONLY after the user tapped 「同意并报名」; without that consent an
activity with ``device_check`` cannot be entered at all. This module keeps
nothing but an activity-domain HMAC-SHA256 digest under the existing
``lottery_identity_key``. Every activity is judged on its own entries only;
nothing is carried over or compared across activities. iPhone/iPad entries keep
no digest at all: identical models report identical values.

Raw values, the client blob and the digest are never logged or returned by
any API. Ordinary participants receive identical responses whatever the
verdict; the decision that counts is the one taken inside the draw transaction.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
from collections import defaultdict

from app.modules import lottery_identity
from app.modules.economy_rules import encode

CONSENT_VERSION = 1
CONSENT = {
    'version': CONSENT_VERSION,
    'title': '设备校验',
    'body': ('参加本活动需完成设备校验：报名时将读取本设备的屏幕、显卡、系统等信息，生成不可逆的摘要，'
             '仅用于本期活动的参与资格校验，不作其他用途。不同意将无法参加本活动。'),
    'accept': '同意并报名',
    'decline': '不同意',
}
NEED_CONSENT = '需同意设备校验后才能参加'
FAILED = '未能完成设备校验，请重试'
UNAVAILABLE = '报名暂不可用，请联系管理员检查活动校验服务'
RULE = ('设备校验已开启：报名前须同意设备校验，服务端只保存不可逆摘要。每期单独判定，本期内设备摘要相同的账号'
        '判定为同一设备，关联账号全部排除中奖（包括先报名者），以开奖时的判定为准。同型号、同系统版本且设置相同的'
        '不同手机可能被误判为同一设备，更换设备或主动伪造也可能绕过检测。')

REASON = 'hardware'
LABELS = {'ip': '相同可信公网 IP', 'device': '相同浏览器安装标识', REASON: '相同设备'}
STATUS = {'same': '判定为同一设备，关联账号全部排除'}

_HEX = r'[0-9a-f]{64}'
_NUMBER = r'[0-9]{1,6}(?:\.[0-9]{1,6})?'
# Short neutral client keys -> accepted canonical form. Only values that stay
# identical across Telegram clients on one device (no language, UA, plugins,
# permissions, viewport or Telegram.WebApp data).
FIELDS = {
    'a': _HEX,  # canvas digest
    'b': r'[^\x00-\x1f\x7f|]{1,128}\|[^\x00-\x1f\x7f]{1,200}',  # WebGL vendor|renderer
    'c': _HEX,  # remaining WebGL digest
    'e': _HEX,  # audio digest
    'f': r'[0-9]{1,5}x[0-9]{1,5}x[0-9]{1,3}x[0-9]{1,2}(?:\.[0-9]{1,17})?',  # screen
    'g': _NUMBER,  # CPU cores
    'h': _NUMBER,  # device memory
    'i': _NUMBER,  # touch points
    'j': r'[A-Za-z0-9_+\-/]{1,64}',  # time zone
    'n': _HEX,  # fonts digest
}
LIMITS = {'b': 256, 'f': 64}
# Without any rendering component the remaining values are too coarse.
STRONG = ('a', 'b', 'c', 'e')
MINIMUM = 5
MAX_BLOB = 8192

SCHEMA = """
CREATE TABLE IF NOT EXISTS lottery_device_entries (
 lottery_id TEXT NOT NULL, tg_id TEXT NOT NULL, consent_version INTEGER NOT NULL, consented_at INTEGER NOT NULL,
 digest TEXT NOT NULL, verdict TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(lottery_id,tg_id)
);
CREATE INDEX IF NOT EXISTS idx_lottery_device_match ON lottery_device_entries(lottery_id,digest);
"""


def migrate(db):
    """Additive only; no backfill. Activities without the field are not device-checked."""
    db._conn.executescript(SCHEMA)


def flag(raw, anti_multi):
    """New configurations default to the strict-mode switch; it only exists inside strict mode."""
    value = raw.get('device_check', anti_multi)
    if type(value) is not bool:
        raise ValueError('设备校验开关必须为布尔值')
    return value and anti_multi


def required(config):
    return bool(config.get('anti_multi') and config.get('device_check'))


def unpack(raw):
    """Decode the client blob. Never raises; returns ``(browser_id, trait)``.

    ``trait`` is a canonical string that is only ever passed through HMAC,
    never stored or logged; ``None`` when the device part is missing/unusable.
    """
    if not isinstance(raw, str) or not 0 < len(raw) <= MAX_BLOB or not re.fullmatch(r'[A-Za-z0-9_-]+', raw):
        return None, None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)).decode('utf-8'))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None, None
    if not isinstance(data, dict) or data.get('v') != 1:
        return None, None
    token = data.get('k')
    token = token if isinstance(token, str) and re.fullmatch(r'[0-9a-f]{64}', token) else None
    return token, canonical_trait(data.get('s'))


def canonical_trait(values):
    if not isinstance(values, dict):
        return None
    clean = {}
    for key, pattern in FIELDS.items():
        value = values.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            if not math.isfinite(value):
                continue
            value = format(value, 'g')
        if (isinstance(value, str) and len(value) <= LIMITS.get(key, 64)
                and re.fullmatch(pattern, value.strip())):
            clean[key] = value.strip()
    if len(clean) < MINIMUM or not set(clean).intersection(STRONG):
        return None
    return encode(clean)


def _mac(key, parts):
    return hmac.new(key, encode(parts).encode(), hashlib.sha256).hexdigest()


def _guard(conn):
    # Never let a lost key be silently replaced while device digests exist.
    if conn.execute('SELECT 1 FROM lottery_identity_key WHERE id=1').fetchone():
        return
    if conn.execute('SELECT 1 FROM lottery_device_entries LIMIT 1').fetchone():
        raise ValueError(UNAVAILABLE)


def admit(conn, config, consent_version, blob):
    """Inside the entry transaction and before any debit. ``None`` when not required.

    The blob is not even decoded without the current consent version.
    """
    if not required(config):
        if config.get('anti_multi'):
            _guard(conn)  # the shared key must not be regenerated under existing device digests
        return None
    if type(consent_version) is not int or consent_version != CONSENT_VERSION:
        raise ValueError(NEED_CONSENT)
    token, trait = unpack(blob)
    if trait is None:
        raise ValueError(FAILED)
    _guard(conn)
    return {'token': token, 'trait': None if apple_mobile(trait) else trait, 'version': consent_version}


def apple_mobile(trait):
    """iOS/iPadOS WebKit: masked "Apple GPU", touch screen and no deviceMemory (which every
    Chromium WebView reports). A same-model collision there proves nothing."""
    values = json.loads(trait)
    return values.get('i', '0') != '0' and 'h' not in values and values.get('b', 'Apple').startswith('Apple')


def browser(admitted, fallback):
    """The consented blob carries the installation identifier; else the legacy field."""
    return admitted['token'] if admitted and admitted['token'] else fallback


def record(conn, lottery_id, tg_id, admitted, now):
    """New entry only, same BEGIN IMMEDIATE transaction as the entry row."""
    if not admitted or not admitted['trait']:
        return
    key = lottery_identity._key(conn)
    conn.execute('INSERT INTO lottery_device_entries(lottery_id,tg_id,consent_version,consented_at,digest) '
                 'VALUES(?,?,?,?,?)',
                 (lottery_id, tg_id, admitted['version'], now,
                  _mac(key, ['lottery-identity-v1', lottery_id, REASON, admitted['trait']])))


def _rows(conn):
    return lambda sql, params=(): [dict(r) for r in conn.execute(sql, params).fetchall()]


def _group(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['digest']].append(row)
    return [(digest, members) for digest, members in grouped.items() if len(members) > 1]


def _shape(members, status):
    return {'reason': REASON, 'label': LABELS[REASON], 'tg_ids': [m['tg_id'] for m in members],
            'detected_at': max(m['consented_at'] for m in members), 'status': status,
            'status_label': STATUS[status], 'excluded': status == 'same'}


def _verdict(query, lottery_id):
    rows = query('SELECT tg_id,digest,consented_at FROM lottery_device_entries WHERE lottery_id=? '
                 'ORDER BY consented_at,tg_id', (lottery_id,))
    return [(digest, _shape(members, 'same')) for digest, members in _group(rows)]


def verdict(query, lottery_id):
    """Current decision: ``(groups, ids_to_exclude)``. ``query(sql, params) -> list[dict]``."""
    groups = [g for _, g in _verdict(query, lottery_id)]
    return groups, {tg for g in groups if g['excluded'] for tg in g['tg_ids']}


def frozen(query, lottery_id):
    """The decision persisted by the draw transaction."""
    rows = query("SELECT tg_id,digest,verdict,consented_at FROM lottery_device_entries "
                 "WHERE lottery_id=? AND verdict<>'' ORDER BY consented_at,tg_id", (lottery_id,))
    groups = [_shape(m, m[0]['verdict']) for _, m in _group(rows)]
    return groups, {tg for g in groups if g['excluded'] for tg in g['tg_ids']}


def settle(conn, lottery_id, config, now):
    """Inside the draw transaction, before candidates are read: freeze and merge into ``excluded``."""
    if not required(config):
        return set()
    decided = _verdict(_rows(conn), lottery_id)
    for digest, group in decided:
        conn.execute('UPDATE lottery_device_entries SET verdict=? WHERE lottery_id=? AND digest=?',
                     (group['status'], lottery_id, digest))
    conn.execute("UPDATE lottery_entries SET excluded=1 WHERE lottery_id=? AND tg_id IN "
                 "(SELECT tg_id FROM lottery_device_entries WHERE lottery_id=? AND verdict='same')",
                 (lottery_id, lottery_id))
    return {tg for _, g in decided if g['excluded'] for tg in g['tg_ids']}


def effective(db, row):
    """Administrator view: live verdict while entries are open, frozen afterwards."""
    if not required(row['config']):
        return [], set()
    if row['state'] == 'published':
        return verdict(db.query, row['id'])
    return frozen(db.query, row['id'])


def _hard(db, lottery_id):
    return {r['tg_id'] for r in db.query('SELECT tg_id FROM lottery_entries WHERE lottery_id=? AND excluded=1',
                                         (lottery_id,))}


def _account(db, lottery_id, tg_id, excluded):
    row = db.one('SELECT label FROM lottery_entries WHERE lottery_id=? AND tg_id=?', (lottery_id, tg_id)) or {}
    return {'tg_id': tg_id, 'label': row.get('label', ''), 'excluded': tg_id in excluded}


def present(db, row, data, tg_id, admin):
    """Detail response. Everybody gets the same consent text; only administrators get counts/rules."""
    if not admin:
        # Like anti_multi: a switched-off check is not advertised to participants.
        data['config'].pop('device_check', None)
    if not required(row['config']):
        return data
    data['consent'] = dict(CONSENT)
    if admin:
        _, ids = effective(db, row)
        excluded = _hard(db, row['id']) | ids
        data['qualification'] = {'excluded': bool(data['joined']) and str(tg_id) in excluded}
        data['excluded_count'] = len(excluded)
        data['candidate_count'] = data['count'] - len(excluded)
        data['admin_rules'] = data.get('admin_rules', '') + RULE
    return data


def annotate_records(db, row, result):
    """Administrator roster: merge device verdicts with the IP/browser clusters."""
    if not required(row['config']):
        return result
    groups, ids = effective(db, row)
    excluded = _hard(db, row['id']) | ids
    result['excluded'] = len(excluded)
    for entry in result['entries']:
        if entry['tg_id'] in ids and not entry['excluded']:
            entry.update(excluded=1, outcome='excluded')
    result['device_check'] = True
    result['clusters'] = _merge(db, row['id'], result['clusters'], groups, excluded)
    return result


def _merge(db, lottery_id, clusters, groups, excluded):
    accounts, links = {}, []
    for cluster in clusters:
        for account in cluster['accounts']:
            accounts[account['tg_id']] = {**account, 'excluded': account['excluded'] or account['tg_id'] in excluded}
        links += cluster['links']
    for group in groups:
        for tg in group['tg_ids']:
            accounts.setdefault(tg, _account(db, lottery_id, tg, excluded))
        links.append(group)
    parent = {tg: tg for tg in accounts}

    def root(tg):
        while parent[tg] != tg:
            parent[tg] = parent[parent[tg]]
            tg = parent[tg]
        return tg

    for link in links:
        for tg in link['tg_ids'][1:]:
            parent[root(tg)] = root(link['tg_ids'][0])
    components = {}
    for link in links:
        component = components.setdefault(root(link['tg_ids'][0]), {'links': [], 'ids': set(), 'reasons': set()})
        component['links'].append({**link, 'label': LABELS[link['reason']]})
        component['ids'].update(link['tg_ids'])
        component['reasons'].add(link['reason'])
    return [{'accounts': [accounts[tg] for tg in sorted(c['ids'])], 'reasons': sorted(c['reasons']),
             'labels': [LABELS[r] for r in sorted(c['reasons'])], 'links': c['links']}
            for c in components.values()]
