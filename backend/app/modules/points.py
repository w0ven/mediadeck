"""Points: an append-only ledger, and a balance derived from it.

The balance is **not** a column anyone can set. Every change is a row saying
how much, why, and who did it, and the balance is the sum of those rows. That
ordering matters: a stored balance and a ledger are two sources of truth that
will eventually disagree, and when they do there is no way to tell which one
lied. Deriving the number means a wrong balance is always a wrong row, and a
wrong row can be found and reversed.

``balance_after`` is still written on each row, but only as a witness: it is
what the balance was believed to be at that moment, so a later audit can point
at the exact row where reality diverged. Nothing reads it to answer "how many
points does this member have".

Points can never go negative. A member who cannot afford something is refused
before anything is written, rather than being allowed to go into debt that the
panel would then have to model, display and collect.

Transfers are one transaction: the debit and the credit are written under a
single lock, and both rows carry a reference to the other. A transfer that
took from one side without giving to the other is the failure that destroys
trust in the whole feature, so it is made structurally impossible rather than
merely unlikely.
"""
from __future__ import annotations

import contextlib
import sqlite3
import time
from typing import Any

# Ledger reasons used by the panel itself. Plugins may write their own, but
# these are the ones the UI knows how to label.
REASON_LABELS = {
    "checkin": "每日签到",
    "transfer.out": "转账支出",
    "transfer.in": "转账收入",
    "shop.redeem": "商城兑换",
    "shop.refund": "兑换回滚",
    "admin.adjust": "管理员调整",
}


def reason_label(reason: str) -> str:
    return REASON_LABELS.get(str(reason or ""), str(reason or "其他"))


class PointsService:
    """The ledger, and the only way to change a balance."""

    def __init__(self, db: Any) -> None:
        self._db = db

    # -- read ----------------------------------------------------------------

    def balance(self, user_id: str) -> int:
        row = self._db.one(
            "SELECT COALESCE(SUM(delta),0) AS total FROM points_ledger "
            "WHERE emby_user_id=?", (str(user_id),))
        return int((row or {}).get("total") or 0)

    def balances(self, user_ids: list[str] | None = None) -> dict[str, int]:
        """Every balance in one query.

        The member list renders hundreds of rows; asking per row is how a page
        that used to be instant starts taking seconds.
        """
        rows = self._db.query(
            "SELECT emby_user_id, COALESCE(SUM(delta),0) AS total "
            "FROM points_ledger GROUP BY emby_user_id")
        out = {str(r["emby_user_id"]): int(r["total"] or 0) for r in rows}
        if user_ids is None:
            return out
        return {uid: out.get(uid, 0) for uid in user_ids}

    def ledger(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM points_ledger WHERE emby_user_id=? "
            "ORDER BY id DESC LIMIT ?",
            (str(user_id), max(1, min(int(limit or 20), 500))))
        for row in rows:
            row["reason_label"] = reason_label(row.get("reason", ""))
        return rows

    def top(self, limit: int = 20) -> list[dict[str, Any]]:
        """Balance ranking, richest first.

        Members with no ledger rows are absent rather than shown at zero: a
        leaderboard of people who have never earned anything is noise.
        """
        return self._db.query(
            "SELECT l.emby_user_id AS emby_user_id, "
            "       COALESCE(m.username,'') AS username, "
            "       SUM(l.delta) AS balance "
            "FROM points_ledger l "
            "LEFT JOIN members m ON m.emby_user_id = l.emby_user_id "
            "GROUP BY l.emby_user_id "
            "HAVING SUM(l.delta) <> 0 "
            "ORDER BY balance DESC, username COLLATE NOCASE ASC LIMIT ?",
            (max(1, min(int(limit or 20), 200)),))

    def spent_since(self, user_id: str, reason: str, since: int) -> int:
        """How much has left this account for one reason since a timestamp.

        Used for daily transfer caps. Returns a positive magnitude, because
        "you have sent 300 today" reads better than "-300".
        """
        row = self._db.one(
            "SELECT COALESCE(SUM(-delta),0) AS total FROM points_ledger "
            "WHERE emby_user_id=? AND reason=? AND created_at>=? AND delta<0",
            (str(user_id), str(reason), int(since)))
        return int((row or {}).get("total") or 0)

    # -- write ---------------------------------------------------------------

    def _apply(self, conn: sqlite3.Connection, user_id: str, delta: int,
               reason: str, ref: str, actor: str, now: int) -> int:
        """One ledger row on an already-open transaction. Returns new balance.

        Private because it takes a connection: callers that need two writes to
        be one transaction (a transfer) must own the transaction themselves,
        and callers that need only one go through ``add``.
        """
        user_id = str(user_id)
        delta = int(delta)
        cur = conn.execute(
            "SELECT COALESCE(SUM(delta),0) AS total FROM points_ledger "
            "WHERE emby_user_id=?", (user_id,))
        current = int((cur.fetchone() or {"total": 0})["total"] or 0)
        after = current + delta
        if after < 0:
            raise ValueError("积分不足")
        conn.execute(
            "INSERT INTO points_ledger"
            "(emby_user_id,delta,balance_after,reason,ref,actor,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (user_id, delta, after, str(reason)[:60], str(ref)[:120],
             str(actor)[:60], now))
        return after

    def add(self, user_id: str, delta: int, reason: str, ref: str = "",
            actor: str = "system") -> int:
        """Move a balance by ``delta``. Returns the new balance.

        A zero delta is refused rather than silently ignored: it is always a
        bug at the call site, and a ledger full of no-op rows makes the real
        history harder to read.
        """
        delta = int(delta)
        if delta == 0:
            raise ValueError("积分变动不能为 0")
        if not str(user_id or "").strip():
            raise ValueError("缺少用户")
        with self._db.write() as conn:
            return self._apply(conn, user_id, delta, reason, ref, actor,
                               int(time.time()))

    def transfer(self, from_id: str, to_id: str, amount: int,
                 actor: str = "member", fee: int = 0, *,
                 conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Move points between two members, atomically.

        ``fee`` is destroyed rather than paid to anyone: it exists to make
        transferring cost something, and inventing a treasury account to hold
        it would only raise the question of who may spend it.
        """
        from_id, to_id = str(from_id or ""), str(to_id or "")
        amount = int(amount)
        if amount <= 0:
            raise ValueError("转账数量必须大于 0")
        if not from_id or not to_id:
            raise ValueError("缺少转账双方")
        if from_id == to_id:
            raise ValueError("不能转给自己")
        fee = max(0, min(int(fee), amount))
        received = amount - fee
        if received <= 0:
            raise ValueError("手续费过高，对方将收不到积分")

        now = int(time.time())
        # One transaction for both halves: a debit that lands without its
        # credit is the failure this whole method exists to prevent.
        with (contextlib.nullcontext(conn) if conn is not None else self._db.write()) as tx:
            out_balance = self._apply(
                tx, from_id, -amount, "transfer.out", f"to:{to_id}",
                actor, now)
            in_balance = self._apply(
                tx, to_id, received, "transfer.in", f"from:{from_id}",
                actor, now)
        return {
            "ok": True,
            "amount": amount,
            "fee": fee,
            "received": received,
            "from_balance": out_balance,
            "to_balance": in_balance,
        }
