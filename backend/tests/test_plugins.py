"""The plugin framework is the panel's only scheduler, so its failure modes
are the interesting part rather than its happy path.

Four properties are load-bearing and each is pinned from both sides:

- **A plugin that raises cannot take the scheduler down.** The failure lands on
  that plugin's card and the loop keeps going. If this breaks, one bad plugin
  silently stops every other job.
- **A daily job runs once a day.** The reminder and the ranking post were moved
  here out of a hand-rolled loop; firing twice means members get the same
  message twice and stop reading them.
- **Unknown config keys are dropped, bad values are refused.** The config
  document is written from a browser form, so anything it accepts it must be
  able to render back.
- **Nothing schedules itself into running.** A disabled plugin, or one whose
  interval has not elapsed, must not run -- and the manual button must run it
  anyway, because that button exists to test a plugin before enabling it.
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path as FilePath
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.plugins import Field, Plugin, PluginRegistry, Spec
from app.modules.plugins_builtin import (
    BUILTIN_PLUGINS,
    ExpiryReminderPlugin,
    GroupAuditPlugin,
    InactiveCleanupPlugin,
    PluginContext,
    RankingsPostPlugin,
    RankingsWeeklyPlugin,
    WatchRankPostPlugin,
    WatchRankWeeklyPlugin,
    RequestDigestPlugin,
    ViewingReportPlugin,
    migrate_legacy_telegram_jobs,
    register_builtin,
)

ADMIN = ("admin", "change-me")


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeStore:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data: dict[str, Any] = data or {}

    def section(self, name: str) -> dict[str, Any]:
        value = self.data.get(name, {})
        return value if isinstance(value, dict) else {}

    def set_section(self, name: str, value: dict[str, Any]) -> None:
        self.data[name] = value


class FakeDb:
    """Just enough of Database for run history."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def execute(self, sql: str, params: tuple = ()) -> int:
        if "plugin_runs" in sql:
            self.rows.append({
                "id": len(self.rows) + 1, "plugin_id": params[0], "ok": params[1],
                "summary": params[2], "started_at": params[3],
                "duration_ms": params[4], "trigger": params[5],
            })
        return 1

    def query(self, sql: str, params: tuple = ()):
        rows = [r for r in self.rows if r["plugin_id"] == params[0]]
        rows.sort(key=lambda r: r["started_at"], reverse=True)
        return rows[: params[1]] if len(params) > 1 else rows

    def one(self, sql: str, params: tuple = ()):
        rows = self.query(sql, (params[0], 1))
        return rows[0] if rows else None


class FakeMembers:
    def __init__(self, members: list[dict[str, Any]] | None = None) -> None:
        self.members = members or []
        self.status_calls: list[tuple[str, str, str]] = []

    def list(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self.members)

    def linked_telegram(self) -> list[dict[str, Any]]:
        return [m for m in self.members if m.get("tg_user_id")]

    def expiring_within(self, days: int = 7) -> list[dict[str, Any]]:
        now = time.time()
        return [m for m in self.members
                if m.get("expires_at")
                and now < m["expires_at"] <= now + days * 86400]

    def set_status(self, user_id: str, status: str, actor: str = "operator"):
        self.status_calls.append((user_id, status, actor))
        return {"emby_user_id": user_id, "status": status}


class FakeBot:
    def __init__(self, *, enabled: bool = True,
                 audit: dict[str, Any] | None = None) -> None:
        self.enabled = enabled
        self._audit = audit or {"checked": 0, "left": [], "unavailable": False}
        self.notified: list[tuple[str, str]] = []
        self.broadcasts: list[tuple[str, int]] = []
        self.watch_broadcasts: list[tuple[str, int]] = []
        self.expiring_calls: list[list[dict[str, Any]]] = []

    async def audit_group_membership(self) -> dict[str, Any]:
        return self._audit

    async def notify_member(self, member: dict[str, Any], text: str) -> bool:
        self.notified.append((str(member.get("emby_user_id")), text))
        return True

    async def broadcast_rankings(self, chat_id: str, days: int = 1) -> bool:
        self.broadcasts.append((chat_id, days))
        return True

    async def broadcast_watch_rank(self, chat_id: str, days: int = 1) -> bool:
        self.watch_broadcasts.append((chat_id, days))
        return True

    async def notify_expiring(self, members: list[dict[str, Any]]) -> int:
        self.expiring_calls.append(list(members))
        return sum(1 for m in members if m.get("tg_user_id"))

    async def send(self, chat_id: str | int, text: str,
                   keyboard: Any = None) -> bool:
        self.notified.append((str(chat_id), text))
        return True


class FakeRequests:
    """Just the two calls the digest card makes."""

    def __init__(self, stats: dict[str, Any] | None = None,
                 uploaders: list[dict[str, Any]] | None = None) -> None:
        self._stats = stats or {"open": 2, "claimed": 1, "done": 5,
                                "rejected": 0, "month_total": 8}
        self._uploaders = uploaders if uploaders is not None else [
            {"emby_user_id": "up1", "username": "bob", "tg_user_id": "801"},
            {"emby_user_id": "up2", "username": "dave", "tg_user_id": "802"},
        ]

    def stats(self) -> dict[str, Any]:
        return dict(self._stats)

    def uploaders(self) -> list[dict[str, Any]]:
        return list(self._uploaders)


class FakeStats:
    def __init__(self, detail: dict[str, Any] | None = None) -> None:
        self.detail = detail if detail is not None else {
            "series": [{"day": "2026-09-01", "hours": 2.5, "plays": 3,
                        "bytes": 1024 * 1024 * 900}],
            "recent_plays": [{"series_name": "剧集甲"}, {"series_name": "剧集甲"},
                             {"item_name": "电影乙"}],
        }

    def member_detail(self, user_id: str, days: int = 30) -> dict[str, Any]:
        return self.detail


