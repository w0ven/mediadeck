"""Verified Telegram reassignment, independent of registration and retired claims."""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from app.core.db import Database


class RebindingService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def allow_attempt(self, tg_id: str, now: int | None = None) -> bool:
        now = int(time.time()) if now is None else now
        with self.db.write() as conn:
            row = conn.execute(
                "SELECT window_at,attempts FROM tg_rebind_attempts WHERE tg_user_id=?", (tg_id,)
            ).fetchone()
            start, count = (int(row[0]), int(row[1])) if row else (now, 0)
            if now - start >= 900:
                start, count = now, 0
            if count >= 5:
                return False
            conn.execute(
                "INSERT INTO tg_rebind_attempts VALUES(?,?,?) ON CONFLICT(tg_user_id) DO UPDATE SET window_at=excluded.window_at,attempts=excluded.attempts",
                (tg_id, start, count + 1),
            )
        return True

    def handoff(self, old_tg: str, new_tg: str) -> str:
        if not new_tg.isascii() or not new_tg.isdigit() or not 0 < int(new_tg) < 2**63 or str(int(new_tg)) == old_tg:
            raise ValueError('请输入不同于当前账号的新 Telegram 数字 ID。')
        new_tg = str(int(new_tg))
        now = int(time.time())
        token = secrets.token_urlsafe(24)
        with self.db.write() as conn:
            member = conn.execute('SELECT emby_user_id FROM members WHERE tg_user_id=? AND emby_missing_since IS NULL', (old_tg,)).fetchone()
            if not member or conn.execute('SELECT 1 FROM members WHERE tg_user_id=?', (new_tg,)).fetchone():
                raise ValueError('原账号不可用，或新 Telegram 已绑定其他账号。')
            conn.execute('UPDATE tg_rebind_handoffs SET expires_at=? WHERE emby_user_id=?', (now, member[0]))
            conn.execute('INSERT INTO tg_rebind_handoffs VALUES(?,?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), member[0], old_tg, new_tg, now + 1800))
        return token

    @staticmethod
    def _handoff(conn: Any, token: str, tg_id: str, now: int) -> dict:
        row = conn.execute('SELECT * FROM tg_rebind_handoffs WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row['expires_at'] <= now or row['new_tg_user_id'] != tg_id:
            raise ValueError('换绑链接无效、已过期，或不是发给当前 Telegram 的。')
        r = dict(row)
        member = conn.execute('SELECT tg_user_id FROM members WHERE emby_user_id=?', (r['emby_user_id'],)).fetchone()
        if not member or member[0] != r['old_tg_user_id']:
            raise ValueError('原绑定已变化，请重新发起换绑。')
        return r

    def open_handoff(self, token: str, tg_id: str) -> dict:
        with self.db.write() as conn:
            return self._handoff(conn, token, tg_id, int(time.time()))

    def create(self, user_id: str, tg_id: str, tg_name: str, *, handoff: str = '') -> dict[str, Any]:
        now = int(time.time())
        with self.db.write() as conn:
            if handoff:
                intent = self._handoff(conn, handoff, tg_id, now)
                if intent['emby_user_id'] != user_id:
                    raise ValueError('验证的账号与换绑链接不一致。')
                conn.execute('UPDATE tg_rebind_handoffs SET expires_at=? WHERE token_hash=?', (now, intent['token_hash']))
            # Verification proves an existing Emby ID, never a typed username.
            target = conn.execute(
                "SELECT username,tg_user_id,emby_missing_since FROM members WHERE emby_user_id=?",
                (user_id,),
            ).fetchone()
            if not target or not target[1] or target[2]:
                raise ValueError("仅支持现存且已绑定其他 Telegram 的账号换绑。")
            if conn.execute("SELECT 1 FROM members WHERE tg_user_id=?", (tg_id,)).fetchone():
                raise ValueError("这个 Telegram 已有账号，不能覆盖现有绑定。")
            conn.execute(
                "UPDATE tg_requests SET status='expired',reviewed_at=?,note='申请已过期' WHERE kind='rebind' AND status='pending' AND expires_at<=?",
                (now, now),
            )
            if conn.execute(
                "SELECT 1 FROM tg_requests WHERE kind='rebind' AND status='pending' AND (tg_user_id=? OR emby_user_id=?)",
                (tg_id, user_id),
            ).fetchone():
                raise ValueError("该账号或 Telegram 已有待处理的换绑申请。")
            cur = conn.execute(
                "INSERT INTO tg_requests(kind,tg_user_id,tg_username,wanted_username,status,created_at,emby_user_id,old_tg_user_id,verified_at,expires_at) VALUES('rebind',?,?,?,'pending',?,?,?,?,?)",
                (tg_id, tg_name, target[0], now, user_id, target[1], now, now + 86400),
            )
            request_id = int(cur.lastrowid)
        return self.get(request_id)

    def get(self, request_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM tg_requests WHERE id=?", (request_id,))
        if not row or row.get("kind") != "rebind" or not row.get("verified_at"):
            raise ValueError("旧认领入口已停用；这不是已验证的换绑申请。")
        return row

    def review(self, request_id: int, approve: bool, reviewer: str) -> dict[str, Any]:
        """Caller has checked live Emby presence and the current reviewer role."""
        now = int(time.time())
        with self.db.write() as conn:
            row = conn.execute("SELECT * FROM tg_requests WHERE id=?", (request_id,)).fetchone()
            if not row:
                raise KeyError(request_id)
            r = dict(row)
            if r["kind"] != "rebind" or not r.get("verified_at") or not r.get("emby_user_id"):
                raise ValueError("认领已停用，不能通过未验证的旧申请。")
            if r["status"] != "pending":
                return {**r, "changed": False, "approved": r["status"] == "approved"}
            if now >= int(r.get("expires_at") or 0):
                status, note = "expired", "申请已过期，请重新验证"
            elif not approve:
                status, note = "rejected", "管理员拒绝申请"
            else:
                target = conn.execute(
                    "SELECT tg_user_id,emby_missing_since FROM members WHERE emby_user_id=?",
                    (r["emby_user_id"],),
                ).fetchone()
                occupied = conn.execute(
                    "SELECT 1 FROM members WHERE tg_user_id=?", (r["tg_user_id"],)
                ).fetchone()
                if not target or target[1] or target[0] != r["old_tg_user_id"] or occupied:
                    status, note = "conflict", "账号或绑定已变化，请重新验证"
                else:
                    conn.execute(
                        "UPDATE members SET tg_user_id=?,tg_username=?,tg_bound_at=?,updated_at=? WHERE emby_user_id=? AND tg_user_id=?",
                        (
                            r["tg_user_id"],
                            r["tg_username"],
                            now,
                            now,
                            r["emby_user_id"],
                            r["old_tg_user_id"],
                        ),
                    )
                    status, note = "approved", "仅更换 Telegram 绑定，账号权益与历史保留"
            conn.execute(
                "UPDATE tg_requests SET status=?,reviewed_at=?,reviewed_by=?,note=? WHERE id=? AND status='pending'",
                (status, now, reviewer, note, request_id),
            )
            conn.execute(
                "INSERT INTO audit_log(ts,actor,action,subject,detail,ok) VALUES(?,?,?,?,?,?)",
                (
                    now,
                    reviewer,
                    "telegram.rebind." + status,
                    r["emby_user_id"],
                    "request=" + str(request_id),
                    int(status == "approved"),
                ),
            )
        return {
            **r,
            "status": status,
            "note": note,
            "changed": True,
            "approved": status == "approved",
        }
