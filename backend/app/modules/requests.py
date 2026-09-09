"""Media requests: a member asks for a title, an uploader takes the job.

Three properties this file exists to guarantee.

**One request per title.** The partial unique index on
``(tmdb_id, media_type) WHERE status IN ('open','claimed')`` is the mechanism;
this module's job is to catch the IntegrityError and answer "somebody already
asked for that, it is being handled" instead of surfacing a database error.
Two rows for one film means two uploaders download the same 40GB.

**One claimer.** Claiming is ``UPDATE ... WHERE status='open'`` and the
rowcount decides the winner. Reading the row and then writing it would let two
uploaders who tapped at the same moment both believe they own the job, which
is the same wasted evening the deduplication above prevents. The loser is told
who won.

**A quota that counts refusals.** ``request_used`` is incremented when the
request is created, not when it succeeds. Deriving remaining allowance from
open rows instead would refund every rejection, and a member could keep asking
for unavailable titles forever.

The month boundary is a stored ``YYYY-MM`` string compared on read, so a
member whose month rolled over is correct the first time they ask rather than
whenever a scheduled job next runs.
"""
from __future__ import annotations

import contextlib
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any

from app.core.errors import ConfigError

STATUSES = ("open", "claimed", "done", "rejected")

# Live statuses: the ones the unique index covers, and the ones the panel shows
# as outstanding work.
ACTIVE_STATUSES = ("open", "claimed")

STATUS_LABELS = {
    "open": "待接单",
    "claimed": "处理中",
    "done": "已处理",
    "rejected": "已拒绝",
}

MEDIA_TYPES = ("movie", "tv")

MEDIA_TYPE_LABELS = {"movie": "电影", "tv": "剧集"}

NOTE_MAX = 500


class RequestError(Exception):
    """A refusal the member is allowed to read: quota, duplicate, or state."""


def current_period(now: int | None = None) -> str:
    """Calendar month key, UTC. Matches how traffic periods roll."""
    ts = int(now if now is not None else time.time())
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m")


def status_label(status: str) -> str:
    return STATUS_LABELS.get(str(status or ""), str(status or ""))


def media_label(media_type: str) -> str:
    return MEDIA_TYPE_LABELS.get(str(media_type or ""), str(media_type or ""))


def display_title(row: dict[str, Any]) -> str:
    """What to call this request in a message.

    Falls back to the id when TMDB never answered, because a request with no
    title still has to be nameable in a notification.
    """
    title = str(row.get("title") or "").strip()
    if not title:
        return f"#{row.get('tmdb_id')}"
    year = row.get("year")
    return f"{title} ({year})" if year else title


