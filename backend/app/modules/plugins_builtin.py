"""The task plugins that ship with the panel.

These five were previously three different things: a hard-coded background loop
in ``main.py`` (expiry reminders, ranking posts), a button on a page
(group audit), and a feature request written down but never built (inactive
cleanup, viewing reports). Expressing all of them as plugins means one place to
switch a job on, one place to configure it, one place to see whether its last
run worked, and one scheduler that cannot fire the same job twice.

Two rules the whole file obeys:

**Nothing here deletes an account.** The strongest action available is
suspension, which is reversible from the member page. Deleting a member is not
just losing a row -- it takes their watch history and, in a household, other
people's access with it. That decision belongs to a person looking at the
account, not to a timer.

**Every run reports what it did in numbers the operator can check.** A summary
of ``{"已通知": 0}`` and a summary of ``{"错误": "..."}`` say very different
things, and a job that silently does nothing is the failure mode that takes
weeks to notice.
"""
from __future__ import annotations

import contextlib
import html
import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.modules.groups import WHITELIST_GROUP_ID
from app.modules.intake_plugin import IntakePipelinePlugin
from app.modules.plugins import Field, Plugin, PluginRegistry, Spec
from app.modules.plugins_points import POINTS_PLUGINS

# How long a "we told you" note is kept before it is considered stale. Without
# this the state document grows one entry per member forever, and a member who
# was warned a year ago would be suspended the day they lapse again rather than
# being warned first.
NOTICE_TTL = 45 * 86400


@dataclass
class PluginContext:
    """What a plugin is allowed to reach.

    Passed in rather than imported: a plugin that reaches for ``app.state``
    directly cannot be tested without booting the whole application, and every
    test here would become an integration test.
    """

    members: Any = None
    emby: Any = None
    telegram: Any = None
    stats: Any = None
    db: Any = None
    settings: Any = None
    store: Any = None
    points: Any = None
    shop: Any = None
    # Node dispatcher, so a member can be shown where they will be served
    # from and how busy it is.
    scheduler: Any = None
    # Media requests, for the digest card that nudges uploaders.
    requests: Any = None
    # -- intake pipeline observability ---------------------------------------
    # Injected wholesale rather than reached for: the collector's filesystem
    # reader and media-server client are the two seams the tests replace, and
    # a plugin that imported either directly could only be tested by booting
    # the whole application against a real host.
    intake_store: Any = None
    intake_paths: Any = None
    intake_fs: Any = None
    intake_emby: Any = None
    intake_downloaders: Any = None
    # Set by register_builtin. A points plugin needs its own live config at
    # the moment a member taps a button, which is not the config that was
    # passed to the last scheduled run.
    registry: Any = None
    # Fallback used when no settings store is available (tests, mock runs).
    _memory: dict[str, dict[str, Any]] = field(default_factory=dict)

    # -- per-plugin state ----------------------------------------------------
    # Distinct from config: config is what the operator typed, state is what
    # the plugin remembers between runs (who has already been warned). Keeping
    # them in separate store sections means a config save cannot wipe the
    # memory of a pending grace period and re-start everyone's clock.

    def state(self, plugin_id: str) -> dict[str, Any]:
        if self.store is None:
            return dict(self._memory.get(plugin_id) or {})
        section = self.store.section("plugin_state") or {}
        return dict(section.get(plugin_id) or {})

    def set_state(self, plugin_id: str, value: dict[str, Any]) -> None:
        if self.store is None:
            self._memory[plugin_id] = dict(value)
            return
        section = dict(self.store.section("plugin_state") or {})
        section[plugin_id] = value
        self.store.set_section("plugin_state", section)


def _prune_notices(notices: dict[str, Any], now: float,
                   ttl: float = NOTICE_TTL) -> dict[str, Any]:
    return {k: v for k, v in notices.items()
            if isinstance(v, (int, float)) and 0 <= now - float(v) < ttl}


def _delivery_state(plugin: Plugin, config: dict[str, Any]) -> dict[str, Any]:
    """Checkpoint only an unfinished daily batch in the existing plugin state.

    Successful manual runs may be tried again. Failed/cancelled runs resume
    without re-sending to recipients whose delivery was already acknowledged.
    """
    key = time.strftime("%Y-%m-%d") + json.dumps(config, sort_keys=True)
    state = plugin.ctx.state(plugin.spec.id)
    return state if state.get("batch") == key else {"batch": key, "sent": []}


def _telegram_ready(ctx: PluginContext) -> bool:
    bot = ctx.telegram
    return bool(bot) and bool(getattr(bot, "enabled", False))


async def _progress(ctx: PluginContext, job: str, text: str) -> None:
    """Start / running / result in the interaction group, edited in place."""
    bot = ctx.telegram
    if not _telegram_ready(ctx):
        return
    post = getattr(bot, "post_job_progress", None)
    if not callable(post):
        return
    with contextlib.suppress(Exception):
        await post(job, text)