def make_ctx(**kwargs: Any) -> PluginContext:
    return PluginContext(**kwargs)


def make_registry(store: FakeStore | None = None, db: FakeDb | None = None):
    return PluginRegistry(store or FakeStore(), db or FakeDb())


class OkPlugin(Plugin):
    spec = Spec(id="ok", name="正常", description="", category="task",
                interval=60,
                fields=[Field("n", "数量", kind="int", default=1, min=0, max=10),
                        Field("flag", "开关", kind="bool", default=False)])

    async def run(self, config: dict[str, Any]) -> dict[str, Any]:
        return {"收到": config.get("n")}


class BoomPlugin(Plugin):
    spec = Spec(id="boom", name="会炸", description="", category="task", interval=60)

    async def run(self, config: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("炸了")


# ---------------------------------------------------------------------------
# Field.coerce
# ---------------------------------------------------------------------------
def test_bool_accepts_the_strings_a_form_actually_sends() -> None:
    f = Field("x", "开关", kind="bool")
    for truthy in ("1", "true", "TRUE", "on", "yes", True, 1):
        assert f.coerce(truthy) is True, truthy
    for falsy in ("0", "false", "off", "", "no", False, 0, None):
        assert f.coerce(falsy) is False, falsy


def test_int_coerces_numeric_strings_and_refuses_the_rest() -> None:
    f = Field("x", "数量", kind="int")
    assert f.coerce("7") == 7
    assert f.coerce(7.0) == 7
    for bad in ("abc", "", None, [], "3.5"):
        with pytest.raises(ValueError):
            f.coerce(bad)


def test_int_bounds_are_enforced_on_both_ends() -> None:
    f = Field("x", "小时", kind="int", min=0, max=23)
    assert f.coerce(0) == 0
    assert f.coerce(23) == 23
    with pytest.raises(ValueError, match="不能小于"):
        f.coerce(-1)
    with pytest.raises(ValueError, match="不能大于"):
        f.coerce(24)


def test_select_only_accepts_declared_options() -> None:
    f = Field("x", "动作", kind="select", default="a",
              options=[("a", "甲"), ("b", "乙")])
    assert f.coerce("b") == "b"
    for bad in ("c", "", None):
        with pytest.raises(ValueError, match="必须是"):
            f.coerce(bad)


def test_str_and_text_are_trimmed_and_never_raise() -> None:
    for kind in ("str", "text"):
        f = Field("x", "文本", kind=kind)
        assert f.coerce("  hi  ") == "hi"
        assert f.coerce(None) == ""
        assert f.coerce(12) == "12"


def test_public_shape_carries_bounds_and_options_only_when_set() -> None:
    plain = Field("x", "文本").to_public()
    assert "min" not in plain and "options" not in plain
    rich = Field("y", "小时", kind="int", min=0, max=23).to_public()
    assert rich["min"] == 0 and rich["max"] == 23
    sel = Field("z", "动作", kind="select", options=[("a", "甲")]).to_public()
    assert sel["options"] == [{"value": "a", "label": "甲"}]


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
def test_registering_a_plugin_makes_it_visible_as_a_card() -> None:
    reg = make_registry()
    reg.register(OkPlugin(make_ctx()))
    assert reg.ids() == ["ok"]
    card = reg.card("ok")
    assert card["name"] == "正常"
    assert card["enabled"] is False
    assert [f["key"] for f in card["fields"]] == ["n", "flag"]


def test_a_duplicate_id_is_refused_rather_than_silently_replacing() -> None:
    reg = make_registry()
    reg.register(OkPlugin(make_ctx()))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(OkPlugin(make_ctx()))


def test_an_unknown_category_is_refused() -> None:
    class Weird(Plugin):
        spec = Spec(id="weird", name="?", description="", category="nonsense")

    with pytest.raises(ValueError, match="unknown category"):
        make_registry().register(Weird(make_ctx()))


def test_an_unknown_field_kind_is_refused() -> None:
    class Weird(Plugin):
        spec = Spec(id="weird", name="?", description="", category="task",
                    fields=[Field("x", "?", kind="colour")])

    with pytest.raises(ValueError, match="unknown field kind"):
        make_registry().register(Weird(make_ctx()))


def test_cards_filter_by_category() -> None:
    class Points(Plugin):
        spec = Spec(id="pts", name="积分", description="", category="points")

    reg = make_registry()
    reg.register(OkPlugin(make_ctx()))
    reg.register(Points(make_ctx()))
    assert [c["id"] for c in reg.cards("task")] == ["ok"]
    assert [c["id"] for c in reg.cards("points")] == ["pts"]
    assert len(reg.cards()) == 2


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------
def test_saving_coerces_declared_keys_and_drops_unknown_ones() -> None:
    store = FakeStore()
    reg = make_registry(store)
    reg.register(OkPlugin(make_ctx()))
    card = reg.save("ok", enabled=True, config={"n": "5", "flag": "on",
                                                "evil": "rm -rf /"})
    assert card["enabled"] is True
    assert card["config"] == {"n": 5, "flag": True}
    assert "evil" not in str(store.data["plugins"]["ok"]["config"])


def test_saving_a_bad_value_refuses_the_whole_save() -> None:
    store = FakeStore()
    reg = make_registry(store)
    reg.register(OkPlugin(make_ctx()))
    reg.save("ok", config={"n": 3})
    with pytest.raises(ValueError):
        reg.save("ok", config={"n": 99})
    # The previous good value survives: a refused save must not half-apply.
    assert reg.config("ok")["n"] == 3


def test_toggling_enabled_alone_keeps_the_stored_config() -> None:
    reg = make_registry()
    reg.register(OkPlugin(make_ctx()))
    reg.save("ok", config={"n": 4})
    reg.save("ok", enabled=True)
    assert reg.config("ok")["n"] == 4
    assert reg.enabled("ok") is True


def test_config_falls_back_to_declared_defaults() -> None:
    reg = make_registry()
    reg.register(OkPlugin(make_ctx()))
    assert reg.config("ok") == {"n": 1, "flag": False}


# ---------------------------------------------------------------------------
# run_now
# ---------------------------------------------------------------------------
def test_a_manual_run_returns_the_summary_and_records_it() -> None:
    db = FakeDb()
    reg = make_registry(db=db)
    reg.register(OkPlugin(make_ctx()))
    reg.save("ok", config={"n": 3})
    result = asyncio.run(reg.run_now("ok"))
    assert result == {"ok": True, "收到": 3}
    history = reg.history("ok")
    assert len(history) == 1
    assert history[0]["trigger"] == "manual"
    assert history[0]["summary"] == {"收到": 3}


def test_a_raising_plugin_is_recorded_as_failed_and_does_not_propagate() -> None:
    reg = make_registry()
    reg.register(BoomPlugin(make_ctx()))
    result = asyncio.run(reg.run_now("boom"))
    assert result["ok"] is False
    assert "炸了" in result["error"]
    assert reg.history("boom")[0]["ok"] == 0


def test_a_run_is_possible_even_while_disabled() -> None:
    """The button exists to try a plugin *before* switching it on."""
    reg = make_registry()
    reg.register(OkPlugin(make_ctx()))
    assert reg.enabled("ok") is False
    assert asyncio.run(reg.run_now("ok"))["ok"] is True


def test_one_plugin_failing_does_not_stop_the_others_in_a_tick() -> None:
    store = FakeStore()
    reg = make_registry(store)
    reg.register(BoomPlugin(make_ctx()))
    reg.register(OkPlugin(make_ctx()))
    reg.save("boom", enabled=True)
    reg.save("ok", enabled=True)
    assert set(asyncio.run(reg.tick())) == {"boom", "ok"}
    assert reg.last_result("ok")["ok"] == 1


def test_a_second_run_is_refused_while_the_first_is_still_going() -> None:
    class Slow(Plugin):
        spec = Spec(id="slow", name="慢", description="", category="task")

        async def run(self, config: dict[str, Any]) -> dict[str, Any]:
            await asyncio.sleep(0.05)
            return {"done": True}

    reg = make_registry()
    reg.register(Slow(make_ctx()))

    async def race() -> list[dict[str, Any]]:
        return list(await asyncio.gather(reg.run_now("slow"), reg.run_now("slow")))

    results = asyncio.run(race())
    assert sum(1 for r in results if r.get("error") == "already running") == 1


# ---------------------------------------------------------------------------
# scheduling
# ---------------------------------------------------------------------------
def test_tick_skips_a_disabled_plugin() -> None:
    reg = make_registry()
    reg.register(OkPlugin(make_ctx()))
    assert asyncio.run(reg.tick()) == []


def test_tick_runs_an_enabled_interval_plugin_then_waits_for_the_interval() -> None:
    reg = make_registry()
    reg.register(OkPlugin(make_ctx()))
    reg.save("ok", enabled=True)
    assert asyncio.run(reg.tick()) == ["ok"]
    assert asyncio.run(reg.tick()) == []  # interval has not elapsed
    reg._last_run["ok"] = time.time() - 120
    assert asyncio.run(reg.tick()) == ["ok"]


def test_a_manual_only_plugin_is_never_scheduled() -> None:
    class Manual(Plugin):
        spec = Spec(id="manual", name="手动", description="", category="task",
                    interval=0)

        async def run(self, config: dict[str, Any]) -> dict[str, Any]:
            return {}

    reg = make_registry()
    reg.register(Manual(make_ctx()))
    reg.save("manual", enabled=True)
    assert asyncio.run(reg.tick()) == []


class DailyPlugin(Plugin):
    spec = Spec(id="daily", name="每日", description="", category="task", hour=10,
                fields=[Field("hour", "时间", kind="int", default=10, min=0, max=23)])

    async def run(self, config: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}


def _at(hour: int, day: int = 1) -> float:
    return time.mktime((2026, 9, day, hour, 0, 0, 0, 0, -1))


def test_a_daily_plugin_runs_once_per_day_not_once_per_tick() -> None:
    reg = make_registry()
    reg.register(DailyPlugin(make_ctx()))
    reg.save("daily", enabled=True)
    noon = _at(12)
    assert asyncio.run(reg.tick(noon)) == ["daily"]
    assert asyncio.run(reg.tick(noon + 3600)) == []
    # Next calendar day it is due again.
    assert asyncio.run(reg.tick(_at(12, day=2))) == ["daily"]


def test_a_daily_plugin_waits_for_its_hour() -> None:
    reg = make_registry()
    reg.register(DailyPlugin(make_ctx()))
    reg.save("daily", enabled=True)
    assert asyncio.run(reg.tick(_at(9))) == []
    assert asyncio.run(reg.tick(_at(10))) == ["daily"]


def test_the_configured_hour_wins_over_the_declared_default() -> None:
    """An hour field the scheduler ignored would be pure decoration."""
    reg = make_registry()
    reg.register(DailyPlugin(make_ctx()))
    reg.save("daily", enabled=True, config={"hour": 20})
    assert asyncio.run(reg.tick(_at(10))) == []
    assert asyncio.run(reg.tick(_at(20))) == ["daily"]


def test_due_today_can_veto_a_daily_run() -> None:
    class Weekly(Plugin):
        spec = Spec(id="weekly", name="每周", description="", category="task", hour=0)
        allow = False

        async def run(self, config: dict[str, Any]) -> dict[str, Any]:
            return {}

        def due_today(self, config: dict[str, Any], now: float) -> bool:
            return self.allow

    plugin = Weekly(make_ctx())
    reg = make_registry()
    reg.register(plugin)
    reg.save("weekly", enabled=True)
    assert asyncio.run(reg.tick(_at(12))) == []
    plugin.allow = True
    assert asyncio.run(reg.tick(_at(12))) == ["weekly"]


def test_a_raising_due_today_does_not_break_the_tick() -> None:
    class Broken(Plugin):
        spec = Spec(id="broken", name="坏的", description="", category="task", hour=0)

        async def run(self, config: dict[str, Any]) -> dict[str, Any]:
            return {}

        def due_today(self, config: dict[str, Any], now: float) -> bool:
            raise RuntimeError("nope")

    reg = make_registry()
    reg.register(Broken(make_ctx()))
    reg.register(OkPlugin(make_ctx()))
    reg.save("broken", enabled=True)
    reg.save("ok", enabled=True)
    assert asyncio.run(reg.tick(_at(12))) == ["ok"]


def test_start_is_idempotent_and_stop_cleans_up() -> None:
    reg = make_registry()

    async def run() -> None:
        reg.start()
        first = reg._task
        reg.start()
        assert reg._task is first
        await reg.stop()
        assert reg._task is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# built-in plugins
# ---------------------------------------------------------------------------
def test_every_builtin_declares_a_card_with_unique_id() -> None:
    reg = register_builtin(make_registry(), make_ctx())
    assert len(reg.ids()) == len(BUILTIN_PLUGINS)
    # Every card must declare a known category and explain itself: the panel
    # renders both straight from the spec, so a blank one is a blank card.
    assert all(c["category"] in ("task", "points", "request")
               for c in reg.cards())
    assert all(c["description"] for c in reg.cards())
    # Registration hands the registry to the context. Points plugins are
    # invoked by the bot rather than the scheduler, so they have to be able to
    # read their own live config at that moment.
    assert reg.get("checkin").ctx.registry is reg


def _member(uid: str, **kwargs: Any) -> dict[str, Any]:
    row = {"emby_user_id": uid, "username": "u" + uid, "status": "active",
           "tg_user_id": "tg" + uid, "created_at": time.time()}
    row.update(kwargs)
    return row


# -- group_audit ------------------------------------------------------------
def test_group_audit_reports_without_touching_anyone() -> None:
    members = FakeMembers()
    bot = FakeBot(audit={"checked": 3, "left": [_member("1")], "unavailable": False})
    plugin = GroupAuditPlugin(make_ctx(members=members, telegram=bot))
    summary = asyncio.run(plugin.run({"action": "report", "grace_days": 3}))
    assert summary["检查人数"] == 3
    assert summary["已退群"] == 1
    assert summary["已通知"] == 0
    assert members.status_calls == []
    assert bot.notified == []


def test_group_audit_notifies_first_and_suspends_only_after_the_grace_period() -> None:
    members = FakeMembers()
    bot = FakeBot(audit={"checked": 1, "left": [_member("1")], "unavailable": False})
    ctx = make_ctx(members=members, telegram=bot)
    plugin = GroupAuditPlugin(ctx)
    config = {"action": "suspend", "grace_days": 3}

    first = asyncio.run(plugin.run(config))
    assert first["已通知"] == 1 and first["已停用"] == 0
    assert members.status_calls == []

    # Still inside the grace window: nothing happens a second time.
    assert asyncio.run(plugin.run(config))["已停用"] == 0
    assert members.status_calls == []

    # Backdate the notice past the window.
    ctx.set_state("group_audit", {"1": time.time() - 4 * 86400})
    assert asyncio.run(plugin.run(config))["已停用"] == 1
    assert members.status_calls == [("1", "suspended", "plugin:group_audit")]


def test_group_audit_forgets_someone_who_came_back() -> None:
    members = FakeMembers()
    bot = FakeBot(audit={"checked": 1, "left": [], "unavailable": False})
    ctx = make_ctx(members=members, telegram=bot)
    ctx.set_state("group_audit", {"1": time.time() - 99 * 86400})
    summary = asyncio.run(GroupAuditPlugin(ctx).run(
        {"action": "suspend", "grace_days": 0}))
    assert summary["已停用"] == 0
    assert ctx.state("group_audit") == {}


def test_group_audit_says_so_when_the_bot_is_off() -> None:
    plugin = GroupAuditPlugin(make_ctx(members=FakeMembers(),
                                       telegram=FakeBot(enabled=False)))
    summary = asyncio.run(plugin.run({"action": "report", "grace_days": 3}))
    assert summary["ok"] is False and "机器人" in summary["错误"]


def test_group_audit_says_so_when_no_group_is_configured() -> None:
    bot = FakeBot(audit={"checked": 0, "left": [], "unavailable": True})
    plugin = GroupAuditPlugin(make_ctx(members=FakeMembers(), telegram=bot))
    summary = asyncio.run(plugin.run({"action": "report", "grace_days": 3}))
    assert summary["ok"] is False and "群组" in summary["错误"]


# -- inactive_cleanup -------------------------------------------------------
def test_inactive_cleanup_ignores_members_seen_recently() -> None:
    members = FakeMembers([_member("1", last_seen_at=time.time() - 3600)])
    plugin = InactiveCleanupPlugin(make_ctx(members=members, telegram=FakeBot()))
    summary = asyncio.run(plugin.run(
        {"days": 7, "notify": True, "suspend_after_days": 3}))
    assert summary["不活跃人数"] == 0
    assert summary["已通知"] == 0


def test_inactive_cleanup_notifies_then_suspends_but_never_deletes() -> None:
    old = time.time() - 40 * 86400
    members = FakeMembers([_member("1", last_seen_at=old)])
    bot = FakeBot()
    ctx = make_ctx(members=members, telegram=bot)
    plugin = InactiveCleanupPlugin(ctx)
    config = {"days": 7, "notify": True, "suspend_after_days": 3}

    first = asyncio.run(plugin.run(config))
    assert first["不活跃人数"] == 1 and first["已通知"] == 1
    assert first["说明"] == "不会删号"
    assert members.status_calls == []

    ctx.set_state("inactive_cleanup", {"1": time.time() - 5 * 86400})
    assert asyncio.run(plugin.run(config))["已停用"] == 1
    assert members.status_calls == [("1", "suspended", "plugin:inactive_cleanup")]
    assert not hasattr(members, "deleted")


def test_inactive_cleanup_with_suspend_disabled_only_ever_warns() -> None:
    old = time.time() - 40 * 86400
    members = FakeMembers([_member("1", last_seen_at=old)])
    ctx = make_ctx(members=members, telegram=FakeBot())
    ctx.set_state("inactive_cleanup", {"1": time.time() - 90 * 86400})
    summary = asyncio.run(InactiveCleanupPlugin(ctx).run(
        {"days": 7, "notify": True, "suspend_after_days": 0}))
    assert summary["已停用"] == 0
    assert members.status_calls == []


def test_inactive_cleanup_skips_members_that_are_not_active() -> None:
    old = time.time() - 40 * 86400
    members = FakeMembers([_member("1", last_seen_at=old, status="suspended")])
    plugin = InactiveCleanupPlugin(make_ctx(members=members, telegram=FakeBot()))
    assert asyncio.run(plugin.run(
        {"days": 7, "notify": True, "suspend_after_days": 3}))["不活跃人数"] == 0


def test_inactive_cleanup_does_not_message_when_notify_is_off() -> None:
    old = time.time() - 40 * 86400
    bot = FakeBot()
    members = FakeMembers([_member("1", last_seen_at=old)])
    asyncio.run(InactiveCleanupPlugin(make_ctx(members=members, telegram=bot)).run(
        {"days": 7, "notify": False, "suspend_after_days": 3}))
    assert bot.notified == []


# -- viewing_report ---------------------------------------------------------
def test_viewing_report_sends_a_private_summary_to_linked_members() -> None:
    members = FakeMembers([_member("1")])
    bot = FakeBot()
    plugin = ViewingReportPlugin(
        make_ctx(members=members, telegram=bot, stats=FakeStats()))
    summary = asyncio.run(plugin.run({"period": "weekly", "hour": 20}))
    assert summary["已发送"] == 1
    body = bot.notified[0][1]
    assert "周报" in body and "剧集甲" in body


def test_viewing_report_skips_members_with_nothing_to_report() -> None:
    members = FakeMembers([_member("1")])
    bot = FakeBot()
    empty = FakeStats({"series": [], "recent_plays": []})
    summary = asyncio.run(ViewingReportPlugin(
        make_ctx(members=members, telegram=bot, stats=empty)).run(
            {"period": "weekly", "hour": 20}))
    assert summary["已发送"] == 0 and summary["无记录跳过"] == 1
    assert bot.notified == []


def test_viewing_report_weekly_lands_on_monday_only() -> None:
    plugin = ViewingReportPlugin(make_ctx())
    monday = time.mktime((2026, 9, 7, 20, 0, 0, 0, 0, -1))
    tuesday = time.mktime((2026, 9, 8, 20, 0, 0, 0, 0, -1))
    assert plugin.due_today({"period": "weekly"}, monday) is True
    assert plugin.due_today({"period": "weekly"}, tuesday) is False


def test_viewing_report_monthly_lands_on_the_first_only() -> None:
    plugin = ViewingReportPlugin(make_ctx())
    first = time.mktime((2026, 10, 1, 20, 0, 0, 0, 0, -1))
    second = time.mktime((2026, 10, 2, 20, 0, 0, 0, 0, -1))
    assert plugin.due_today({"period": "monthly"}, first) is True
    assert plugin.due_today({"period": "monthly"}, second) is False


def test_viewing_report_needs_a_running_bot() -> None:
    summary = asyncio.run(ViewingReportPlugin(
        make_ctx(members=FakeMembers(), telegram=FakeBot(enabled=False),
                 stats=FakeStats())).run({"period": "weekly", "hour": 20}))
    assert summary["ok"] is False


# -- rankings_post ----------------------------------------------------------
def test_rankings_post_sends_to_the_configured_chat() -> None:
    bot = FakeBot()
    summary = asyncio.run(RankingsPostPlugin(make_ctx(telegram=bot)).run(
        {"chat_id": "@somechannel", "hour": 21, "days": 1}))
    assert summary["ok"] is True
    assert bot.broadcasts == [("@somechannel", 1)]


def test_rankings_post_without_a_target_reports_instead_of_guessing() -> None:
    bot = FakeBot()
    summary = asyncio.run(RankingsPostPlugin(make_ctx(telegram=bot)).run(
        {"chat_id": "  ", "hour": 21, "days": 1}))
    assert summary["ok"] is False and bot.broadcasts == []


def test_weekly_rankings_post_on_sunday_and_not_midweek() -> None:
    plugin = RankingsWeeklyPlugin(make_ctx(telegram=FakeBot()))
    sunday = time.mktime((2026, 9, 13, 21, 0, 0, 6, 256, -1))
    wednesday = time.mktime((2026, 9, 9, 21, 0, 0, 2, 252, -1))
    assert plugin.due_today({}, sunday) is True
    assert plugin.due_today({}, wednesday) is False
    bot = FakeBot()
    summary = asyncio.run(RankingsWeeklyPlugin(make_ctx(telegram=bot)).run(
        {"chat_id": "@somechannel", "hour": 21}))
    assert summary["ok"] is True and bot.broadcasts == [("@somechannel", 7)]


def test_watch_rank_post_sends_every_viewer_board() -> None:
    bot = FakeBot()
    summary = asyncio.run(WatchRankPostPlugin(make_ctx(telegram=bot)).run(
        {"chat_id": "@somechannel", "hour": 23, "days": 1}))
    assert summary["ok"] is True
    assert bot.watch_broadcasts == [("@somechannel", 1)]
    assert bot.broadcasts == []


def test_weekly_watch_rank_on_sunday_and_not_midweek() -> None:
    plugin = WatchRankWeeklyPlugin(make_ctx(telegram=FakeBot()))
    sunday = time.mktime((2026, 9, 13, 23, 0, 0, 6, 256, -1))
    wednesday = time.mktime((2026, 9, 9, 23, 0, 0, 2, 252, -1))
    assert plugin.due_today({}, sunday) is True
    assert plugin.due_today({}, wednesday) is False
    bot = FakeBot()
    summary = asyncio.run(WatchRankWeeklyPlugin(make_ctx(telegram=bot)).run(
        {"chat_id": "@somechannel", "hour": 23}))
    assert summary["ok"] is True and bot.watch_broadcasts == [("@somechannel", 7)]


# -- expiry_reminder --------------------------------------------------------
def test_expiry_reminder_notifies_members_inside_the_window() -> None:
    soon = time.time() + 2 * 86400
    far = time.time() + 40 * 86400
    members = FakeMembers([_member("1", expires_at=soon),
                           _member("2", expires_at=far)])
    bot = FakeBot()
    summary = asyncio.run(ExpiryReminderPlugin(
        make_ctx(members=members, telegram=bot)).run(
            {"days_ahead": 3, "hour": 10}))
    assert summary["即将到期"] == 1 and summary["已通知"] == 1
    assert [m["emby_user_id"] for m in bot.expiring_calls[0]] == ["1"]


def test_expiry_reminder_with_nobody_due_sends_nothing() -> None:
    members = FakeMembers([_member("1", expires_at=time.time() + 40 * 86400)])
    bot = FakeBot()
    summary = asyncio.run(ExpiryReminderPlugin(
        make_ctx(members=members, telegram=bot)).run(
            {"days_ahead": 3, "hour": 10}))
    assert summary["即将到期"] == 0 and summary["已通知"] == 0


def test_expiry_reminder_needs_a_running_bot() -> None:
    summary = asyncio.run(ExpiryReminderPlugin(
        make_ctx(members=FakeMembers(), telegram=FakeBot(enabled=False))).run(
            {"days_ahead": 3, "hour": 10}))
    assert summary["ok"] is False


# -- request_digest ---------------------------------------------------------
def test_request_digest_tells_every_uploader_what_is_waiting() -> None:
    bot = FakeBot()
    plugin = RequestDigestPlugin(
        make_ctx(telegram=bot, requests=FakeRequests()))

    summary = asyncio.run(plugin.run({"hour": 9, "only_if_open": True}))

    assert summary["待接单"] == 2 and summary["处理中"] == 1
    assert summary["已通知"] == 2
    assert {chat for chat, _ in bot.notified} == {"801", "802"}
    assert "待接单" in bot.notified[0][1] and "/req" in bot.notified[0][1]


def test_request_digest_stays_quiet_when_the_queue_is_empty() -> None:
    """A digest that says 'nothing to do' every morning is one people stop
    reading, and then they miss the one that mattered."""
    bot = FakeBot()
    plugin = RequestDigestPlugin(make_ctx(
        telegram=bot,
        requests=FakeRequests({"open": 0, "claimed": 3, "month_total": 3})))

    summary = asyncio.run(plugin.run({"hour": 9, "only_if_open": True}))

    assert summary["ok"] is True and summary["已通知"] == 0
    assert bot.notified == []


def test_request_digest_can_be_told_to_report_even_when_idle() -> None:
    bot = FakeBot()
    plugin = RequestDigestPlugin(make_ctx(
        telegram=bot,
        requests=FakeRequests({"open": 0, "claimed": 0, "month_total": 0})))

    summary = asyncio.run(plugin.run({"hour": 9, "only_if_open": False}))

    assert summary["已通知"] == 2


def test_request_digest_needs_a_running_bot() -> None:
    plugin = RequestDigestPlugin(make_ctx(
        telegram=FakeBot(enabled=False), requests=FakeRequests()))
    summary = asyncio.run(plugin.run({"hour": 9, "only_if_open": True}))
    assert summary["ok"] is False


def test_request_digest_without_the_service_reports_rather_than_raises() -> None:
    plugin = RequestDigestPlugin(make_ctx(telegram=FakeBot(), requests=None))
    summary = asyncio.run(plugin.run({"hour": 9, "only_if_open": True}))
    assert summary["ok"] is False


def test_request_digest_skips_uploaders_with_no_chat() -> None:
    bot = FakeBot()
    plugin = RequestDigestPlugin(make_ctx(telegram=bot, requests=FakeRequests(
        uploaders=[{"emby_user_id": "up1", "tg_user_id": "801"},
                   {"emby_user_id": "up2", "tg_user_id": ""}])))

    summary = asyncio.run(plugin.run({"hour": 9, "only_if_open": True}))

    assert summary["已通知"] == 1


def test_request_digest_defaults_to_nine_in_the_morning() -> None:
    assert RequestDigestPlugin.spec.hour == 9
    defaults = {f.key: f.default for f in RequestDigestPlugin.spec.fields}
    assert defaults == {"hour": 9, "only_if_open": True}


# ---------------------------------------------------------------------------
# legacy migration
# ---------------------------------------------------------------------------
def test_legacy_telegram_schedule_moves_onto_the_cards() -> None:
    store = FakeStore({"telegram": {
        "enabled": True, "rankings_enabled": True, "rankings_chat": "@old",
        "rankings_hour": 22, "notify_expiring": True, "notify_expiring_days": 5,
    }})
    result = migrate_legacy_telegram_jobs(store)
    assert set(result["migrated"]) == {"rankings_post", "expiry_reminder"}
    plugins = store.section("plugins")
    assert plugins["rankings_post"]["enabled"] is True
    assert plugins["rankings_post"]["config"]["chat_id"] == "@old"
    assert plugins["rankings_post"]["config"]["hour"] == 22
    assert plugins["expiry_reminder"]["config"]["days_ahead"] == 5
    # The old keys are gone, so nothing can drive the same post twice.
    assert "rankings_chat" not in store.section("telegram")
    assert store.section("telegram")["enabled"] is True


def test_migration_is_idempotent_and_a_second_pass_changes_nothing() -> None:
    store = FakeStore({"telegram": {"rankings_enabled": True,
                                    "rankings_chat": "@old"}})
    migrate_legacy_telegram_jobs(store)
    before = str(store.section("plugins"))
    assert migrate_legacy_telegram_jobs(store)["migrated"] == []
    assert str(store.section("plugins")) == before


def test_migration_never_overwrites_config_the_operator_already_set() -> None:
    store = FakeStore({
        "telegram": {"rankings_enabled": True, "rankings_chat": "@old"},
        "plugins": {"rankings_post": {"enabled": False,
                                      "config": {"chat_id": "@new"}}},
    })
    migrate_legacy_telegram_jobs(store)
    assert store.section("plugins")["rankings_post"]["config"]["chat_id"] == "@new"


def test_migration_on_a_fresh_install_is_a_no_op() -> None:
    store = FakeStore()
    assert migrate_legacy_telegram_jobs(store)["migrated"] == []
    assert store.section("plugins") == {}


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
def test_plugin_endpoints_all_require_auth() -> None:
    with TestClient(app) as client:
        assert client.get("/api/plugins").status_code == 401
        assert client.get("/api/plugins/rankings_post").status_code == 401
        assert client.post("/api/plugins/rankings_post", json={}).status_code == 401
        assert client.post("/api/plugins/rankings_post/run").status_code == 401
        assert client.get("/api/plugins/rankings_post/history").status_code == 401


def test_listing_returns_every_builtin_card() -> None:
    with TestClient(app) as client:
        cards = client.get("/api/plugins?category=task", auth=ADMIN).json()
        ids = {c["id"] for c in cards}
        assert {"group_audit", "inactive_cleanup", "viewing_report",
                "rankings_post", "rankings_weekly", "watch_rank_post",
                "watch_rank_weekly", "expiry_reminder"} <= ids
        assert all("fields" in c and "config" in c for c in cards)


def test_an_unused_category_is_not_an_error_it_is_an_empty_list() -> None:
    """A category nobody registered into is empty, not a 4xx."""
    with TestClient(app) as client:
        assert client.get("/api/plugins?category=nosuchcategory",
                          auth=ADMIN).json() == []


def test_the_request_category_carries_the_digest_card() -> None:
    """The automation page grows a 求片 tab from the category alone."""
    with TestClient(app) as client:
        cards = client.get("/api/plugins?category=request", auth=ADMIN).json()
        assert {c["id"] for c in cards} == {"request_digest"}


def test_the_points_category_carries_the_points_plugins() -> None:
    """The automation page renders these from the category alone."""
    with TestClient(app) as client:
        cards = client.get("/api/plugins?category=points", auth=ADMIN).json()
        assert {c["id"] for c in cards} == {"checkin", "points_transfer"}
        # Neither is scheduled: a member triggers them, so a timer that fired
        # them would be awarding points nobody asked for.
        assert all(c["interval"] == 0 and c["hour"] is None for c in cards)
        # Off until the operator decides otherwise.
        assert all(c["enabled"] is False for c in cards)


def test_unknown_plugin_ids_answer_404_on_every_route() -> None:
    with TestClient(app) as client:
        assert client.get("/api/plugins/nope", auth=ADMIN).status_code == 404
        assert client.post("/api/plugins/nope", auth=ADMIN,
                           json={"enabled": True}).status_code == 404
        assert client.post("/api/plugins/nope/run", auth=ADMIN).status_code == 404
        assert client.get("/api/plugins/nope/history", auth=ADMIN).status_code == 404


def test_saving_persists_and_shows_up_on_the_next_read() -> None:
    with TestClient(app) as client:
        saved = client.post("/api/plugins/rankings_post", auth=ADMIN, json={
            "enabled": True, "config": {"chat_id": "@here", "hour": 9, "days": 2},
        }).json()
        assert saved["enabled"] is True
        again = client.get("/api/plugins/rankings_post", auth=ADMIN).json()
        assert again["config"]["chat_id"] == "@here"
        assert again["config"]["hour"] == 9


def test_a_bad_value_is_a_400_with_a_reason_a_person_can_read() -> None:
    with TestClient(app) as client:
        r = client.post("/api/plugins/rankings_post", auth=ADMIN,
                        json={"config": {"hour": 99}})
        assert r.status_code == 400
        assert "不能大于" in r.json()["detail"]


def test_a_non_object_config_is_refused() -> None:
    with TestClient(app) as client:
        r = client.post("/api/plugins/rankings_post", auth=ADMIN,
                        json={"config": "hour=9"})
        assert r.status_code == 400


def test_saving_is_written_to_the_audit_trail() -> None:
    with TestClient(app) as client:
        client.post("/api/plugins/expiry_reminder", auth=ADMIN,
                    json={"enabled": True})
        body = str(client.get("/api/audit?limit=20", auth=ADMIN).json())
        assert "plugin.save" in body and "expiry_reminder" in body


def test_running_by_hand_answers_200_with_the_failure_rather_than_an_error() -> None:
    """A failed run is the answer to the question the operator asked."""
    with TestClient(app) as client:
        r = client.post("/api/plugins/rankings_post/run", auth=ADMIN)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False  # no bot configured in a fresh install
        assert body["card"]["last_run"] is not None


def test_running_is_written_to_the_audit_trail_and_the_history() -> None:
    with TestClient(app) as client:
        client.post("/api/plugins/group_audit/run", auth=ADMIN)
        body = str(client.get("/api/audit?limit=20", auth=ADMIN).json())
        assert "plugin.run" in body
        history = client.get("/api/plugins/group_audit/history?limit=5",
                             auth=ADMIN).json()
        assert len(history) == 1
        assert history[0]["trigger"] == "manual"


def test_history_is_empty_before_anything_has_run() -> None:
    with TestClient(app) as client:
        assert client.get("/api/plugins/viewing_report/history",
                          auth=ADMIN).json() == []


# ---------------------------------------------------------------------------
# front end wiring
# ---------------------------------------------------------------------------
# The nav is a hand-maintained list and the pages are a hand-maintained map.
# When they drift, the entry still renders and clicking it says 页面不存在 --
# which looks like a broken deployment rather than a missing line of code.
_STATIC = FilePath(__file__).resolve().parents[1] / "app" / "static"


#: Every script index.html loads. A page defined in a file missing from this
#: list would look absent to the nav guard below, so the list is derived from
#: the document rather than repeated by hand.
def _panel_scripts() -> list[str]:
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    return re.findall(r'src="/static/([A-Za-z0-9_.-]+\.js)"', html)


def _panel_source() -> str:
    return "".join((_STATIC / name).read_text(encoding="utf-8")
                   for name in _panel_scripts())


def _nav_ids() -> list[str]:
    app_js = (_STATIC / "app.js").read_text(encoding="utf-8")
    nav = app_js.split("const NAV = [", 1)[1].split("\n];", 1)[0]
    # Capture to the closing quote, not a character class: a class that stops
    # early matches a *prefix* of the id, so a renamed page would still find
    # the old handler and the check would pass while the nav is broken.
    return re.findall(r"\{\s*id:\s*'([^']+)'", nav)


def test_every_nav_entry_has_a_page_that_renders_it() -> None:
    source = _panel_source()
    missing = [nid for nid in _nav_ids() if f"PAGES.{nid} =" not in source]
    assert not missing, f"nav entries without a PAGES handler: {missing}"


def test_the_new_sections_are_actually_in_the_nav() -> None:
    ids = _nav_ids()
    for expected in ("automation", "access", "sharing", "requests"):
        assert expected in ids, expected


def test_the_request_page_is_reachable_from_the_operations_group() -> None:
    """A page with no nav entry is a page nobody finds."""
    app_js = (_STATIC / "app.js").read_text(encoding="utf-8")
    assert "id: 'requests'" in app_js
    assert "PAGES.requests =" in _panel_source()


def test_only_one_telegram_form_owns_the_credential_field() -> None:
    """Two forms saving the same settings overwrite each other's values.

    The settings page used to carry a second copy of the bot form; whichever
    page was saved last won, silently reverting the other.
    """
    app_js = (_STATIC / "app.js").read_text(encoding="utf-8")
    assert "id=\"tg-token\"" not in app_js
    assert "telegramPayload" not in app_js
    ops_js = (_STATIC / "ops.js").read_text(encoding="utf-8")
    assert ops_js.count("id=\"tg-token\"") == 1


def test_bot_connection_test_is_explicit_and_does_not_save_other_sections() -> None:
    """Testing saved credentials must not implicitly save drafts or enable the bot."""
    workspace = (_STATIC / 'workspace.js').read_text(encoding='utf-8')
    verify = workspace.split("$('#tg-test').onclick = async () => {", 1)[1].split('\n  };', 1)[0]
    assert '/api/settings/telegram/verify' in verify
    assert 'enabled: true' not in verify and 'telegramPagePayload' not in verify
    assert '启用状态未改变' in verify


def test_the_migrated_scheduling_fields_are_gone_from_the_bot_page() -> None:
    ops_js = (_STATIC / "ops.js").read_text(encoding="utf-8")
    for gone in ("tg-rankchat", "tg-rankhour", "tg-rank\"", "tg-notify", "tg-days"):
        assert gone not in ops_js, gone
