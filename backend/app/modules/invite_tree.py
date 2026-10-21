"""Invite accountability: who brought whom in, and what happens upstream.

A deleted member leaves a departure row so their inviter still sees them in
the tree. When an administrator or a violation rule deletes someone, the
direct inviter becomes a case that an administrator settles once: delete the
inviter, pause their invitations, or leave them alone. Only one level up;
settling a case never opens another.
"""
from __future__ import annotations

import time
from typing import Any

from app.modules import viewing

DAY = 86400
SUSPEND_DAYS = 30
REVIEW_SOURCES = ("admin", "violation")
CASE_ACTIONS = ("delete", "suspend", "ignore")
SETTLED = ("deleted", "suspended", "ignored", "gone")

SCHEMA = """
CREATE TABLE IF NOT EXISTS member_departures (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    emby_user_id    TEXT NOT NULL,
    username        TEXT NOT NULL DEFAULT '',
    inviter_id      TEXT NOT NULL DEFAULT '',
    tg_user_id      TEXT NOT NULL DEFAULT '',
    tg_username     TEXT NOT NULL DEFAULT '',
    tg_display_name TEXT NOT NULL DEFAULT '',
    registered_at   INTEGER,
    expires_at      INTEGER,
    deleted_at      INTEGER NOT NULL,
    source          TEXT NOT NULL DEFAULT '',
    actor           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_departures_inviter ON member_departures(inviter_id);
CREATE INDEX IF NOT EXISTS idx_departures_user ON member_departures(emby_user_id);
CREATE TABLE IF NOT EXISTS invite_cases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    departure_id    INTEGER NOT NULL UNIQUE,
    deleted_user_id TEXT NOT NULL,
    inviter_id      TEXT NOT NULL,
    source          TEXT NOT NULL,
    actor           TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    decided_by      TEXT NOT NULL DEFAULT '',
    decided_at      INTEGER,
    result          TEXT NOT NULL DEFAULT '',
    chat_id         TEXT NOT NULL DEFAULT '',
    thread_id       INTEGER,
    message_id      INTEGER,
    delivery        TEXT NOT NULL DEFAULT 'pending',
    delivery_error  TEXT NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invite_cases_status ON invite_cases(status);
CREATE INDEX IF NOT EXISTS idx_invite_cases_inviter ON invite_cases(inviter_id);
CREATE TABLE IF NOT EXISTS invite_suspensions (
    user_id    TEXT PRIMARY KEY,
    until      INTEGER NOT NULL,
    case_id    INTEGER,
    actor      TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
"""

SOURCE_LABELS = {"admin": "管理员删号", "violation": "违规规则自动删号"}
STATUS_LABELS = {"active": "正常", "expired": "已到期", "suspended": "已停用",
                 "exhausted": "已停用", "pending": "待开通", "deleted": "已删除"}


def migrate_invites(db: Any) -> None:
    db._conn.executescript(SCHEMA)


def record_departure(conn: Any, row: dict[str, Any], *, source: str, actor: str,
                     now: int | None = None) -> int:
    """Called with the raw members row, before it is removed."""
    now = int(time.time()) if now is None else int(now)
    cursor = conn.execute(
        "INSERT INTO member_departures(emby_user_id,username,inviter_id,tg_user_id,tg_username,"
        "tg_display_name,registered_at,expires_at,deleted_at,source,actor) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (str(row["emby_user_id"]), str(row.get("username") or ""), str(row.get("inviter_id") or ""),
         str(row.get("tg_user_id") or ""), str(row.get("tg_username") or ""),
         str(row.get("tg_display_name") or ""), row.get("register_at") or row.get("created_at"),
         row.get("expires_at"), now, str(source or ""), str(actor or "")))
    return int(cursor.lastrowid)


def active_suspension(db: Any, user_id: str, now: float | None = None) -> dict[str, Any] | None:
    if not user_id:
        return None
    now = time.time() if now is None else now
    return db.one("SELECT * FROM invite_suspensions WHERE user_id=? AND until>?", (str(user_id), int(now)))


