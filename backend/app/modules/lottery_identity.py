"""Lottery-only association signals. No raw IP/device value is persisted or logged.

cloudflare_protected is an operator assertion about the entire ingress boundary,
NOT a header claiming that a request came from Cloudflare. It must only be enabled
on a server reachable exclusively through a trusted, header-overwriting CF edge.
Uvicorn may already have rewritten scope.client; it is deliberately not used to
infer the transport peer here. All other modes fail closed.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
from collections import defaultdict

from app.modules.economy_rules import encode

RULE = ('严格防小号已开启：本期相同可信公网 IP 或相同浏览器安装标识关联的所有报名账号'
        '均标记异常、排除中奖，包括先报名者。共享网络也可能被排除。'
        '浏览器标识不是硬件 ID；不同 WebView、清缓存、换浏览器/设备/IP 可能绕过检测，'
        '不能保证识别同一个人。只影响本期抽奖，不限制观影；发布后不能关闭此规则。')


def public_ip(raw):
    """A single canonical, globally routable address (not a chain/port/zone)."""
    if not isinstance(raw, str) or not raw or len(raw) > 64 or raw != raw.strip() or '%' in raw:
        return None
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if not address.is_global or address.is_multicast or address.is_reserved or address.is_unspecified:
        return None
    return str(address)


def trusted_ip(request, mode):
    # No request.client loopback heuristics, XFF, Forwarded, X-Real-IP or UA.
    # Do not add a runtime/client-selectable switch for this trust assertion.
    if mode != 'cloudflare_protected':
        return None
    values = request.headers.getlist('cf-connecting-ip')
    return public_ip(values[0]) if len(values) == 1 else None


def _key(conn):
    row = conn.execute('SELECT secret FROM lottery_identity_key WHERE id=1').fetchone()
    if not row:
        if conn.execute('SELECT 1 FROM lottery_signals LIMIT 1').fetchone():
            raise ValueError('报名暂不可用，请联系管理员检查活动校验服务')
        value = secrets.token_hex(32)
        conn.execute('INSERT INTO lottery_identity_key(id,secret) VALUES(1,?)', (value,))
    else:
        value = row['secret']
    if not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('报名暂不可用，请联系管理员检查活动校验服务')
    return bytes.fromhex(value)


def record(conn, lottery_id, tg_id, source_ip, browser_id, now):
    """Called ONLY for a new entry, inside its BEGIN IMMEDIATE transaction.

    Repeated TG requests never replace signals or change qualification. A second
    TG keeps its entry, and both sides of every detected association are excluded.
    """
    address = public_ip(source_ip)
    if not address:
        raise ValueError('报名暂不可用，请稍后重试或联系管理员')
    if not isinstance(browser_id, str) or not re.fullmatch(r'[0-9a-f]{64}', browser_id):
        raise ValueError('报名暂不可用，请允许本地存储并重新打开活动')
    key = _key(conn)
    for kind, value in (('ip', address), ('device', browser_id)):
        digest = hmac.new(key, encode(['lottery-identity-v1', lottery_id, kind, value]).encode(), hashlib.sha256).hexdigest()
        conn.execute('INSERT INTO lottery_signals(lottery_id,tg_id,kind,digest,created_at) VALUES(?,?,?,?,?)',
                     (lottery_id, tg_id, kind, digest, now))
        matches = conn.execute('SELECT COUNT(*) FROM lottery_signals WHERE lottery_id=? AND kind=? AND digest=?',
                               (lottery_id, kind, digest)).fetchone()[0]
        if matches > 1:
            conn.execute('UPDATE lottery_entries SET excluded=1 WHERE lottery_id=? AND tg_id IN '
                         '(SELECT tg_id FROM lottery_signals WHERE lottery_id=? AND kind=? AND digest=?)',
                         (lottery_id, lottery_id, kind, digest))


def clusters(db, lottery_id):
    """Admin-only connected components, including each direct association reason.

    Return account labels/IDs and explanation edges, not IPs, browser IDs,
    digests or the key. Cross-activity associations are intentionally impossible.
    """
    signals = db.query('SELECT s.tg_id,s.kind,s.digest,s.created_at,e.label,e.excluded '
                       'FROM lottery_signals s JOIN lottery_entries e '
                       'ON e.lottery_id=s.lottery_id AND e.tg_id=s.tg_id WHERE s.lottery_id=? '
                       'ORDER BY s.created_at,s.tg_id,s.kind', (lottery_id,))
    grouped, accounts, parent = defaultdict(list), {}, {}
    for row in signals:
        grouped[(row['kind'], row['digest'])].append(row)
        accounts[row['tg_id']] = {'tg_id': row['tg_id'], 'label': row['label'], 'excluded': bool(row['excluded'])}
        parent[row['tg_id']] = row['tg_id']

    def root(tg):
        while parent[tg] != tg:
            parent[tg] = parent[parent[tg]]
            tg = parent[tg]
        return tg

    edges = []
    for (kind, _digest), group in grouped.items():
        if len(group) < 2:
            continue
        ids = [r['tg_id'] for r in group]
        for tg in ids[1:]:
            parent[root(tg)] = root(ids[0])
        edges.append({'reason': kind, 'tg_ids': ids, 'detected_at': max(r['created_at'] for r in group)})
    components = {}
    for edge in edges:
        cid = root(edge['tg_ids'][0])
        component = components.setdefault(cid, {'links': [], 'ids': set(), 'reasons': set()})
        component['links'].append(edge)
        component['ids'].update(edge['tg_ids'])
        component['reasons'].add(edge['reason'])
    return [{'accounts': [accounts[tg] for tg in sorted(c['ids'])], 'reasons': sorted(c['reasons']),
             'links': c['links']} for c in components.values()]
