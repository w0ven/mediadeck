"""Escrow is NOT spendable points. All changes share the existing ledger transaction."""
from __future__ import annotations

import time


class PlayError(ValueError):
    """Controlled public error; no private balance or transport details."""


def integer(value, label='数量', low=1, high=2**63 - 1):
    if type(value) is not int or not low <= value <= high:
        raise PlayError(f'{label}须为{low}～{high}的整数')
    return value


class CashBook:
    def __init__(self, points):
        self.points = points

    @staticmethod
    def _tx(conn):
        if not conn.in_transaction:
            raise RuntimeError('cash movement requires the owning write transaction')

    def _event(self, conn, scope, ref, uid, op, amount, target='', now=None):
        self._tx(conn)
        conn.execute('INSERT INTO play_cash_events(scope,ref,user_id,operation,amount,target,created_at) VALUES(?,?,?,?,?,?,?)',
                     (scope, ref, uid, op, amount, target, int(time.time() if now is None else now)))

    def reserve(self, conn, scope, ref, uid, amount, *, now=None):
        self._tx(conn)
        integer(amount, '冻结积分')
        uid = str(uid)
        row = conn.execute('SELECT * FROM play_escrows WHERE scope=? AND ref=? AND user_id=?', (scope, ref, uid)).fetchone()
        if row:
            if row['reserved'] != amount:
                raise PlayError('原资金意图不一致')
            return False
        clock = int(time.time() if now is None else now)
        self.points._apply(conn, uid, -amount, 'play.reserve', f'{scope}:{ref}', 'play', clock)
        conn.execute('INSERT INTO play_escrows(scope,ref,user_id,amount,reserved,created_at) VALUES(?,?,?,?,?,?)',
                     (scope, ref, uid, amount, amount, clock))
        self._event(conn, scope, ref, uid, 'reserve', amount, now=clock)
        return True

    @staticmethod
    def held(conn, scope, ref, uid):
        row = conn.execute('SELECT amount FROM play_escrows WHERE scope=? AND ref=? AND user_id=?', (scope, ref, str(uid))).fetchone()
        return int(row['amount']) if row else 0

    def _take(self, conn, scope, ref, uid, amount, field):
        self._tx(conn)
        integer(amount)
        if field not in ('spent', 'released'):
            raise RuntimeError('invalid escrow movement')
        changed = conn.execute(f'UPDATE play_escrows SET amount=amount-?,{field}={field}+? WHERE scope=? AND ref=? AND user_id=? AND amount>=?',
                               (amount, amount, scope, ref, str(uid), amount))
        if changed.rowcount != 1:
            raise PlayError('冻结资金状态已变化')

    def release(self, conn, scope, ref, uid, amount=None, *, now=None):
        self._tx(conn)
        amount = self.held(conn, scope, ref, uid) if amount is None else integer(amount, low=0)
        if not amount:
            return 0
        self._take(conn, scope, ref, uid, amount, 'released')
        self.points._apply(conn, uid, amount, 'play.release', f'{scope}:{ref}', 'play', int(time.time() if now is None else now))
        self._event(conn, scope, ref, uid, 'release', amount, now=now)
        return amount

    def consume(self, conn, scope, ref, uid, amount, kind, fund_ref, *, now=None):
        self._tx(conn)
        if kind not in ('blackwhite', 'poker', 'issuance', 'fee'):
            raise RuntimeError('unknown nonspendable fund')
        self._take(conn, scope, ref, uid, amount, 'spent')
        conn.execute('INSERT INTO play_funds(kind,ref,amount,received) VALUES(?,?,?,?) ON CONFLICT(kind,ref) DO UPDATE SET amount=amount+excluded.amount,received=received+excluded.received',
                     (kind, fund_ref, amount, amount))
        self._event(conn, scope, ref, uid, 'consume', amount, f'{kind}:{fund_ref}', now)

    def payout(self, conn, kind, fund_ref, uid, amount, *, now=None):
        self._tx(conn)
        if kind not in ('blackwhite', 'poker'):
            raise RuntimeError('issuance/fee sinks cannot pay anyone')
        integer(amount)
        changed = conn.execute('UPDATE play_funds SET amount=amount-?,paid=paid+? WHERE kind=? AND ref=? AND amount>=?',
                               (amount, amount, kind, fund_ref, amount))
        if changed.rowcount != 1:
            raise PlayError('奖池状态已变化')
        self.points._apply(conn, uid, amount, 'play.payout', f'{kind}:{fund_ref}', 'play', int(time.time() if now is None else now))
        self._event(conn, kind, fund_ref, str(uid), 'payout', amount, now=now)