class RequestService:
    """Quota, deduplication, claiming and resolution for media requests."""

    def __init__(self, db: Any, members: Any = None, groups: Any = None,
                 tmdb: Any = None) -> None:
        self._db = db
        self._members = members
        self._groups = groups
        self._tmdb = tmdb

    # -- quota ---------------------------------------------------------------

    def quota_for(self, member: dict[str, Any]) -> int:
        """The member's monthly allowance. 0 = unlimited."""
        group_id = str(member.get("group_id") or "")
        group = None
        if self._groups is not None and group_id:
            group = self._groups.get(group_id)
        if group is None:
            group = member.get("group") or {}
        try:
            return max(0, int(group.get("request_quota") or 0))
        except (TypeError, ValueError):
            return 0

    def used(self, user_id: str, now: int | None = None) -> int:
        """Requests opened this month. Stale periods read as zero."""
        row = self._db.one(
            "SELECT request_used, request_period FROM members WHERE emby_user_id=?",
            (str(user_id),))
        if not row:
            return 0
        if str(row.get("request_period") or "") != current_period(now):
            return 0
        return max(0, int(row.get("request_used") or 0))

    def remaining(self, user_id: str, now: int | None = None) -> int | None:
        """How many more this month. None = unlimited.

        None rather than a large number: the bot prints "不限" for it, and a
        sentinel integer would eventually be shown to somebody as a count.
        """
        member = self._members.get(user_id) if self._members else None
        if not member:
            return 0
        quota = self.quota_for(member)
        if quota <= 0:
            return None
        return max(0, quota - self.used(user_id, now))

    # -- read ----------------------------------------------------------------

    def get(self, request_id: int) -> dict[str, Any] | None:
        row = self._db.one("SELECT * FROM media_requests WHERE id=?",
                           (int(request_id),))
        return self._decorate(row) if row else None

    def list(self, status: str | None = None,
             limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM media_requests"
        params: list[Any] = []
        if status:
            if status == "active":
                sql += " WHERE status IN ('open','claimed')"
            else:
                if status not in STATUSES:
                    raise RequestError(f"未知状态：{status}")
                sql += " WHERE status=?"
                params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit or 100), 500)))
        return [self._decorate(r) for r in self._db.query(sql, tuple(params))]

    def for_user(self, user_id: str, limit: int = 5) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM media_requests WHERE emby_user_id=? "
            "ORDER BY id DESC LIMIT ?",
            (str(user_id), max(1, min(int(limit or 5), 100))))
        return [self._decorate(r) for r in rows]

    def stats(self, now: int | None = None) -> dict[str, Any]:
        counts = {s: 0 for s in STATUSES}
        for row in self._db.query(
                "SELECT status, COUNT(*) AS n FROM media_requests GROUP BY status"):
            key = str(row.get("status") or "")
            if key in counts:
                counts[key] = int(row.get("n") or 0)
        period = current_period(now)
        month = self._db.one(
            "SELECT COUNT(*) AS n FROM media_requests "
            "WHERE strftime('%Y-%m', created_at, 'unixepoch') = ?", (period,))
        return {
            "open": counts["open"],
            "claimed": counts["claimed"],
            "done": counts["done"],
            "rejected": counts["rejected"],
            "month_total": int((month or {}).get("n") or 0),
            "period": period,
        }

    def _decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        out["status_label"] = status_label(out.get("status", ""))
        out["media_label"] = media_label(out.get("media_type", ""))
        out["display_title"] = display_title(out)
        claimed_by = str(out.get("claimed_by") or "")
        out["claimed_by_name"] = ""
        if claimed_by and self._members is not None:
            holder = self._members.get(claimed_by)
            if holder:
                out["claimed_by_name"] = str(holder.get("username") or "")
        return out

    # -- create --------------------------------------------------------------

    async def create(self, user_id: str, media_type: str, tmdb_id: int,
                     note: str = "", *, confirmed_type: bool = False) -> dict[str, Any]:
        """Open a request, charging one slot against the member's month.

        TMDB enrichment happens *before* the transaction and is allowed to
        fail: the write must not be held open across a network call, and a
        request with no metadata is still a request.
        """
        member = self._members.get(user_id) if self._members else None
        if not member:
            raise RequestError("账号不存在")

        media_type = str(media_type or "").lower()
        try:
            tmdb_id = int(tmdb_id)
        except (TypeError, ValueError):
            raise RequestError("TMDB 编号无效") from None
        if tmdb_id <= 0:
            raise RequestError("TMDB 编号无效")

        left = self.remaining(user_id)
        if left is not None and left <= 0:
            quota = self.quota_for(member)
            raise RequestError(f"本月求片次数已用完（每月 {quota} 次）")

        existing = self._db.one(
            "SELECT * FROM media_requests WHERE tmdb_id=? AND media_type=? "
            "AND status IN ('open','claimed')", (tmdb_id, media_type))
        if existing:
            raise RequestError(
                f"《{display_title(existing)}》已经有人求过，正在处理中")

        meta: dict[str, Any] | None = None
        if self._tmdb is not None:
            resolved_type, meta = await self._tmdb.resolve(media_type, tmdb_id)
            if confirmed_type and resolved_type != media_type and media_type in MEDIA_TYPES:
                # The bot already confirmed this type. Metadata is optional;
                # a miss must never substitute another work after confirmation.
                meta = None
            elif meta is not None:
                media_type = resolved_type
        if media_type not in MEDIA_TYPES:
            media_type = "movie"

        now = int(time.time())
        period = current_period(now)
        note = str(note or "").strip()[:NOTE_MAX]
        meta = meta or {}

        try:
            with self._db.write() as conn:
                # Enrichment yielded to other requests and account edits.
                member = self._members.get(user_id) if self._members else None
                if not member:
                    raise RequestError('账号不存在')
                quota = self.quota_for(member)
                if quota and self.used(user_id, now) >= quota:
                    raise RequestError(f'本月求片次数已用完（每月 {quota} 次）')
                cur = conn.execute(
                    "INSERT INTO media_requests"
                    "(emby_user_id,username,tg_user_id,tmdb_id,media_type,title,"
                    "year,poster_path,note,status,claimed_by,claimed_at,"
                    "resolved_at,result_note,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'open','',NULL,NULL,'',?)",
                    (str(user_id), str(member.get("username") or ""),
                     str(member.get("tg_user_id") or ""), tmdb_id, media_type,
                     str(meta.get("title") or ""), meta.get("year"),
                     str(meta.get("poster_path") or ""), note, now))
                request_id = int(cur.lastrowid or 0)
                # Same transaction as the insert: a counter that can advance
                # without a row, or a row without the counter, is a quota that
                # drifts every time something fails halfway.
                conn.execute(
                    "UPDATE members SET request_used=CASE WHEN request_period=? "
                    "THEN COALESCE(request_used,0)+1 ELSE 1 END,"
                    "request_period=? WHERE emby_user_id=?",
                    (period, period, str(user_id)))
        except sqlite3.IntegrityError:
            # Lost a race against another member asking for the same title
            # between the check above and the insert.
            raise RequestError("这部片子刚刚已经有人求过了，正在处理中") from None

        return self.get(request_id) or {}

    # -- claim / resolve -----------------------------------------------------

    def claim(self, request_id: int, uploader_user_id: str) -> dict[str, Any]:
        """Take ownership of an open request.

        The rowcount of a conditional UPDATE is the whole mechanism: exactly
        one caller can move a row out of 'open', so two uploaders tapping at
        once produce one winner and one informative refusal.
        """
        request = self.get(request_id)
        if not request:
            raise RequestError("求片记录不存在")

        now = int(time.time())
        changed = self._db.execute(
            "UPDATE media_requests SET status='claimed',claimed_by=?,claimed_at=? "
            "WHERE id=? AND status='open'",
            (str(uploader_user_id), now, int(request_id)))
        if not changed:
            current = self.get(request_id) or {}
            return {
                "ok": False,
                "request": current,
                "claimed_by": str(current.get("claimed_by") or ""),
                "claimed_by_name": str(current.get("claimed_by_name") or ""),
                "reason": ("已被接单" if current.get("status") == "claimed"
                           else "该求片已处理完毕"),
            }
        self._audit(uploader_user_id, "request.claim", str(request_id),
                    display_title(request))
        return {"ok": True, "request": self.get(request_id) or {}}

    def resolve(self, request_id: int, uploader_user_id: str, done: bool,
                note: str = "", is_admin: bool = False) -> dict[str, Any]:
        """Close a claimed request as handled or refused.

        Only the uploader holding it may close it, unless an admin steps in:
        letting anyone resolve would mean an uploader's queue could be emptied
        by somebody who did none of the work, and the requester would be told
        their title was handled by a person who never touched it.
        """
        request = self.get(request_id)
        if not request:
            raise RequestError("求片记录不存在")
        if request.get("status") != "claimed":
            raise RequestError(
                f"该求片当前状态是「{status_label(request.get('status', ''))}」，无法处理")
        holder = str(request.get("claimed_by") or "")
        if holder != str(uploader_user_id) and not is_admin:
            raise RequestError("这条求片是别人接的单")

        now = int(time.time())
        status = "done" if done else "rejected"
        note = str(note or "").strip()[:NOTE_MAX]
        changed = self._db.execute(
            "UPDATE media_requests SET status=?,resolved_at=?,result_note=? "
            "WHERE id=? AND status='claimed'",
            (status, now, note, int(request_id)))
        if not changed:
            raise RequestError("该求片状态刚刚发生变化，请刷新后重试")
        self._audit(uploader_user_id, f"request.{status}", str(request_id),
                    f"{display_title(request)} {note}".strip())
        return {"ok": True, "request": self.get(request_id) or {}}

    # -- uploader fan-out bookkeeping ---------------------------------------

    def record_notice(self, request_id: int, tg_user_id: str,
                      message_id: int) -> None:
        """Remember which message told which uploader about this request.

        Without this the fan-out is one-way: when somebody claims, the other
        uploaders keep a live button for a job that is gone.
        """
        self._db.execute(
            "INSERT OR REPLACE INTO request_notices"
            "(request_id,tg_user_id,message_id,created_at) VALUES(?,?,?,?)",
            (int(request_id), str(tg_user_id), int(message_id),
             int(time.time())))

    def notices(self, request_id: int) -> list[dict[str, Any]]:
        return self._db.query(
            "SELECT * FROM request_notices WHERE request_id=? ORDER BY tg_user_id",
            (int(request_id),))

    def uploaders(self) -> list[dict[str, Any]]:
        """Uploaders with a linked chat -- the only ones reachable."""
        if self._members is None:
            return []
        try:
            rows = self._members.list(role="uploader")
        except TypeError:
            rows = [m for m in self._members.list()
                    if "uploader" in (m.get("roles") or [])]
        return [m for m in rows if str(m.get("tg_user_id") or "")]

    def _audit(self, actor: str, action: str, subject: str,
               detail: str) -> None:
        if self._members is None:
            return
        # An audit write that fails must not undo a claim that already
        # committed: the uploader owns the job either way.
        with contextlib.suppress(Exception):
            self._members.audit(str(actor), action, subject, detail[:300])


def parse_status(raw: Any) -> str | None:
    """Validate a status filter coming off a query string."""
    value = str(raw or "").strip()
    if not value:
        return None
    if value == "active":
        return "active"
    if value not in STATUSES:
        raise ConfigError(f"未知状态：{value}")
    return value