# ---------------------------------------------------------------------------
# 1. Group audit
# ---------------------------------------------------------------------------
class GroupAuditPlugin(Plugin):
    """Who is still in the group they were required to join.

    The escalation is deliberately gradual. Leaving a chat is not the same as
    stopping paying -- people leave groups by accident, or because the group
    got noisy -- so the default is to report and let a person look. Suspending
    is available, but only after the member has been told and given time to
    come back.
    """

    spec = Spec(
        id="group_audit",
        name="群组核查",
        description="旧的单群 require_group 核查，已被「群组与频道」成员检测替代，不再出现在任务中心。",
        category="task",
        icon="⚑",
        interval=3600,
        hidden=True,
        fields=[
            Field("action", "发现退群后", kind="select", default="report",
                  options=[("report", "仅报告"), ("notify", "通知本人"),
                           ("suspend", "通知并停用")],
                  help="停用可在用户管理里随时恢复；本插件永远不会删号"),
            Field("grace_days", "宽限天数", kind="int", default=3, min=0, max=30,
                  help="通知后仍未回群，超过这个天数才停用；0 表示通知当次即停用"),
        ],
    )

    async def run(self, config: dict[str, Any]) -> dict[str, Any]:
        bot = self.ctx.telegram
        if not _telegram_ready(self.ctx):
            return {"ok": False, "错误": "机器人未启用，无法查询群成员"}
        report = await bot.audit_group_membership()
        if report.get("unavailable"):
            return {"ok": False, "错误": "未配置要求群组"}

        left = list(report.get("left") or [])
        action = str(config.get("action") or "report")
        grace = max(0, int(config.get("grace_days") or 0))
        now = time.time()
        notices = _prune_notices(self.ctx.state(self.spec.id), now)

        notified = suspended = errors = 0
        still_out: set[str] = set()
        for member in left:
            uid = str(member.get("emby_user_id") or "")
            if not uid:
                continue
            still_out.add(uid)
            if action == "report":
                continue

            first_seen = float(notices.get(uid) or 0)
            if not first_seen:
                # First time out: tell them, start the clock. Suspending on the
                # first observation would punish a reconnect.
                delivered = False
                with contextlib.suppress(Exception):
                    delivered = await bot.notify_member(member, self._notice_text(grace))
                if not delivered:
                    errors += 1
                    continue
                notices[uid] = first_seen = now
                notified += 1
                # Persist each delivered notice before the next await so a
                # cancellation cannot erase the grace period already started.
                self.ctx.set_state(self.spec.id, notices)
                if grace > 0:
                    continue

            if action == "suspend" and now - first_seen >= grace * 86400:
                try:
                    self.ctx.members.set_status(
                        uid, "suspended", actor="plugin:group_audit")
                    suspended += 1
                    notices.pop(uid, None)
                except Exception:  # noqa: BLE001 - keep the notice for retry
                    errors += 1

        # Someone who came back stops being on the clock, so returning and
        # leaving again gets the full grace period rather than instant
        # suspension from a stale note.
        for uid in list(notices):
            if uid not in still_out:
                notices.pop(uid, None)
        self.ctx.set_state(self.spec.id, notices)

        return {
            "ok": errors == 0,
            "失败": errors,
            "检查人数": int(report.get("checked") or 0),
            "已退群": len(left),
            "已通知": notified,
            "已停用": suspended,
            "等待宽限期": len(notices),
        }

    @staticmethod
    def _notice_text(grace_days: int) -> str:
        tail = ("请尽快回到群组，否则账号可能被暂停。" if grace_days <= 0
                else f"请在 {grace_days} 天内回到群组，否则账号可能被暂停。")
        return "⚑ <b>群组核查</b>\n\n检测到你已不在要求的 Telegram 群组中。\n" + tail


# ---------------------------------------------------------------------------
# 2. Inactive cleanup
# ---------------------------------------------------------------------------
class InactiveCleanupPlugin(Plugin):
    """Members who have not watched anything for a long time.

    Named "cleanup" but it does not delete: deletion cascades in ways a timer
    cannot judge. A shared household account that looks idle may be one person
    on holiday, and removing it takes the whole household's history with it.
    Account removal stays a manual action on the member page. This plugin
    suspends matching accounts immediately. Whitelist members are exempt.
    Progress is edited in the interaction group, not messaged to the member.
    """

    spec = Spec(
        id="inactive_cleanup",
        name="活跃清理",
        description="找出长时间没有观看的成员并直接停用，不提前通知。"
                    "白名单豁免活跃要求。本插件不会删号。",
        category="task",
        icon="🧹",
        hour=10,
        fields=[
            Field("days", "不活跃天数", kind="int", default=7, min=1, max=365,
                  help="最近一次使用超过这个天数即视为不活跃并直接停用"),
            Field("hour", "执行时间", kind="int", default=10, min=0, max=23,
                  help="每天几点执行（0–23）"),
        ],
    )

    async def run(self, config: dict[str, Any]) -> dict[str, Any]:
        members = self.ctx.members
        if members is None:
            return {"ok": False, "错误": "成员服务不可用"}
        days = max(1, int(config.get("days") or 7))
        now = time.time()
        cutoff = now - days * 86400

        idle: list[dict[str, Any]] = []
        exempt = 0
        for member in members.list(limit=5000):
            if member.get("status") != "active":
                continue
            if str(member.get("group_id") or "") == WHITELIST_GROUP_ID:
                exempt += 1
                continue
            last_seen = member.get("last_seen_at")
            # Never-seen members count as inactive from their creation date,
            # not as "seen just now": an account created and never used is
            # exactly the population this job exists to surface.
            reference = float(last_seen or member.get("created_at") or 0)
            if reference and reference <= cutoff:
                idle.append(member)

        await _progress(self.ctx, "inactive_cleanup",
                        f"🧹 <b>活跃清理</b>\n开始运行…\n不活跃 {len(idle)} 人，白名单豁免 {exempt} 人")
        suspended = errors = 0
        for index, member in enumerate(idle, 1):
            uid = str(member.get("emby_user_id") or "")
            if not uid:
                continue
            try:
                members.set_status(uid, "suspended",
                                   actor="plugin:inactive_cleanup")
                suspended += 1
            except Exception:  # noqa: BLE001 - keep going, report the miss
                errors += 1
            if index == 1 or index == len(idle) or index % 10 == 0:
                name = html.escape(str(member.get("username") or uid))
                await _progress(
                    self.ctx, "inactive_cleanup",
                    f"🧹 <b>活跃清理</b>\n运行中 {index}/{len(idle)} · {name}\n"
                    f"已停用 {suspended}")
        self.ctx.set_state(self.spec.id, {})
        await _progress(
            self.ctx, "inactive_cleanup",
            f"🧹 <b>活跃清理</b>\n已完成\n"
            f"不活跃 {len(idle)} · 已停用 {suspended} · 失败 {errors} · 白名单豁免 {exempt}")
        return {
            "ok": errors == 0,
            "失败": errors,
            "不活跃人数": len(idle),
            "已停用": suspended,
            "白名单豁免": exempt,
            "说明": "不会删号，未提前通知",
        }