def tg_label(row: dict[str, Any]) -> str:
    """Telegram identity only; the Emby account name stays admin-only."""
    name = str(row.get("tg_display_name") or "").strip()
    if name:
        return name
    handle = str(row.get("tg_username") or "").strip().lstrip("@")
    if handle:
        return "@" + handle
    return "Telegram用户" if str(row.get("tg_user_id") or "").strip() else "未绑定 TG"


class InviteTree:
    def __init__(self, db: Any, members: Any):
        self.db = db
        self.members = members

    # -- tree ------------------------------------------------------------------

    def active_7d(self, user_id: str, now: float) -> bool | None:
        try:
            rows = viewing.records(self.db, user_id, now - 7 * DAY, now)
            return viewing.window(rows, now - 7 * DAY, now)["seconds"] > 0
        except Exception:  # noqa: BLE001 - unknown activity is shown as unknown, not inactive
            return None

    def _live_node(self, member: dict[str, Any], now: float) -> dict[str, Any]:
        return {
            "emby_user_id": str(member["emby_user_id"]),
            "username": str(member.get("username") or ""),
            "label": tg_label(member),
            "tg_user_id": str(member.get("tg_user_id") or ""),
            "status": str(member.get("state") or member.get("status") or ""),
            "registered_at": member.get("register_at") or member.get("created_at"),
            "expires_at": member.get("expires_at_effective", member.get("expires_at")),
            "deleted_at": None,
            "active_7d": self.active_7d(str(member["emby_user_id"]), now),
        }

    @staticmethod
    def _departed_node(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "emby_user_id": str(row["emby_user_id"]),
            "username": str(row.get("username") or ""),
            "label": tg_label(row),
            "tg_user_id": str(row.get("tg_user_id") or ""),
            "status": "deleted",
            "registered_at": row.get("registered_at"),
            "expires_at": row.get("expires_at"),
            "deleted_at": row.get("deleted_at"),
            "deleted_source": str(row.get("source") or ""),
            "active_7d": False,
        }

    def children(self, user_id: str, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        nodes = []
        for row in self.db.query("SELECT emby_user_id FROM members WHERE inviter_id=?", (str(user_id),)):
            member = self.members.get(str(row["emby_user_id"]))
            if member:
                nodes.append(self._live_node(member, now))
        live = {node["emby_user_id"] for node in nodes}
        for row in self.db.query(
                "SELECT * FROM member_departures WHERE inviter_id=? ORDER BY deleted_at DESC", (str(user_id),)):
            if row["emby_user_id"] not in live:
                live.add(row["emby_user_id"])
                nodes.append(self._departed_node(row))
        order = {"active": 0, "pending": 1, "expired": 2, "suspended": 3, "exhausted": 3, "deleted": 4}
        nodes.sort(key=lambda n: (order.get(n["status"], 3), -(n["registered_at"] or 0)))
        return nodes

    def downstream(self, user_id: str, depth: int = 2, now: float | None = None,
                   _seen: set[str] | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        seen = _seen if _seen is not None else {str(user_id)}
        nodes = []
        for node in self.children(user_id, now):
            if node["emby_user_id"] in seen:
                continue  # a corrupted cycle must not recurse forever
            seen.add(node["emby_user_id"])
            node["children"] = (self.downstream(node["emby_user_id"], depth - 1, now, seen)
                                if depth > 1 else [])
            node["descendants"] = self.count_descendants(node["emby_user_id"]) if depth <= 1 else (
                len(node["children"]) + sum(child.get("descendants", 0) for child in node["children"]))
            nodes.append(node)
        return nodes

    def count_descendants(self, user_id: str, limit: int = 1000) -> int:
        seen, frontier = {str(user_id)}, [str(user_id)]
        while frontier and len(seen) <= limit:
            marks = ",".join("?" * len(frontier))
            rows = self.db.query(
                f"SELECT emby_user_id FROM members WHERE inviter_id IN ({marks}) "
                f"UNION SELECT emby_user_id FROM member_departures WHERE inviter_id IN ({marks})",
                tuple(frontier) * 2)
            frontier = [r["emby_user_id"] for r in rows if r["emby_user_id"] not in seen]
            seen.update(frontier)
        return len(seen) - 1

    def upstream(self, user_id: str, limit: int = 12) -> list[dict[str, Any]]:
        chain, seen, now = [], {str(user_id)}, time.time()
        current = self._inviter_id(str(user_id))
        while current and current not in seen and len(chain) < limit:
            seen.add(current)
            member = self.members.get(current)
            if member:
                chain.append(self._live_node(member, now))
            else:
                row = self.db.one("SELECT * FROM member_departures WHERE emby_user_id=? "
                                  "ORDER BY deleted_at DESC LIMIT 1", (current,))
                if not row:
                    chain.append({"emby_user_id": current, "username": "", "label": "未知账号",
                                  "status": "deleted", "registered_at": None, "expires_at": None,
                                  "deleted_at": None, "active_7d": None})
                    break
                chain.append(self._departed_node(row))
            current = self._inviter_id(current)
        return chain

    def _inviter_id(self, user_id: str) -> str:
        row = self.db.one("SELECT inviter_id FROM members WHERE emby_user_id=?", (user_id,))
        if row is None:
            row = self.db.one("SELECT inviter_id FROM member_departures WHERE emby_user_id=? "
                              "ORDER BY deleted_at DESC LIMIT 1", (user_id,))
        inviter = str((row or {}).get("inviter_id") or "")
        return "" if inviter == user_id else inviter

    # -- cases -----------------------------------------------------------------

    def open_case(self, departure_id: int | None, source: str, actor: str) -> int | None:
        """One case per reviewable deletion whose direct inviter is still a member."""
        if not departure_id or source not in REVIEW_SOURCES:
            return None
        departure = self.db.one("SELECT * FROM member_departures WHERE id=?", (int(departure_id),))
        inviter_id = str((departure or {}).get("inviter_id") or "")
        if not inviter_id or inviter_id == departure["emby_user_id"]:
            return None
        inviter = self.members.get(inviter_id)
        if not inviter or "admin" in (inviter.get("roles") or []):
            return None
        now = int(time.time())
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO invite_cases(departure_id,deleted_user_id,inviter_id,source,actor,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(departure_id) DO NOTHING",
                (int(departure_id), departure["emby_user_id"], inviter_id, source, str(actor or ""), now, now))
            row = conn.execute("SELECT id FROM invite_cases WHERE departure_id=?", (int(departure_id),)).fetchone()
        return int(row[0]) if row else None

    def case(self, case_id: int) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM invite_cases WHERE id=?", (int(case_id),))
        if not row:
            return None
        departure = self.db.one("SELECT * FROM member_departures WHERE id=?", (row["departure_id"],)) or {}
        inviter = self.members.get(row["inviter_id"])
        if inviter:
            inviter_view = {**self._live_node(inviter, time.time()),
                            "level": inviter.get("activity_level"),
                            "invitees": self.count_descendants(row["inviter_id"])}
        else:
            gone = self.db.one("SELECT * FROM member_departures WHERE emby_user_id=? ORDER BY deleted_at DESC LIMIT 1",
                               (row["inviter_id"],))
            inviter_view = self._departed_node(gone) if gone else {
                "emby_user_id": row["inviter_id"], "username": "", "label": "未知账号", "status": "deleted"}
        suspension = active_suspension(self.db, row["inviter_id"])
        return {**row, "deleted": self._departed_node(departure) if departure else {
                    "emby_user_id": row["deleted_user_id"], "username": "", "label": "未知账号"},
                "inviter": inviter_view, "suspended_until": suspension["until"] if suspension else None,
                "source_label": SOURCE_LABELS.get(row["source"], row["source"])}

    def cases(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if status == "pending":
            rows = self.db.query("SELECT id FROM invite_cases WHERE status IN ('pending','processing') "
                                 "ORDER BY id DESC LIMIT ?", (int(limit),))
        else:
            rows = self.db.query("SELECT id FROM invite_cases ORDER BY id DESC LIMIT ?", (int(limit),))
        return [case for row in rows if (case := self.case(row["id"]))]

    def cases_for(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT id FROM invite_cases WHERE inviter_id=? OR deleted_user_id=? ORDER BY id DESC LIMIT 20",
                             (str(user_id), str(user_id)))
        return [case for row in rows if (case := self.case(row["id"]))]

    def claim(self, case_id: int) -> dict[str, Any] | None:
        """Exactly one decider proceeds; everyone else sees the settled case."""
        changed = self.db.execute(
            "UPDATE invite_cases SET status='processing',updated_at=? WHERE id=? AND status='pending'",
            (int(time.time()), int(case_id)))
        return self.db.one("SELECT * FROM invite_cases WHERE id=?", (int(case_id),)) if changed else None

    def settle(self, case_id: int, status: str, actor: str, result: str) -> None:
        now = int(time.time())
        self.db.execute(
            "UPDATE invite_cases SET status=?,decided_by=?,decided_at=?,result=?,updated_at=? WHERE id=?",
            (status, str(actor or ""), now, result, now, int(case_id)))

    def release(self, case_id: int, error: str) -> None:
        self.db.execute("UPDATE invite_cases SET status='pending',result=?,updated_at=? WHERE id=? AND status='processing'",
                        (error, int(time.time()), int(case_id)))

    def recover(self) -> None:
        """A crash mid-decision: settled if the inviter is already gone, otherwise reopened."""
        for row in self.db.query("SELECT id,inviter_id FROM invite_cases WHERE status='processing'"):
            if self.members.get(row["inviter_id"]) is None:
                self.settle(row["id"], "deleted", "system", "处理中断；邀请人已不在系统中，按已删除记录")
            else:
                self.release(row["id"], "上次处理被中断，结果未生效，请重新选择")

    # -- suspensions -------------------------------------------------------------

    def suspend(self, user_id: str, *, actor: str, case_id: int | None = None,
                days: int = SUSPEND_DAYS) -> int:
        now = int(time.time())
        current = active_suspension(self.db, user_id, now)
        until = max(int(current["until"]) if current else 0, now + int(days) * DAY)
        self.db.execute(
            "INSERT INTO invite_suspensions(user_id,until,case_id,actor,created_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET until=excluded.until,case_id=excluded.case_id,"
            "actor=excluded.actor,created_at=excluded.created_at",
            (str(user_id), until, case_id, str(actor or ""), now))
        self.members.audit(actor, "invite.suspend", str(user_id), f"邀请资格暂停至 {until}")
        return until

    def lift(self, user_id: str, *, actor: str) -> bool:
        removed = self.db.execute("DELETE FROM invite_suspensions WHERE user_id=?", (str(user_id),))
        if removed:
            self.members.audit(actor, "invite.unsuspend", str(user_id), "解除邀请资格暂停")
        return bool(removed)


async def decide_case(tree: InviteTree, emby: Any, case_id: int, action: str, *,
                      actor: str, authorize: Any = None) -> dict[str, Any]:
    """Shared by the group card and the Deck; returns the settled case view."""
    if action not in CASE_ACTIONS:
        raise ValueError("未知操作")
    row = tree.claim(case_id)
    if row is None:
        current = tree.case(case_id)
        if current is None:
            raise KeyError(case_id)
        return {**current, "already": True}
    inviter_id = row["inviter_id"]
    try:
        if tree.members.get(inviter_id) is None:
            tree.settle(case_id, "gone", actor, "邀请人已不在系统中")
        elif action == "ignore":
            tree.settle(case_id, "ignored", actor, "不处理")
        elif action == "suspend":
            until = tree.suspend(inviter_id, actor=actor, case_id=case_id)
            tree.settle(case_id, "suspended", actor, f"邀请资格暂停至 {until}")
        else:
            from app.modules import member_ops
            result = await member_ops.execute_delete(
                tree.members, emby, inviter_id, actor=actor, cascade=False,
                delete_emby=True, confirm_ids=[inviter_id], authorize=authorize,
                review_source="")
            if not result.get("deleted"):
                error = str(result.get("error") or "删除未完成")
                tree.release(case_id, "删除未完成：" + error)
                return {**(tree.case(case_id) or {}), "error": error}
            tree.settle(case_id, "deleted", actor, "已连带删除邀请人")
    except Exception as exc:
        tree.release(case_id, "处理失败：" + str(exc)[:120])
        raise
    return tree.case(case_id) or {}
