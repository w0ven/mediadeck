"""Request center: open -> accepted/rejected/cancelled, with transactional quota/outbox.

Acceptance is FINAL. Downloads are arranged manually, never observed or started here.
Legacy claim/resolve entry points are intentionally disabled (HTTP 410 at the API).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime

from app.core.errors import ConfigError

STATUSES = ("open", "accepted", "rejected", "cancelled")
ACTIVE_STATUSES = ("open",)
STATUS_LABELS = {
    "open": "待处理",
    "accepted": "已接受",
    "rejected": "已拒绝",
    "cancelled": "已取消",
}
MEDIA_TYPES = ("movie", "tv")
MEDIA_TYPE_LABELS = {"movie": "电影", "tv": "剧集"}
NOTE_MAX = 500
ACCEPT_NOTICE = "上片员已接受你的请求，将安排下载，请耐心等待下载完成并入库"


class RequestError(Exception):
    """A safe, actionable business refusal."""


def current_period(now=None):
    return datetime.fromtimestamp(int(now if now is not None else time.time()), UTC).strftime(
        "%Y-%m"
    )


def status_label(status):
    return STATUS_LABELS.get(status, status)


def media_label(media_type):
    return MEDIA_TYPE_LABELS.get(media_type, media_type)


def display_title(row):
    title = str(
        row.get("title")
        or f"暂未获取片名 · {media_label(row.get('media_type', ''))} #{row.get('tmdb_id')}"
    )
    return f"{title} ({row['year']})" if row.get("year") else title


def numbers(value, maximum=999):
    """Canonical season/episode sets, accepting 1,3-5; bounded expansion."""
    if isinstance(value, list):
        value = ",".join(map(str, value))
    result = set()
    for part in re.split(r"[,，\s]+", str(value or "").strip()):
        if not part:
            continue
        if not re.fullmatch(r"\d{1,3}(?:-\d{1,3})?", part):
            raise RequestError("请用数字和逗号/范围，例如 1,3-5")
        a, _, b = part.partition("-")
        lo, hi = int(a), int(b or a)
        if lo < 0 or hi < lo or hi > maximum:
            raise RequestError("季/集范围无效")
        result.update(range(lo, hi + 1))
    return sorted(result)


def normalize_demand(media_type, raw=None, *, legacy=False):
    if media_type not in MEDIA_TYPES:
        raise RequestError("必须明确选择电影或剧集")
    raw = raw or {}
    if not isinstance(raw, dict):
        raise RequestError("需求格式无效")
    scope = str(raw.get("scope") or ("movie" if media_type == "movie" else "series"))
    if scope not in (
        ("movie", "version")
        if media_type == "movie"
        else ("series", "season", "episodes", "version")
    ):
        raise RequestError("需求类型无效")
    seasons, episodes = numbers(raw.get("seasons")), numbers(raw.get("episodes"))
    if media_type == "movie":
        seasons, episodes = [], []
    if scope in ("season", "episodes") and not seasons and not legacy:
        raise RequestError("请指定季数")
    if scope == "episodes" and (len(seasons) != 1 or not episodes):
        raise RequestError("指定集需要一季及集数；多季请分别提交")
    if scope != "episodes":
        episodes = []
    out = {"scope": scope, "seasons": seasons, "episodes": episodes}
    for field in ("version", "quality", "subtitle", "audio"):
        text = str(raw.get(field) or "").strip()
        if len(text) > 100:
            raise RequestError("单项要求最多 100 字")
        out[field] = "" if text in ("无要求", "跳过", "-") else text
    if scope == "version" and not out["version"]:
        raise RequestError("补版本请注明所需版本")
    return out


def demand_key(demand):
    canonical = {k: v.casefold() if isinstance(v, str) else v for k, v in demand.items()}
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def number_ranges(values):
    ranges = []
    values = sorted(set(values or []))
    for value in values:
        if ranges and value == ranges[-1][1] + 1:
            ranges[-1][1] = value
        else:
            ranges.append([value, value])
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in ranges)


def demand_text(d):
    scope = {
        "movie": "电影整片",
        "series": "剧集全剧",
        "season": "指定季",
        "episodes": "指定集",
        "version": "补版本",
    }.get(d.get("scope"), "")
    lines = [scope]
    if d.get("seasons"):
        lines.append("季：" + number_ranges(d["seasons"]))
    if d.get("episodes"):
        lines.append("集：" + number_ranges(d["episodes"]))
    for field, label in (
        ("version", "版本"),
        ("quality", "清晰度"),
        ("subtitle", "字幕"),
        ("audio", "配音"),
    ):
        lines.append(f"{label}：{d.get(field) or '无要求'}")
    return "\n".join(lines)


def parse_status(raw):
    value = str(raw or "").strip()
    if value == "active":
        return "open"
    if value and value not in STATUSES:
        raise ConfigError(f"未知状态：{value}")
    return value or None


class RequestService:
    def __init__(self, db, members=None, groups=None, tmdb=None, library=None):
        self._db, self._members, self._groups, self._tmdb, self._library = (
            db,
            members,
            groups,
            tmdb,
            library,
        )

    @contextmanager
    def _transaction(self):
        # Serialize even distinct SQLite connections/processes before checking
        # quota/followers/refunds, not only this process's shared connection.
        with self._db.write() as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            yield conn

    def quota_for(self, member):
        group_id = str(member.get("group_id") or "")
        group = self._groups.get(group_id) if self._groups and group_id else None
        group = group or member.get("group") or {}
        try:
            return max(0, int(group.get("request_quota") or 0))
        except (ValueError, TypeError):
            return 0

    def used(self, user_id, now=None):
        row = self._db.one(
            "SELECT request_used,request_period FROM members WHERE emby_user_id=?", (str(user_id),)
        )
        return (
            max(0, int(row.get("request_used") or 0))
            if row and row["request_period"] == current_period(now)
            else 0
        )

    def remaining(self, user_id, now=None):
        member = self._members.get(user_id) if self._members else None
        if not member:
            return 0
        quota = self.quota_for(member)
        return max(0, quota - self.used(user_id, now)) if quota else None

    def get(self, request_id):
        row = self._db.one("SELECT * FROM media_requests WHERE id=?", (int(request_id),))
        return self._decorate(row) if row else None

    def _decorate(self, row):
        row = dict(row)
        row["demand"] = json.loads(row["demand_json"])
        row["demand_text"] = demand_text(row["demand"])
        row["status_label"] = status_label(row["status"])
        row["media_label"] = media_label(row["media_type"])
        row["display_title"] = display_title(row)
        holder = (
            self._members.get(row["claimed_by"]) if self._members and row["claimed_by"] else None
        )
        row["claimed_by_name"] = (holder or {}).get("username", row["claimed_by"])
        return row

    def list(
        self,
        status=None,
        limit=100,
        offset=0,
        *,
        user_id=None,
        followed=False,
        media_type=None,
        search="",
    ):
        where, params = [], []
        if status:
            status = "open" if status == "active" else status
            if status not in STATUSES:
                raise RequestError("未知状态")
            where.append("r.status=?")
            params.append(status)
        if user_id:
            where.append(
                "r.id IN (SELECT request_id FROM request_followers WHERE user_id=?)"
                if followed
                else "r.emby_user_id=?"
            )
            params.append(str(user_id))
        if media_type:
            if media_type not in MEDIA_TYPES:
                raise RequestError("类型无效")
            where.append("r.media_type=?")
            params.append(media_type)
        if search:
            where.append("(r.title LIKE ? OR CAST(r.id AS TEXT)=? OR CAST(r.tmdb_id AS TEXT)=?)")
            params.extend(["%" + str(search)[:100] + "%", str(search), str(search)])
        sql = "SELECT r.* FROM media_requests r" + (
            " WHERE " + " AND ".join(where) if where else ""
        )
        sql += " ORDER BY r.id DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        return [self._decorate(r) for r in self._db.query(sql, tuple(params))]

    def for_user(self, user_id, limit=5):
        return self.list(user_id=user_id, limit=limit)

    def stats(self, now=None):
        counts = {s: 0 for s in STATUSES}
        for row in self._db.query("SELECT status,COUNT(*) n FROM media_requests GROUP BY status"):
            counts[row["status"]] = row["n"]
        period = current_period(now)
        counts.update(
            period=period,
            month_total=self._db.one(
                "SELECT COUNT(*) n FROM media_requests WHERE strftime('%Y-%m',created_at,'unixepoch')=?",
                (period,),
            )["n"],
        )
        return counts

    def same_demand(self, media_type, tmdb_id, demand):
        rows = self._db.query(
            "SELECT * FROM media_requests WHERE tmdb_id=? AND media_type=? AND demand_key=? AND status IN ('open','accepted') ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,id DESC LIMIT 1",
            (int(tmdb_id), media_type, demand_key(normalize_demand(media_type, demand))),
        )
        return self._decorate(rows[0]) if rows else None

    def history(self, media_type, tmdb_id):
        return [
            self._decorate(r)
            for r in self._db.query(
                "SELECT * FROM media_requests WHERE media_type=? AND tmdb_id=? AND status='accepted' ORDER BY id DESC LIMIT 5",
                (media_type, int(tmdb_id)),
            )
        ]

    def _event(self, conn, rid, actor, kind, body="", internal=False, key=None):
        cur = conn.execute(
            "INSERT INTO request_events(request_id,actor,kind,body,internal,event_key,created_at) VALUES(?,?,?,?,?,?,?)",
            (rid, str(actor), kind, str(body), int(internal), key, int(time.time())),
        )
        return cur.lastrowid

    def _queue(self, conn, rid, uid, kind, payload, key):
        conn.execute(
            "INSERT OR IGNORE INTO request_outbox(request_id,user_id,kind,payload,event_key) VALUES(?,?,?,?,?)",
            (rid, str(uid), kind, json.dumps(payload, ensure_ascii=False), key),
        )

    def _refresh(self, conn, rid, revision):
        self._queue(conn, rid, "", "refresh", {}, f"refresh:{rid}:{revision}")

    def _results(self, conn, row, *, correction=False):
        uids = {row["emby_user_id"]} | {
            r[0]
            for r in conn.execute(
                "SELECT user_id FROM request_followers WHERE request_id=?", (row["id"],)
            )
        }
        for uid in uids:
            self._queue(
                conn,
                row["id"],
                uid,
                "result",
                {"status": row["status"], "note": row["result_note"], "correction": correction},
                f"result:{row['id']}:{row['revision']}:{uid}",
            )
        self._refresh(conn, row["id"], row["revision"])

    async def create(
        self,
        user_id,
        media_type,
        tmdb_id,
        note="",
        *,
        confirmed_type=False,
        demand=None,
        create_key=None,
    ):
        if media_type not in MEDIA_TYPES:
            raise RequestError("必须明确选择电影或剧集")
        try:
            tmdb_id = int(tmdb_id)
        except (ValueError, TypeError):
            raise RequestError("TMDB 编号无效") from None
        if tmdb_id <= 0:
            raise RequestError("TMDB 编号无效")
        demand = normalize_demand(media_type, demand)
        if len(str(note)) > NOTE_MAX:
            raise RequestError("备注最多 500 字")
        meta = None
        if self._tmdb:
            # Explicit type never falls back to a different work with the same ID.
            if hasattr(self._tmdb, "lookup"):
                lookup = getattr(self._tmdb, "lookup_request", self._tmdb.lookup)
                meta = await lookup(media_type, tmdb_id)
            else:
                resolved, found = await self._tmdb.resolve(media_type, tmdb_id)
                meta = found if resolved == media_type else None
        meta = meta or {}
        now, period = int(time.time()), current_period()
        with self._transaction() as c:
            if create_key:
                existing = c.execute(
                    "SELECT id,emby_user_id FROM media_requests WHERE create_key=?", (create_key,)
                ).fetchone()
                if existing:
                    if existing["emby_user_id"] != user_id:
                        raise RequestError("确认凭据不属于你")
                    return dict(self.get(existing["id"]), created=False)
            member = self._members.get(user_id) if self._members else None
            if not member:
                raise RequestError("账号不存在")
            existing = self.same_demand(media_type, tmdb_id, demand)
            if existing:
                if existing["status"] == "open":
                    if existing["emby_user_id"] != str(user_id):
                        c.execute(
                            "INSERT OR IGNORE INTO request_followers VALUES(?,?,?)",
                            (existing["id"], str(user_id), now),
                        )
                    return dict(
                        existing, created=False, followed=existing["emby_user_id"] != str(user_id)
                    )
                raise RequestError(
                    f"同需求 #{existing['id']} 已接受，请等待下载完成并入库；可提交不同缺集/版本需求"
                )
            quota = self.quota_for(member)
            if quota and self.used(user_id, now) >= quota:
                raise RequestError(f"本月求片次数已用完（每月 {quota} 次）")
            cur = c.execute(
                "INSERT INTO media_requests(emby_user_id,username,tg_user_id,tmdb_id,media_type,title,year,poster_path,note,status,created_at,demand_json,demand_key,create_key,original_title,overview) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(user_id),
                    member.get("username", ""),
                    member.get("tg_user_id", ""),
                    tmdb_id,
                    media_type,
                    meta.get("title", ""),
                    meta.get("year"),
                    meta.get("poster_path", ""),
                    str(note).strip(),
                    "open",
                    now,
                    json.dumps(demand, ensure_ascii=False),
                    demand_key(demand),
                    create_key,
                    str(meta.get("original_title") or ""),
                    str(meta.get("overview") or "")[:2000],
                ),
            )
            rid = cur.lastrowid
            c.execute(
                "UPDATE members SET request_used=CASE WHEN request_period=? THEN COALESCE(request_used,0)+1 ELSE 1 END,request_period=? WHERE emby_user_id=?",
                (period, period, str(user_id)),
            )
            self._event(c, rid, user_id, "created")
            for uploader in self.uploaders():
                self._queue(
                    c,
                    rid,
                    uploader["emby_user_id"],
                    "new",
                    {},
                    f"new:{rid}:{uploader['emby_user_id']}",
                )
        return dict(self.get(rid), created=True)

    def authorize(self, request_id, user_id, *, staff=False, admin=False):
        row = self.get(request_id)
        if not row:
            raise RequestError("求片记录不存在")
        member = self._members.get(str(user_id)) if self._members else None
        if not member:
            raise RequestError("账号不存在或权限已变化")
        roles = member.get("roles") or []
        if admin and "admin" not in roles:
            raise RequestError("仅管理员可执行")
        if staff and not set(roles) & {"uploader", "admin"}:
            raise RequestError("你不是上片员")
        if (
            not staff
            and not admin
            and row["emby_user_id"] != str(user_id)
            and not self._db.one(
                "SELECT 1 FROM request_followers WHERE request_id=? AND user_id=?",
                (request_id, str(user_id)),
            )
        ):
            raise RequestError("这不是你的工单")
        return row

    def finish(self, rid, actor, status, *, note="", revision=None, panel_admin=False):
        if status not in ("accepted", "rejected", "cancelled"):
            raise RequestError("终态无效")
        if len(str(note)) > NOTE_MAX:
            raise RequestError("理由最多 500 字")
        with self._transaction() as c:
            row = (
                self.get(rid)
                if panel_admin
                else self.authorize(rid, actor, staff=status != "cancelled")
            )
            if not row:
                raise RequestError("求片记录不存在")
            if status == "cancelled" and row["emby_user_id"] != str(actor):
                raise RequestError("只能撤回自己的求片")
            if row["status"] != "open" or (revision is not None and row["revision"] != revision):
                raise RequestError("工单已变化或已终结，请刷新；未重复执行")
            changed = c.execute(
                "UPDATE media_requests SET status=?,claimed_by=?,claimed_at=?,resolved_at=?,result_note=?,revision=revision+1 WHERE id=? AND status='open' AND revision=?",
                (
                    status,
                    str(actor),
                    int(time.time()),
                    int(time.time()),
                    str(note).strip() if status == "rejected" else "",
                    rid,
                    row["revision"],
                ),
            ).rowcount
            if not changed:
                raise RequestError("工单已被其他人终结")
            self._event(c, rid, actor, status, str(note).strip() if status == "rejected" else "")
            row = self.get(rid)
            self._results(c, row)
            if status == "accepted":
                self._start_watch(c, rid)
        return {"ok": True, "request": row}

    def claim(self, *args, **kwargs):
        raise RequestError("旧接单操作已停用，请刷新后使用「接受请求」")

    def resolve(self, *args, **kwargs):
        raise RequestError("旧二次处理操作已停用，请刷新后直接接受或拒绝待处理请求")

    def follow(self, rid, actor):
        with self._transaction() as c:
            row = self.get(rid)
            if not row or row["status"] != "open" or not self._members.get(actor):
                raise RequestError("只能关注待处理求片，请刷新")
            if row["emby_user_id"] != str(actor):
                c.execute(
                    "INSERT OR IGNORE INTO request_followers VALUES(?,?,?)",
                    (rid, str(actor), int(time.time())),
                )
        return self.get(rid)

    def modify(self, rid, actor, demand, note, revision):
        with self._transaction() as c:
            row = self.authorize(rid, actor)
            if row["emby_user_id"] != actor:
                raise RequestError("关注者不能修改他人的工单")
            if row["status"] != "open" or row["revision"] != revision:
                raise RequestError("工单已变化或终结，请刷新")
            demand = normalize_demand(row["media_type"], demand)
            if len(note) > NOTE_MAX:
                raise RequestError("备注最多 500 字")
            other = self.same_demand(row["media_type"], row["tmdb_id"], demand)
            if other and other["id"] != rid:
                raise RequestError(f"同需求已有 #{other['id']}，请关注原单")
            try:
                c.execute(
                    "UPDATE media_requests SET demand_json=?,demand_key=?,note=?,revision=revision+1 WHERE id=?",
                    (json.dumps(demand, ensure_ascii=False), demand_key(demand), note.strip(), rid),
                )
            except sqlite3.IntegrityError:
                raise RequestError("同需求已有待处理工单") from None
            self._event(c, rid, actor, "modified", demand_text(demand) + "\n备注：" + note)
            self._refresh(c, rid, revision + 1)
        return self.get(rid)

    def message(
        self,
        rid,
        actor,
        text,
        *,
        staff=False,
        internal=False,
        revision=None,
        key=None,
        panel_admin=False,
    ):
        text = str(text).strip()
        if not text or len(text) > NOTE_MAX:
            raise RequestError("消息须为 1–500 字")
        with self._transaction() as c:
            row = (
                self.get(rid)
                if panel_admin
                else self.authorize(rid, actor, staff=staff or internal)
            )
            if (
                key
                and c.execute(
                    "SELECT 1 FROM request_events WHERE event_key=? AND request_id=? AND actor=?",
                    (key, rid, str(actor)),
                ).fetchone()
            ):
                return row
            if not row:
                raise RequestError("求片记录不存在")
            if row["status"] != "open" or (revision is not None and row["revision"] != revision):
                raise RequestError("工单已变化或终结，无法发送")
            if not (staff or internal or panel_admin) and row["emby_user_id"] != actor:
                raise RequestError("关注者不能补充他人的工单")
            eid = self._event(
                c,
                rid,
                actor,
                "internal" if internal else ("question" if staff or panel_admin else "reply"),
                text,
                internal,
                key,
            )
            if not internal:
                targets = (
                    [row["emby_user_id"]]
                    if staff or panel_admin
                    else [u["emby_user_id"] for u in self.uploaders()]
                )
                for uid in set(targets):
                    self._queue(
                        c,
                        rid,
                        uid,
                        "message",
                        {"body": text, "staff": staff or panel_admin},
                        f"msg:{eid}:{uid}",
                    )
            self._refresh(c, rid, f"message{eid}")
        return row

    def events(self, rid, *, internal=False, limit=20, offset=0):
        return self._db.query(
            "SELECT * FROM request_events WHERE request_id=?"
            + ("" if internal else " AND internal=0")
            + " ORDER BY id DESC LIMIT ? OFFSET ?",
            (rid, min(int(limit), 100), max(0, int(offset))),
        )

    def correct(self, rid, actor, status, reason, revision, *, panel_admin=False):
        if status not in STATUSES or not str(reason).strip():
            raise RequestError("纠错须选择状态并填写原因")
        with self._transaction() as c:
            row = self.get(rid) if panel_admin else self.authorize(rid, actor, admin=True)
            if not row or row["revision"] != revision:
                raise RequestError("工单已变化，请刷新")
            if row["status"] == status:
                raise RequestError("状态未改变")
            try:
                c.execute(
                    "UPDATE media_requests SET status=?,result_note=?,revision=revision+1,resolved_at=? WHERE id=?",
                    (status, "", None if status == "open" else int(time.time()), rid),
                )
            except sqlite3.IntegrityError:
                raise RequestError("同需求已有待处理工单，不能重开") from None
            self._event(
                c,
                rid,
                actor,
                "correction",
                f"{row['status']} -> {status}: {str(reason)[:500]}",
                True,
            )
            self._results(c, self.get(rid), correction=True)
            if status == "accepted":
                self._start_watch(c, rid)
            else:
                self._stop_watch(c, rid)
        return self.get(rid)

    def refund(self, rid, actor, reason, *, panel_admin=False):
        if not str(reason).strip():
            raise RequestError("额度退回须填写原因")
        with self._transaction() as c:
            row = self.get(rid) if panel_admin else self.authorize(rid, actor, admin=True)
            if not row:
                raise RequestError("求片记录不存在")
            if row["refund_at"]:
                raise RequestError("此工单已退回额度，不能重复退回")
            # No cross-month windfall: only refund the charged calendar period.
            if current_period(row["created_at"]) != current_period():
                raise RequestError("该工单扣次月份已结束，不能退入本月额度")
            if not self._members.get(row["emby_user_id"]):
                raise RequestError("原账号已不存在")
            c.execute(
                "UPDATE members SET request_used=MAX(0,request_used-1) WHERE emby_user_id=? AND request_period=?",
                (row["emby_user_id"], current_period()),
            )
            c.execute("UPDATE media_requests SET refund_at=? WHERE id=?", (int(time.time()), rid))
            self._event(c, rid, actor, "refund", str(reason)[:500], True)
        return self.get(rid)

    async def library_hint(self, media_type, tmdb_id, user_id=None):
        try:
            if self._library is None:
                raise RuntimeError("unconfigured")
            items = await self._library.request_lookup(media_type, int(tmdb_id), user_id=user_id)
            return {"available": True, "items": items}
        except Exception:  # noqa: BLE001 - optional upstream delivery must not replay a business action
            return {
                "available": False,
                "items": [],
                "message": "媒体库查询失败/未配置，未知是否已有；仍可提交",
            }

    def _start_watch(self, conn, rid, now=None):
        now = int(now if now is not None else time.time())
        conn.execute(
            "INSERT INTO request_library_watch"
            "(request_id,state,notified_stage,last_checked_at,created_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(request_id) DO UPDATE SET "
            "state='pending', notified_stage='', last_checked_at=0, "
            "created_at=excluded.created_at "
            "WHERE request_library_watch.state NOT IN ('pending','partial')",
            (int(rid), "pending", "", 0, now),
        )

    def _stop_watch(self, conn, rid):
        conn.execute(
            "UPDATE request_library_watch SET state='expired' "
            "WHERE request_id=? AND state IN ('pending','partial')",
            (int(rid),),
        )

    def active_watches(self):
        return self._db.query(
            "SELECT w.request_id, w.state, w.notified_stage, w.last_checked_at, "
            "w.created_at, r.tmdb_id, r.media_type, r.demand_json, r.emby_user_id "
            "FROM request_library_watch w "
            "JOIN media_requests r ON r.id=w.request_id "
            "WHERE w.state IN ('pending','partial') AND r.status='accepted'"
        )

    def touch_watch(self, rid, now=None):
        now = int(now if now is not None else time.time())
        self._db.execute(
            "UPDATE request_library_watch SET last_checked_at=? "
            "WHERE request_id=? AND state IN ('pending','partial')",
            (now, int(rid)),
        )

    def expire_due_watches(self, now=None):
        now = int(now if now is not None else time.time())
        with self._transaction() as conn:
            cur = conn.execute(
                "UPDATE request_library_watch SET state='expired', last_checked_at=? "
                "WHERE state IN ('pending','partial') AND created_at<=?",
                (now, now - 10 * 86400),
            )
            return cur.rowcount

    def apply_library_stage(self, rid, stage, now=None):
        """Enqueue one library notice per recipient; same stage is ignored."""
        if stage not in ("partial", "complete"):
            return None
        now = int(now if now is not None else time.time())
        with self._transaction() as conn:
            watch = conn.execute(
                "SELECT * FROM request_library_watch WHERE request_id=?", (int(rid),)
            ).fetchone()
            row = conn.execute(
                "SELECT * FROM media_requests WHERE id=?", (int(rid),)
            ).fetchone()
            if not watch or not row or row["status"] != "accepted":
                return None
            if watch["state"] not in ("pending", "partial"):
                return None
            already = watch["notified_stage"]
            if already == "complete" or (
                stage == "partial" and already in ("partial", "complete")
            ):
                conn.execute(
                    "UPDATE request_library_watch SET last_checked_at=? WHERE request_id=?",
                    (now, int(rid)),
                )
                return dict(watch)
            state = "done" if stage == "complete" else "partial"
            conn.execute(
                "UPDATE request_library_watch SET state=?, notified_stage=?, last_checked_at=? "
                "WHERE request_id=?",
                (state, stage, now, int(rid)),
            )
            uids = {row["emby_user_id"]} | {
                item[0]
                for item in conn.execute(
                    "SELECT user_id FROM request_followers WHERE request_id=?", (int(rid),)
                )
            }
            for uid in uids:
                self._queue(
                    conn,
                    int(rid),
                    uid,
                    "library",
                    {"stage": stage},
                    f"library:{int(rid)}:{stage}:{uid}",
                )
            return {"state": state, "notified_stage": stage}

    def uploaders(self):
        if self._members is None:
            return []
        return [
            m
            for m in self._members.list()
            if set(m.get("roles") or []) & {"uploader", "admin"} and m.get("tg_user_id")
        ]

    def record_notice(self, request_id, tg_user_id, message_id):
        self._db.execute(
            "INSERT OR REPLACE INTO request_notices VALUES(?,?,?,?)",
            (request_id, str(tg_user_id), message_id, int(time.time())),
        )

    def notices(self, request_id):
        return self._db.query("SELECT * FROM request_notices WHERE request_id=?", (request_id,))

    def claim_delivery(self):
        now = int(time.time())
        with self._transaction() as c:
            row = c.execute(
                "SELECT * FROM request_outbox WHERE state!='sent' AND next_at<=? AND lease_until<=? ORDER BY id LIMIT 1",
                (now, now),
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE request_outbox SET state='sending',lease_until=?,attempts=attempts+1 WHERE id=?",
                    (now + 120, row["id"]),
                )
                return dict(row)
        return None

    def delivery_result(self, oid, ok, message_id=None):
        self._db.execute(
            "UPDATE request_outbox SET state=?,lease_until=0,next_at=?,message_id=COALESCE(?,message_id),error=? WHERE id=?",
            (
                "sent" if ok else "pending",
                int(time.time()) + (0 if ok else 60),
                message_id,
                "" if ok else "Telegram 未确认送达，可重试",
                oid,
            ),
        )

    def retry_notifications(self, rid=None):
        self._db.execute(
            "UPDATE request_outbox SET next_at=0 WHERE state='pending'"
            + (" AND request_id=?" if rid is not None else ""),
            (rid,) if rid is not None else (),
        )

    def notification_status(self, rid):
        return self._db.query(
            "SELECT id,kind,state,attempts,error FROM request_outbox WHERE request_id=? ORDER BY id",
            (rid,),
        )
