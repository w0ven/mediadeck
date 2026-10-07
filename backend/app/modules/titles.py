"""Ordinary Telegram member tags, with durable retries and hand-edit protection.

Telegram has no compare-and-set API. We read immediately before setting a tag;
a simultaneous external admin edit in that network window cannot be excluded.
No administrators are promoted and administrator custom titles are never used.
"""

from __future__ import annotations

import asyncio
import time
from app.modules.economy_rules import economy_write

from app.modules.inventory import validate_title
from app.modules.economy_rules import encode


class TitleService:
    def __init__(self, db, members, bot, config):
        self.db, self.members, self.bot, self.config = db, members, bot, config
        self._lock = asyncio.Lock()

    def titles(self, user):
        return self.db.query(
            "SELECT * FROM member_titles WHERE emby_user_id=? ORDER BY id DESC", (str(user),)
        )

    def states(self, user=None):
        return self.db.query(
            "SELECT * FROM title_tags" + (" WHERE emby_user_id=?" if user else ""),
            (str(user),) if user else (),
        )

    def target(self):
        target = self.config().get("title_chat", "")
        allowed = self.bot._group_allowlist()
        if not target:
            if len(allowed) != 1:
                raise ValueError("请后台明确配置称号目标群")
            target = allowed[0]
        if target not in allowed:
            raise ValueError("称号群须属于现有Bot群交互白名单")
        return target

    def grant(self, user, tag, days=0, actor="operator"):
        tag = validate_title(tag)
        if type(days) is not int or not 0 <= days <= 36500:
            raise ValueError("期限无效")
        if not self.members.get(str(user)):
            raise ValueError("账号不存在")
        now = int(time.time())
        with economy_write(self.db) as conn:
            cur = conn.execute(
                "INSERT INTO member_titles(emby_user_id,tag,created_at,expires_at,actor) VALUES(?,?,?,?,?)",
                (str(user), tag, now, now + days * 86400 if days else None, actor),
            )
            title_id = int(cur.lastrowid)
            self._audit(
                conn, actor, "title.grant", user, dict(title_id=title_id, tag=tag, days=days)
            )
        return dict(ok=True, title_id=title_id)

    async def revoke(self, user, title_id, actor="operator"):
        with economy_write(self.db) as conn:
            cur = conn.execute(
                "UPDATE member_titles SET revoked_at=?,worn=0 WHERE id=? AND emby_user_id=?",
                (int(time.time()), int(title_id), str(user)),
            )
            if not cur.rowcount:
                raise ValueError("称号不存在")
            conn.execute(
                "UPDATE title_tags SET desired_tag='',title_id=NULL,status='pending',generation=generation+1,retry_at=0 "
                "WHERE emby_user_id=? AND title_id=?",
                (str(user), int(title_id)),
            )
            self._audit(conn, actor, "title.revoke", user, dict(title_id=title_id))
        await self.sync(user)
        return dict(ok=True, states=self.states(user))

    async def wear(self, user, title_id=None):
        user = str(user)
        member = self.members.get(user)
        if not member or not member.get("tg_user_id"):
            raise ValueError("请先绑定Telegram账号")
        target = self.target()
        now = int(time.time())
        with economy_write(self.db) as conn:
            tag = ""
            if title_id is not None:
                row = conn.execute(
                    "SELECT * FROM member_titles WHERE id=? AND emby_user_id=?",
                    (int(title_id), user),
                ).fetchone()
                if (
                    not row
                    or row["revoked_at"] is not None
                    or (row["expires_at"] is not None and row["expires_at"] <= now)
                ):
                    raise ValueError("称号不存在、已撤销或已到期")
                tag = row["tag"]
            conn.execute("UPDATE member_titles SET worn=0 WHERE emby_user_id=?", (user,))
            if title_id is not None:
                conn.execute("UPDATE member_titles SET worn=1 WHERE id=?", (int(title_id),))
            # A target/config/linkage change clears the OLD tracked tag as well.
            conn.execute(
                "UPDATE title_tags SET desired_tag='',title_id=NULL,status='pending',generation=generation+1,retry_at=0 "
                "WHERE emby_user_id=? AND (chat_id<>? OR tg_user_id<>?)",
                (user, target, str(member["tg_user_id"])),
            )
            conn.execute(
                "INSERT INTO title_tags(chat_id,tg_user_id,emby_user_id,title_id,desired_tag) VALUES(?,?,?,?,?) "
                "ON CONFLICT(chat_id,tg_user_id) DO UPDATE SET title_id=excluded.title_id,desired_tag=excluded.desired_tag,"
                "status='pending',generation=generation+1,retry_at=0,emby_user_id=excluded.emby_user_id",
                (target, str(member["tg_user_id"]), user, title_id, tag),
            )
            self._audit(conn, "member", "title.wear", user, dict(title_id=title_id, tag=tag))
        await self.sync(user)
        states = self.states(user)
        return dict(
            ok=all(r["status"] == "synced" for r in states),
            states=states,
            reason="已同步"
            if all(r["status"] == "synced" for r in states)
            else "标签尚未同步，请查看待重试/保护状态",
        )

    async def sync(self, user=None):
        async with self._lock:
            now = int(time.time())
            with economy_write(self.db) as conn:
                conn.execute(
                    "UPDATE member_titles SET worn=0 WHERE revoked_at IS NOT NULL OR (expires_at IS NOT NULL AND expires_at<=?)",
                    (now,),
                )
                conn.execute(
                    "UPDATE title_tags SET desired_tag='',title_id=NULL,status='pending',generation=generation+1,retry_at=0 "
                    "WHERE title_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM member_titles t "
                    "WHERE t.id=title_tags.title_id AND t.revoked_at IS NULL AND (t.expires_at IS NULL OR t.expires_at>?))",
                    (now,),
                )
            for row in self.states(user):
                if row["status"] == "protected":
                    continue
                if row["status"] == "synced" and row["desired_tag"] == row["applied_tag"]:
                    continue
                if user is None and row["retry_at"] > now:
                    continue
                try:
                    await self._sync_one(row, now)
                except Exception:
                    # No raw exceptions: client errors may contain the bot URL/token.
                    self._update(
                        row,
                        status="pending",
                        error="TG同步失败，待重试",
                        attempts=row["attempts"] + 1,
                        retry_at=now + min(3600, 60 * 2 ** min(row["attempts"], 6)),
                    )

    async def _sync_one(self, row, now):
        chat, user = row["chat_id"], row["tg_user_id"]
        me = await self.bot._call("getMe")
        if not isinstance(me, dict):
            raise ValueError("TG不可用")
        admin = await self.bot._call("getChatMember", dict(chat_id=chat, user_id=me["id"]))
        if (
            not isinstance(admin, dict)
            or admin.get("status") not in ("administrator", "creator")
            or not admin.get("can_manage_tags")
        ):
            self._update(
                row,
                status="pending",
                error="Bot缺少can_manage_tags权限",
                attempts=row["attempts"] + 1,
                retry_at=now + 600,
            )
            return
        current = await self.bot._call("getChatMember", dict(chat_id=chat, user_id=int(user)))
        if not isinstance(current, dict):
            raise ValueError("无法核对当前标签")
        if current.get("status") not in ("member", "restricted"):
            self._update(
                row,
                status="pending",
                error="目标不是普通群成员（不会升管理员）",
                retry_at=now + 600,
            )
            return
        actual = current.get("tag") or ""
        desired = row["desired_tag"]
        # An unacknowledged successful previous call is recovered by reading.
        if actual == desired and (
            actual == "" or actual in (row["applied_tag"], row["attempted_tag"])
        ):
            self._update(
                row,
                status="synced",
                applied_tag=desired,
                attempted_tag="",
                error="",
                attempts=0,
                retry_at=0,
            )
            return
        expected = (row["applied_tag"], row["attempted_tag"])
        if actual not in expected:
            self._update(
                row, status="protected", error="标签已被群管理/成员手改，保留现有标签，不自动覆盖"
            )
            return
        # Record attempted value BEFORE sending. A lost acknowledgement must
        # not orphan a tag that actually landed (including after expiration).
        if not self._update(row, attempted_tag=desired):
            return  # desired state changed while reading Telegram; never send the stale choice
        result = await self.bot._call(
            "setChatMemberTag", dict(chat_id=chat, user_id=int(user), tag=desired)
        )
        if result is not True:
            raise ValueError("TG拒绝或结果未知")
        self._update(
            row,
            status="synced",
            applied_tag=desired,
            attempted_tag="",
            error="",
            attempts=0,
            retry_at=0,
        )

    def _update(self, row, **values):
        with economy_write(self.db) as conn:
            changed = conn.execute(
                "UPDATE title_tags SET "
                + ",".join(f"{k}=?" for k in values)
                + " WHERE chat_id=? AND tg_user_id=? AND generation=?",
                (*values.values(), row["chat_id"], row["tg_user_id"], row["generation"]),
            )
            return bool(changed.rowcount)

    @staticmethod
    def _audit(conn, actor, action, user, detail):
        conn.execute(
            "INSERT INTO audit_log(ts,actor,action,subject,detail,ok) VALUES(?,?,?,?,?,1)",
            (int(time.time()), actor, action, str(user), encode(detail)),
        )