# ---------------------------------------------------------------------------
# 3. Viewing report
# ---------------------------------------------------------------------------
class ViewingReportPlugin(Plugin):
    """A member's own numbers, sent to them.

    Not the leaderboard: this is private and per person. The ranking post says
    who watched the most; this says what *you* watched, which is the part a
    member actually cares about and the part that cannot be posted in a group.
    """

    spec = Spec(
        id="viewing_report",
        name="观影报告",
        description="给已关联 Telegram 的成员发送本人的周报或月报（只发给本人，不公开）。没有观看记录也会发总结图。手动运行在后台发送，避免页面超时。",
        category="task",
        icon="📊",
        hour=20,
        background=True,
        fields=[
            Field("period", "报告周期", kind="select", default="weekly",
                  options=[("weekly", "每周（周一发送）"), ("monthly", "每月（1 号发送）")]),
            Field("hour", "发送时间", kind="int", default=20, min=0, max=23,
                  help="当天几点发送（0–23）"),
        ],
    )

    def due_today(self, config: dict[str, Any], now: float) -> bool:
        local = time.localtime(now)
        if str(config.get("period") or "weekly") == "monthly":
            return local.tm_mday == 1
        return local.tm_wday == 0  # Monday

    async def run(self, config: dict[str, Any]) -> dict[str, Any]:
        if not _telegram_ready(self.ctx):
            return {"ok": False, "错误": "机器人未启用，无法发送报告"}
        if self.ctx.stats is None or self.ctx.members is None:
            return {"ok": False, "错误": "统计服务不可用"}
        monthly = str(config.get("period") or "weekly") == "monthly"
        days = 30 if monthly else 7
        label = "月报" if monthly else "周报"

        sent = empty = errors = 0
        delivery = _delivery_state(self, config)
        linked = list(self.ctx.members.linked_telegram())
        for member in linked:
            uid = str(member.get("emby_user_id") or "")
            if not uid or uid in delivery["sent"]:
                continue
            try:
                detail = self.ctx.stats.member_detail(uid, days=days) or {}
                hours, plays, total_bytes = _summarise(detail)
            except Exception:  # noqa: BLE001 - not the same as no watch records
                errors += 1
                continue
            if plays <= 0:
                empty += 1
            caption = self._text(label, days, hours, plays, total_bytes, detail)
            ok = False
            photo = await self._poster(
                member, label, days, hours, plays, total_bytes, detail)
            notify_photo = getattr(self.ctx.telegram, "notify_member_photo", None)
            if photo and callable(notify_photo):
                with contextlib.suppress(Exception):
                    ok = await notify_photo(member, photo, caption)
            if not ok:
                with contextlib.suppress(Exception):
                    ok = await self.ctx.telegram.notify_member(member, caption)
            if ok:
                sent += 1
                delivery["sent"].append(uid)
                self.ctx.set_state(self.spec.id, delivery)
            else:
                errors += 1
        if not errors:
            self.ctx.set_state(self.spec.id, {})
        return {"ok": errors == 0, "失败": errors,
                "周期": label, "已发送": sent, "无记录仍发送": empty}

    @staticmethod
    def _text(label: str, days: int, hours: float, plays: int,
              total_bytes: int, detail: dict[str, Any]) -> str:
        lines = [f"📊 <b>你的{label}</b>（近 {days} 天）\n",
                 f"观看时长：{hours} 小时",
                 f"播放次数：{plays} 次",
                 f"消耗流量：{_fmt_bytes(total_bytes)}"]
        titles = _top_titles(detail)
        if titles:
            lines.append("\n<b>看得最多</b>")
            lines.extend(f"{i}. {html.escape(str(item['title']))} · {item['plays']} 次"
                         for i, item in enumerate(titles, 1))
        else:
            lines.append("\n这段时间还没有观看记录。")
        return "\n".join(lines)

    async def _poster(self, member: dict[str, Any], label: str, days: int,
                      hours: float, plays: int, total_bytes: int,
                      detail: dict[str, Any]) -> bytes | None:
        titles = _top_titles(detail, 5)
        covers: dict[str, bytes] = {}
        fetch = getattr(getattr(self.ctx, "emby", None), "item_primary_image", None)
        if callable(fetch):
            for item in titles:
                item_id = str(item.get("item_id") or "")
                if not item_id or item_id in covers:
                    continue
                with contextlib.suppress(Exception):
                    blob = await fetch(item_id)
                    if blob:
                        covers[item_id] = blob
        display = str(member.get("tg_display_name") or "").strip()
        handle = str(member.get("tg_username") or "").strip().lstrip("@")
        name = display or (f"@{handle}" if handle else str(member.get("username") or "会员"))
        try:
            from app.modules.rank_poster import render_viewing_poster
            return render_viewing_poster(
                name=name, label=label, days=days, hours=hours, plays=plays,
                traffic=_fmt_bytes(total_bytes), titles=titles, covers=covers,
                whitelist=str(member.get("group_id") or "") == WHITELIST_GROUP_ID)
        except Exception:  # noqa: BLE001 - caption still goes out
            return None


