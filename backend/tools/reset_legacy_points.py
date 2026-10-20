"""Offline, explicitly confirmed ledger reset that preserves post-cutoff earned funds.

No app/settings imports, no schema migration, no network. Default is read-only.
Run only in an authorized maintenance window after a consistent database backup.
Ambiguous transfers/games/refunds and any unsettled funds fail closed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from contextlib import ExitStack, closing
from pathlib import Path

SCOPE = 'points.legacy_reset'
REASON = 'admin.reset_legacy'
# Only directly earned, independently attributable credits are accepted.
CREDITS = {'checkin', 'watch.reward'}
DEBITS = {'shop.redeem', 'activity.renew'}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def connect(path, *, writable=False):
    path = Path(path).resolve(strict=True)
    conn = sqlite3.connect(path.as_uri()+('?mode=rw' if writable else '?mode=ro'), uri=True,
                           isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def prior_result(conn, cutoff, batch):
    if type(cutoff) is not int or cutoff <= 0 or not isinstance(batch, str) or not 1 <= len(batch) <= 100:
        raise ValueError('invalid cutoff or batch')
    rows = conn.execute('SELECT request_id,request_json,result_json FROM economy_receipts WHERE scope=?', (SCOPE,)).fetchall()
    for row in rows:
        if row['request_id'] == batch:
            if json.loads(row['request_json']) != {'cutoff':cutoff}:
                raise ValueError('batch already used for a different cutoff')
            return json.loads(row['result_json'])
    if rows:
        raise ValueError('a different reset batch already exists; do not reset again')
    return None


def unsettled(conn):
    # Missing financial tables are an incompatible database, not evidence of zero.
    checks = {
        'play_escrows': 'amount<>0', 'play_funds': 'amount<>0',
        'play_rounds': "state NOT IN ('settled','cancelled','expired')",
        'niuniu_rounds': "state NOT IN ('settled','cancelled','expired')",
        'dice_rounds': "state NOT IN ('settled','refunded','empty')",
        'red_packets': "status NOT IN ('settled','cancelled','exhausted','expired') OR remaining<>0",
    }
    return {table:count for table, where in checks.items()
            if (count := conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {where}').fetchone()[0])}


def preview(conn, cutoff, batch):
    cached = prior_result(conn, cutoff, batch)
    if cached is not None:
        return cached
    blocked = unsettled(conn)
    if blocked:
        raise ValueError('unsettled funds/rounds: '+encode(blocked))
    members = {r[0] for r in conn.execute('SELECT emby_user_id FROM members')}
    ledger = [dict(r) for r in conn.execute('SELECT * FROM points_ledger ORDER BY created_at,id')]
    users = members | {r['emby_user_id'] for r in ledger}
    buckets = {uid:{'user_id':uid, 'member_exists':uid in members, 'old_at_cutoff':0,
                    'today_earned':0, 'old_spent':0, 'today_spent':0, 'current':0} for uid in users}
    for row in ledger:
        if type(row['delta']) is not int or type(row['created_at']) is not int:
            raise ValueError(f"invalid integer ledger fields at row {row['id']}")
        bucket = buckets[row['emby_user_id']]
        if row['created_at'] < cutoff:
            bucket['old_at_cutoff'] += row['delta']
        bucket['current'] += row['delta']
    for bucket in buckets.values():
        if bucket['old_at_cutoff'] < 0 or bucket['current'] < 0:
            raise ValueError('negative balance requires manual review; reset never creates or forgives debt')
        bucket['clear'] = bucket['old_at_cutoff']
        bucket['retain'] = 0
    for row in ledger:
        if row['created_at'] < cutoff:
            continue
        delta, reason = row['delta'], row['reason']
        bucket = buckets[row['emby_user_id']]
        if delta > 0 and reason in CREDITS:
            bucket['retain'] += delta
            bucket['today_earned'] += delta
        elif delta < 0 and reason in DEBITS:
            spend = -delta
            old_spend = min(bucket['clear'], spend)
            today_spend = spend-old_spend
            if today_spend > bucket['retain']:
                raise ValueError(f"chronological funding deficit at row {row['id']}")
            bucket['clear'] -= old_spend
            bucket['retain'] -= today_spend
            bucket['old_spent'] += old_spend
            bucket['today_spent'] += today_spend
        else:
            raise ValueError(f"ambiguous post-cutoff source at row {row['id']}: {reason}; manual review required")
    rows = [buckets[uid] for uid in sorted(buckets)]
    for row in rows:
        if not 0 <= row['retain'] <= row['current'] or row['clear']+row['retain'] != row['current']:
            raise ValueError('bucket invariant failed')
    result = {'batch':batch, 'cutoff':cutoff, 'users':rows,
              'summary':{'all_users':len(rows), 'clear_users':sum(r['clear']>0 for r in rows),
                         'retain_users':sum(r['retain']>0 for r in rows),
                         'current':sum(r['current'] for r in rows), 'clear':sum(r['clear'] for r in rows),
                         'retain':sum(r['retain'] for r in rows)},
              'ledger_rows':len(ledger), 'ledger_max_id':max((r['id'] for r in ledger), default=0),
              'ledger_hash':hashlib.sha256(encode(ledger).encode()).hexdigest()}
    result['hash'] = hashlib.sha256(encode(result).encode()).hexdigest()
    return result


def apply(conn, cutoff, batch, expected_hash, *, now=None):
    if conn.in_transaction:
        raise ValueError('reset owns its transaction')
    conn.execute('BEGIN IMMEDIATE')
    try:
        prior = prior_result(conn, cutoff, batch)
        if prior is not None:
            if prior['hash'] != expected_hash:
                raise ValueError('batch hash mismatch')
            conn.commit()
            return prior
        report = preview(conn, cutoff, batch)
        if report['hash'] != expected_hash:
            raise ValueError('ledger/membership changed since preview; refresh dry-run')
        clock = int(time.time() if now is None else now)
        if clock < cutoff:
            raise ValueError('cutoff cannot be in the future')
        for row in report['users']:
            if row['clear']:
                conn.execute('INSERT INTO points_ledger(emby_user_id,delta,balance_after,reason,ref,actor,created_at) '
                             'VALUES(?,?,?,?,?,?,?)',
                             (row['user_id'], -row['clear'], row['retain'], REASON, batch, 'maintenance', clock))
        conn.execute('INSERT INTO economy_receipts(scope,request_id,user_id,request_json,result_json) VALUES(?,?,?,?,?)',
                     (SCOPE, batch, '*', encode({'cutoff':cutoff}), encode(report)))
        conn.commit()
        return report
    except BaseException:
        conn.rollback()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--cutoff', required=True, type=int, help='Unix seconds; inclusive earned-fund boundary')
    parser.add_argument('--batch', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm-hash', help='Exact hash from reviewed dry-run')
    parser.add_argument('--output', help='Private detailed audit JSON, created exclusively with mode 600')
    args = parser.parse_args()
    if args.apply and not args.confirm_hash:
        parser.error('--apply requires --confirm-hash')
    # Reserve report destination before financial mutation; never overwrite an audit.
    with ExitStack() as stack:
        output = stack.enter_context(open(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w')) if args.output else None
        conn = stack.enter_context(closing(connect(args.database, writable=args.apply)))
        if args.apply:
            result = apply(conn, args.cutoff, args.batch, args.confirm_hash)
        else:
            conn.execute('BEGIN')
            result = preview(conn, args.cutoff, args.batch)
            conn.rollback()
        if output:
            output.write(encode(result)+'\n')
            output.flush()
            os.fsync(output.fileno())
        print(encode({'mode':'apply' if args.apply else 'dry-run', 'batch':result['batch'],
                      'hash':result['hash'], 'summary':result['summary']}))


if __name__ == '__main__':
    main()