def _summarise(detail: dict[str, Any]) -> tuple[float, int, int]:
    hours = plays = 0.0
    total_bytes = 0
    for point in detail.get("series") or []:
        hours += float(point.get("hours") or 0)
        plays += int(point.get("plays") or 0)
        total_bytes += int(point.get("bytes") or 0)
    return round(hours, 1), int(plays), total_bytes


def _top_titles(detail: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    counts: dict[str, dict[str, Any]] = {}
    for play in detail.get("recent_plays") or []:
        name = str(play.get("series_name") or play.get("item_name") or "").strip()
        if not name:
            continue
        row = counts.setdefault(name, {"title": name, "plays": 0, "item_id": ""})
        row["plays"] += 1
        item_id = str(play.get("item_id") or "")
        if item_id and not row["item_id"]:
            row["item_id"] = item_id
    return sorted(counts.values(), key=lambda item: item["plays"], reverse=True)[:limit]


def _fmt_bytes(n: int) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ---------------------------------------------------------------------------
# 4. Rankings post
# ---------------------------------------------------------------------------
class RankingsPostPlugin(Plugin):
    """Daily group bulletin: movie/episode heat with a poster.

    Watch-time is a separate plugin. This card keeps posting to the same chat
    it already used; the body matches EmbyBoss day_ranks, not a mixed list.
    """

    spec = Spec(
        id="rankings_post",
        name="日榜推送",
        description="每天固定把电影/剧集热度海报发到指定群组或频道。",
        category="task",
        icon="🏆",
        hour=0,
        fields=[
            Field("chat_id", "推送目标", kind="str", default="",
                  help="@channel 或 -100xxxxxxxxxx；留空则不推送"),
            Field("hour", "推送时间", kind="int", default=0, min=0, max=23,
                  help="每天几点推送（0–23）"),
            Field("minute", "推送分钟", kind="int", default=0, min=0, max=59,
                  help="几点几分推送（0–59）"),
            Field("days", "统计范围", kind="int", default=1, min=1, max=30,
                  help="日榜默认 1 天；保留此字段以免旧配置丢失"),
        ],
    )

    async def run(self, config: dict[str, Any]) -> dict[str, Any]:
        chat = str(config.get("chat_id") or "").strip()
        if not chat:
            return {"ok": False, "错误": "未填写推送目标"}
        if not _telegram_ready(self.ctx):
            return {"ok": False, "错误": "机器人未启用"}
        days = max(1, int(config.get("days") or 1))
        ok = await self.ctx.telegram.broadcast_rankings(chat, days=days)
        return {"ok": bool(ok), "推送目标": chat, "统计天数": days,
                "结果": "已发送" if ok else "发送失败"}


class RankingsWeeklyPlugin(Plugin):
    """Sunday group bulletin covering the last seven days."""

    spec = Spec(
        id="rankings_weekly",
        name="周榜推送",
        description="每周日固定把电影/剧集热度海报发到指定群组或频道。",
        category="task",
        icon="📅",
        hour=21,
        fields=[
            Field("chat_id", "推送目标", kind="str", default="",
                  help="@channel 或 -100xxxxxxxxxx；留空则不推送"),
            Field("hour", "推送时间", kind="int", default=21, min=0, max=23,
                  help="当天几点推送（0–23）"),
        ],
    )

    def due_today(self, config: dict[str, Any], now: float) -> bool:
        return time.localtime(now).tm_wday == 6  # Sunday, same as EmbyBoss weekrank

    async def run(self, config: dict[str, Any]) -> dict[str, Any]:
        chat = str(config.get("chat_id") or "").strip()
        if not chat:
            return {"ok": False, "错误": "未填写推送目标"}
        if not _telegram_ready(self.ctx):
            return {"ok": False, "错误": "机器人未启用"}
        ok = await self.ctx.telegram.broadcast_rankings(chat, days=7)
        return {"ok": bool(ok), "推送目标": chat, "统计天数": 7,
                "结果": "已发送" if ok else "发送失败"}


class WatchRankPostPlugin(Plugin):
    """Daily watch-time board covering every member with sampled seconds."""

    spec = Spec(
        id="watch_rank_post",
        name="观影时长日榜",
        description="每天固定把有观影时长的人全部发到指定群组，每页 10 人。",
        category="task",
        icon="⏱",
        hour=0,
        fields=[
            Field("chat_id", "推送目标", kind="str", default="",
                  help="@channel 或 -100xxxxxxxxxx；留空则不推送"),
            Field("hour", "推送时间", kind="int", default=0, min=0, max=23,
                  help="每天几点推送（0–23）"),
            Field("minute", "推送分钟", kind="int", default=5, min=0, max=59,
                  help="几点几分推送（0–59）"),
            Field("days", "统计范围", kind="int", default=1, min=1, max=30,
                  help="日榜默认 1 天，按完整自然日"),
        ],
    )

    async def run(self, config: dict[str, Any]) -> dict[str, Any]:
        chat = str(config.get("chat_id") or "").strip()
        if not chat:
            return {"ok": False, "错误": "未填写推送目标"}
        if not _telegram_ready(self.ctx):
            return {"ok": False, "错误": "机器人未启用"}
        days = max(1, int(config.get("days") or 1))
        ok = await self.ctx.telegram.broadcast_watch_rank(chat, days=days)
        return {"ok": bool(ok), "推送目标": chat, "统计天数": days,
                "结果": "已发送" if ok else "发送失败"}


class WatchRankWeeklyPlugin(Plugin):
    """Sunday watch-time board covering the last seven complete days."""

    spec = Spec(
        id="watch_rank_weekly",
        name="观影时长周榜",
        description="每周日固定把有观影时长的人全部发到指定群组，每页 10 人。",
        category="task",
        icon="📅",
        hour=23,
        fields=[
            Field("chat_id", "推送目标", kind="str", default="",
                  help="@channel 或 -100xxxxxxxxxx；留空则不推送"),
            Field("hour", "推送时间", kind="int", default=23, min=0, max=23,
                  help="当天几点推送（0–23）"),
        ],
    )

    def due_today(self, config: dict[str, Any], now: float) -> bool:
        return time.localtime(now).tm_wday == 6

    async def run(self, config: dict[str, Any]) -> dict[str, Any]:
        chat = str(config.get("chat_id") or "").strip()
        if not chat:
            return {"ok": False, "错误": "未填写推送目标"}
        if not _telegram_ready(self.ctx):
            return {"ok": False, "错误": "机器人未启用"}
        ok = await self.ctx.telegram.broadcast_watch_rank(chat, days=7)
        return {"ok": bool(ok), "推送目标": chat, "统计天数": 7,
                "结果": "已发送" if ok else "发送失败"}


# ---------------------------------------------------------------------------
# 5. Expiry reminder
# ---------------------------------------------------------------------------
class ExpiryReminderPlugin(Plugin):
    """Tell members before their access ends, not after.

    Migrated out of ``telegram_notify_loop`` for the same reason as the ranking
    post. The 10:00 default is kept: a renewal reminder that arrives at 04:00
    wakes someone up to tell them about something days away.
    """

    spec = Spec(
        id="expiry_reminder",
        name="到期提醒",
        description="在有效期结束前提醒已关联 Telegram 的成员续期。",
        category="task",
        icon="⏳",
        hour=10,
        fields=[
            Field("days_ahead", "提前天数", kind="int", default=3, min=1, max=30,
                  help="有效期还剩多少天时开始提醒"),
            Field("hour", "提醒时间", kind="int", default=10, min=0, max=23,
                  help="每天几点提醒（0–23）"),
        ],
    )

    async def run(self, config: dict[str, Any]) -> dict[str, Any]:
        if not _telegram_ready(self.ctx):
            return {"ok": False, "错误": "机器人未启用"}
        if self.ctx.members is None:
            return {"ok": False, "错误": "成员服务不可用"}
        days = max(1, int(config.get("days_ahead") or 3))
        due = self.ctx.members.expiring_within(days)
        delivery = _delivery_state(self, config)
        sent = errors = 0
        linked = sum(1 for m in due if m.get("tg_user_id"))
        for member in due:
            uid = str(member.get("emby_user_id") or member.get("tg_user_id") or "")
            if not member.get("tg_user_id") or uid in delivery["sent"]:
                continue
            ok = False
            with contextlib.suppress(Exception):
                ok = bool(await self.ctx.telegram.notify_expiring([member]))
            if ok:
                sent += 1
                delivery["sent"].append(uid)
                self.ctx.set_state(self.spec.id, delivery)
            else:
                errors += 1
        if not errors:
            self.ctx.set_state(self.spec.id, {})
        return {"ok": errors == 0, "失败": errors,
                "即将到期": len(due), "已关联": linked, "已通知": sent, "提前天数": days}


# ---------------------------------------------------------------------------
# 6. Request digest
# ---------------------------------------------------------------------------
class RequestDigestPlugin(Plugin):
    """A daily nudge to whoever is supposed to be filling requests.

    The per-request notification is a push at the moment somebody asks, which
    is exactly when an uploader is least likely to be free. Requests that
    nobody took therefore go quiet, and the member is left watching a 待处理
    row for a week. This is the reminder that nothing is quiet because
    everything is done.

    ``only_if_open`` defaults on: a digest that arrives every morning to say
    'nothing to do' is one people stop reading, and then they miss the one
    that mattered.
    """

    spec = Spec(
        id="request_digest",
        name="求片摘要",
        description="每天把待处理和已接受的求片数量发给所有上片员。"
                    "默认只在有待处理时发送。",
        category="request",
        icon="🎬",
        hour=9,
        fields=[
            Field("hour", "推送时间", kind="int", default=9, min=0, max=23,
                  help="每天几点推送（0–23）"),
            Field("only_if_open", "仅在有待处理时推送", kind="bool",
                  default=True,
                  help="关闭后即使没有待处理求片也会每天发一条"),
        ],
    )

    async def run(self, config: dict[str, Any]) -> dict[str, Any]:
        if self.ctx.requests is None:
            return {"ok": False, "错误": "求片服务不可用"}
        if not _telegram_ready(self.ctx):
            return {"ok": False, "错误": "机器人未启用"}

        stats = self.ctx.requests.stats()
        pending = int(stats.get("open") or 0)
        working = int(stats.get("accepted") or 0)
        if bool(config.get("only_if_open", True)) and not pending:
            return {"ok": True, "待处理": 0, "已接受": working,
                    "已通知": 0, "结果": "无待处理，未推送"}

        body = (
            "🎬 <b>求片摘要</b>\n\n"
            f"待处理：<b>{pending}</b> 条\n"
            f"已接受：<b>{working}</b> 条\n"
            f"本月累计：{int(stats.get('month_total') or 0)} 条\n\n"
            "发送 /uploader 查看工作台。接受即终结，之后手动安排下载。")

        sent = errors = 0
        delivery = _delivery_state(self, config)
        for uploader in self.ctx.requests.uploaders():
            chat_id = str(uploader.get("tg_user_id") or "")
            if not chat_id or chat_id in delivery["sent"]:
                continue
            ok = False
            with contextlib.suppress(Exception):
                ok = await self.ctx.telegram.send(chat_id, body)
            if ok:
                sent += 1
                delivery["sent"].append(chat_id)
                self.ctx.set_state(self.spec.id, delivery)
            else:
                errors += 1
        if not errors:
            self.ctx.set_state(self.spec.id, {})
        return {"ok": errors == 0, "失败": errors, "待处理": pending,
                "已接受": working, "已通知": sent}


# ---------------------------------------------------------------------------
# 10. Bot migration sweep (one-shot, Sept 2026)
# ---------------------------------------------------------------------------
# EmbyBoss bound members to the OLD bot (@colaembybot); MediaDeck talks through
# a NEW bot (@cola_embybot). The migrated tg_id numbers are intact, but a bot
# may only DM someone who pressed Start on IT, so personal DMs (viewing
# reports) fail for whoever never started the new one. Per the operator's
# dated order: warn via the old bot, and ~24h later prove migration by
# actually DM-ing from the new one -- a delivered probe means the member
# migrated (keep); an undeliverable probe means they never did (delete via the
# same audited path the panel uses, after a last notice on the old bot).
#
# Admins/uploaders are reminded but never auto-deleted: a timer taking out an
# operator account is the exact failure this file's no-deletion rule exists
# to prevent, so staff remain a human decision.
import asyncio  # noqa: E402 - sweep block needs sleeps between DMs

NEW_BOT = "cola_embybot"
OWNER_TG = "6425070392"

WARN_TEXT = (
    "📢 <b>重要通知：服务机器人已更换</b>\n\n"
    "为继续接收观影报告等个人消息，请立刻在新机器人 @"
    + NEW_BOT + " 的对话框里发送 /start 完成开启。\n\n"
    "⚠️ <b>24 小时内未完成操作，您的账号将被删除，且无法恢复。</b>\n"
    "（绑定群内功能不受影响；给新机器人发一条 /start 即可保留账号）")

PROBE_TEXT = "✅ 迁移确认：你的账号绑定正常，这是一条来自新机器人的测试私信，无需任何操作。"

DELETE_NOTICE = (
    "⚠️ <b>账号已删除</b>\n\n"
    "你在 24 小时提醒后仍未到新机器人 @" + NEW_BOT + " 发送 /start，"
    "按提前告知的规则，你的账号现已删除，此操作无法恢复。\n\n"
    "如需重新开通，请联系管理员。")


class _OldBotClient:
    """sendMessage-only client for the retired EmbyBoss bot.

    The token lives in /root/deploy-secrets, never in settings or state: it
    is a credential for a decommissioned bot kept solely so the operator's
    migration warnings reach people on the channel they actually read.
    """

    TOKEN_PATH = "/root/deploy-secrets/oldbot.token"
    API = "https://api.telegram.org"

    def __init__(self) -> None:
        self._token = ""
        self._client: Any = None

    def ready(self) -> bool:
        if not self._token:
            with contextlib.suppress(Exception):
                self._token = open(self.TOKEN_PATH, encoding="utf-8").read().strip()
        return bool(self._token)

    async def _http(self) -> Any:
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
        return self._client

    async def send(self, chat_id: int, text: str) -> bool:
        if not self.ready():
            return False
        try:
            client = await self._http()
            r = await client.post(
                f"{self.API}/bot{self._token}/sendMessage",
                json={"chat_id": chat_id, "text": text,
                      "parse_mode": "HTML", "disable_web_page_preview": True})
            body = r.json()
        except Exception:  # noqa: BLE001 - delivery result is what matters
            return False
        return bool(isinstance(body, dict) and body.get("ok"))


class BotMigrationSweepPlugin(Plugin):
    """Warn old-bot-only members, then delete the ones that never migrated."""

    spec = Spec(
        id="bot_migration_sweep",
        name="Bot迁移清理（一次性）",
        description="阶段一：用旧bot提醒未迁移用户去新bot发/start；约24小时后阶段二：新bot探测，仍未迁移者删除账号并发送删除通知。管理员/上传员仅提醒不自动删除。跑完请关闭本插件。",
        category="task",
        icon="🚚",
        hour=1,
        fields=[
            Field("dry_run", "只演练不删除", kind="bool", default=True,
                  help="阶段二只探测和列出将删名单，不真正删除；核对无误后关闭此项重跑"),
            Field("hour", "执行时间", kind="int", default=1, min=0, max=23,
                  help="每天几点执行（0–23）"),
            Field("minute", "执行分钟", kind="int", default=0, min=0, max=59,
                  help="几点几分执行（0–59）"),
        ],
    )

    async def _targets(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in self.ctx.members.linked_telegram():
            roles = [str(r) for r in (m.get("roles") or [])]
            out.append({
                "emby_user_id": str(m.get("emby_user_id") or ""),
                "username": str(m.get("username") or ""),
                "tg_user_id": str(m.get("tg_user_id") or ""),
                "staff": "admin" in roles or "uploader" in roles,
            })
        return out

    @staticmethod
    def _names(rows: list[dict[str, Any]], flag: str = "staff") -> str:
        return ", ".join(sorted(r["username"] for r in rows if r[flag])) or "无"

    async def run(self, config: dict[str, Any]) -> dict[str, Any]:
        if not _telegram_ready(self.ctx):
            return {"ok": False, "错误": "机器人未启用"}
        old = _OldBotClient()
        if not old.ready():
            return {"ok": False, "错误": "旧bot token 未配置（/root/deploy-secrets/oldbot.token）"}
        state = self.ctx.state(self.spec.id)
        if state.get("completed"):
            return {"ok": True, "阶段": "已完成", "说明": "本轮迁移清理已结束；如需重跑请先在插件页面清除状态"}
        now = int(time.time())
        targets = await self._targets()

        # ---- Phase 1: warn through the old bot --------------------------
        phase1_at = state.get("phase1_done_at")
        if not phase1_at or now - int(phase1_at) < 20 * 3600:
            warned: dict[str, Any] = state.setdefault("warned", {})
            # Only the un-proven need the warning. A member who already got a
            # personal DM from the new bot (the viewing-report delivery list)
            # is migrated; scaring them with a deletion notice is noise.
            known = set((self.ctx.state("viewing_report") or {}).get("sent") or [])
            known |= set(state.get("known_migrated") or [])
            sent_n = fail_n = 0
            skipped_known = 0
            failures: list[str] = []
            for row in targets:
                tg = row["tg_user_id"]
                if not tg or tg in warned:
                    continue
                if row["emby_user_id"] in known:
                    skipped_known += 1
                    continue
                if await old.send(int(tg), WARN_TEXT):
                    warned[tg] = now
                    sent_n += 1
                else:
                    fail_n += 1
                    failures.append(row["username"])
                await asyncio.sleep(0.5)
            # Failures are excluded from `warned`, so phase 2 can never
            # auto-delete someone the old bot never reached; a second warning
            # attempt is pointless against a chat Telegram already refuses.
            state["phase1_done_at"] = now
            state["warned_count"] = len(warned)
            self.ctx.set_state(self.spec.id, state)
            summary = {"阶段": "阶段一·旧bot提醒", "提醒成功": sent_n,
                       "提醒失败": fail_n, "已迁移免提醒": skipped_known,
                       "管理员/上传员仅提醒": self._names(targets),
                       "下一步": "约24小时后自动复核；仍未迁移者将被删除"}
            if failures:
                summary["失败名单"] = ", ".join(failures)
            return {"ok": not failures, **summary}

        # ---- Phase 2: probe from the new bot, delete the unreachable ----
        warned = dict(state.get("warned") or {})
        if not warned:
            self.ctx.set_state(self.spec.id, {"completed": True})
            return {"ok": True, "阶段": "阶段二·复核", "说明": "无待复核名单，已结束"}
        dry = bool(config.get("dry_run", True))
        kept = 0
        deleted_list: list[str] = []
        will_delete: list[str] = []
        notice_fail: list[str] = []
        failures = []
        staff_unreach: list[str] = []
        from app.modules.member_ops import execute_delete
        for row in targets:
            tg = row["tg_user_id"]
            if not tg or tg not in warned:
                continue
            if await self.ctx.telegram.notify_member({"tg_user_id": tg}, PROBE_TEXT):
                kept += 1
                warned.pop(tg, None)
                known_list = list(state.get("known_migrated") or [])
                if row["emby_user_id"] not in known_list:
                    known_list.append(row["emby_user_id"])
                state["known_migrated"] = known_list
                continue
            if row["staff"]:
                staff_unreach.append(row["username"])
                continue
            if dry:
                will_delete.append(row["username"])
                continue
            if not await old.send(int(tg), DELETE_NOTICE):
                notice_fail.append(row["username"])
            try:
                result = await execute_delete(
                    self.ctx.members, self.ctx.emby, row["emby_user_id"],
                    actor="bot_migration_sweep", cascade=False, delete_emby=True)
            except Exception:  # noqa: BLE001 - keep sweeping, report at the end
                result = {"errors": [{"error": "execute_delete 异常"}]}
            if result.get("errors"):
                failures.append(row["username"] + "(删除失败,未删)")
            else:
                deleted_list.append(row["username"])
            await asyncio.sleep(0.4)
        state["warned"] = warned
        finished = not failures and not dry and not staff_unreach and not notice_fail
        if finished:
            self.ctx.set_state(self.spec.id, {"completed": True, "deleted": deleted_list})
        else:
            self.ctx.set_state(self.spec.id, state)
        summary = {"阶段": "阶段二·复核", "已迁移保留": kept,
                   "演练": dry,
                   "管理员/上传员需人工": self._names(
                       [{"username": n} for n in staff_unreach]) if staff_unreach else "无"}
        if will_delete:
            summary["演练·将删除"] = ", ".join(will_delete)
        if deleted_list:
            summary["已删除"] = ", ".join(deleted_list)
        if notice_fail:
            summary["删除通知失败"] = ", ".join(notice_fail)
        if failures:
            summary["删除失败"] = ", ".join(failures)
        report_lines = ["🚚 <b>Bot迁移清理报告</b>"]
        for k, v in summary.items():
            report_lines.append(f"• {k}：{v}")
        with contextlib.suppress(Exception):
            await old.send(int(OWNER_TG), "\n".join(report_lines))
        return {"ok": not failures, **summary}


BUILTIN_PLUGINS = (
    IntakePipelinePlugin,
    GroupAuditPlugin,
    InactiveCleanupPlugin,
    ViewingReportPlugin,
    RankingsPostPlugin,
    RankingsWeeklyPlugin,
    WatchRankPostPlugin,
    WatchRankWeeklyPlugin,
    BotMigrationSweepPlugin,
    ExpiryReminderPlugin,
    RequestDigestPlugin,
    *POINTS_PLUGINS,
)


def register_builtin(registry: PluginRegistry, ctx: PluginContext) -> PluginRegistry:
    """Register every shipped plugin. Called once at startup.

    The registry is handed back to the context on the way through: points
    plugins are invoked by the bot rather than by the scheduler, so they need
    to read their own current config at that moment instead of being given a
    snapshot from whenever the card was last saved.
    """
    ctx.registry = registry
    for cls in BUILTIN_PLUGINS:
        registry.register(cls(ctx))
    return registry


# Settings keys that used to drive the hard-coded telegram_notify_loop. They
# are now owned by the two plugins that replaced it.
LEGACY_TELEGRAM_KEYS = (
    "rankings_chat", "rankings_enabled", "rankings_hour",
    "notify_expiring", "notify_expiring_days",
)


def migrate_legacy_telegram_jobs(store: Any) -> dict[str, Any]:
    """Move the old loop's settings onto the plugin cards, once.

    An operator who had the ranking post switched on before the upgrade must
    still have it switched on after, at the same hour and to the same chat.
    Dropping the values would silently stop a job they are relying on, and
    they would only find out when someone asks why the post stopped.

    The legacy keys are deleted in the same write, which is what makes this
    idempotent: a second call finds nothing to move. It also removes the second
    switch for the same behaviour -- two places controlling one post is how it
    ends up going out twice.
    """
    section = dict(store.section("telegram") or {})
    present = [k for k in LEGACY_TELEGRAM_KEYS if k in section]
    if not present:
        return {"migrated": []}

    plugins = dict(store.section("plugins") or {})
    migrated: list[str] = []

    def _int(key: str, fallback: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(section.get(key, fallback))))
        except (TypeError, ValueError):
            return fallback

    # Existing plugin config wins: if someone already configured the card, the
    # old settings are stale and must not overwrite a deliberate edit.
    chat = str(section.get("rankings_chat") or "").strip()
    if "rankings_post" not in plugins:
        plugins["rankings_post"] = {
            "enabled": bool(section.get("rankings_enabled")) and bool(chat),
            "config": {"chat_id": chat,
                       "hour": _int("rankings_hour", 21, 0, 23), "days": 1},
        }
        migrated.append("rankings_post")
    if "expiry_reminder" not in plugins:
        plugins["expiry_reminder"] = {
            "enabled": bool(section.get("notify_expiring", True)),
            "config": {"days_ahead": _int("notify_expiring_days", 3, 1, 30),
                       "hour": 10},
        }
        migrated.append("expiry_reminder")

    for key in present:
        section.pop(key, None)
    store.set_section("plugins", plugins)
    store.set_section("telegram", section)
    return {"migrated": migrated, "dropped": present}
