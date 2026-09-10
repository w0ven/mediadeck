"""Plugin framework: one mechanism for scheduled tasks and points features.

Everything that runs on a timer or hands out points is a plugin. A plugin is a
Python class that declares what it is, what it can be configured with, and how
to run; the panel renders every registered plugin as a card from that
declaration alone. Adding a feature means adding a file, not touching the
front end.

Why one framework for two kinds of thing: a "check group membership every hour"
task and a "daily check-in for points" feature look different to a user but
identical to the panel. Both have a switch, a handful of settings, a last
result and a way to trigger them by hand. Giving them separate code paths
would mean maintaining two card renderers that drift apart.

Configuration lives in the settings store under ``plugins.<id>`` so it
survives upgrades like every other setting. Run history lives in SQLite,
because it is append-heavy and the settings document is rewritten in full on
every save.

A plugin that raises must never take the scheduler down with it. Each run is
isolated; the failure is recorded on that plugin's card and the loop moves on.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

FIELD_KINDS = ("bool", "int", "str", "select", "text")
CATEGORIES = ("task", "points", "request")

# A run that has not reported back in this long is treated as hung, so a stuck
# plugin cannot hold its own lock forever and silently stop scheduling.
RUN_TIMEOUT = 600
RETRY_INTERVAL = 60


@dataclass
class Field:
    """One configurable setting, declared by the plugin, rendered by the panel."""

    key: str
    label: str
    kind: str = "str"
    default: Any = ""
    help: str = ""
    options: list[tuple[str, str]] = field(default_factory=list)  # for select
    min: int | None = None
    max: int | None = None

    def coerce(self, raw: Any) -> Any:
        """Turn a form value into the declared type, or raise ValueError."""
        if self.kind == "bool":
            if isinstance(raw, str):
                return raw.strip().lower() in ("1", "true", "on", "yes")
            return bool(raw)
        if self.kind == "int":
            try:
                value = int(raw)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"{self.label} 必须是整数") from None
            if isinstance(raw, float) and raw != value:
                raise ValueError(f"{self.label} 必须是整数")
            if self.min is not None and value < self.min:
                raise ValueError(f"{self.label} 不能小于 {self.min}")
            if self.max is not None and value > self.max:
                raise ValueError(f"{self.label} 不能大于 {self.max}")
            return value
        if self.kind == "select":
            value = str(raw or "")
            allowed = [o[0] for o in self.options]
            if value not in allowed:
                raise ValueError(f"{self.label} 必须是 {'/'.join(allowed)} 之一")
            return value
        return str(raw or "").strip()

    def to_public(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key, "label": self.label, "kind": self.kind,
            "default": self.default, "help": self.help,
        }
        if self.options:
            out["options"] = [{"value": v, "label": lbl} for v, lbl in self.options]
        if self.min is not None:
            out["min"] = self.min
        if self.max is not None:
            out["max"] = self.max
        return out


@dataclass
class Spec:
    """What a plugin is. Declared once; everything else derives from it."""

    id: str
    name: str
    description: str
    category: str = "task"
    fields: list[Field] = field(default_factory=list)
    # Seconds between scheduled runs. 0 = never scheduled, run by hand only.
    interval: int = 0
    # Optional: only run when the local hour equals this (daily jobs).
    hour: int | None = None
    icon: str = "⚙"
    hidden: bool = False
    background: bool = False


class Plugin:
    """Base class. Subclasses declare ``spec`` and implement ``run``."""

    spec: Spec

    def __init__(self, ctx: Any) -> None:
        # ctx exposes the services a plugin may need (members, emby, telegram,
        # stats, db, settings). Plugins take what they use; nothing is global.
        self.ctx = ctx

    async def run(self, config: dict[str, Any]) -> dict[str, Any]:
        """Do the work. Return a small dict summarising what happened.

        The summary is shown on the card verbatim, so keep it to a handful of
        keys a person can read: counts, names, a one-line message.
        """
        raise NotImplementedError

    def defaults(self) -> dict[str, Any]:
        return {f.key: f.default for f in self.spec.fields}

    def due_today(self, config: dict[str, Any], now: float) -> bool:
        """Extra calendar gate for *scheduled* runs only.

        Daily scheduling covers "once a day after hour N", which is all most
        plugins need. A weekly or monthly job needs one more condition, and
        putting it here keeps that knowledge in the plugin instead of teaching
        the scheduler about every possible calendar.

        Manual runs deliberately ignore it: the button exists to try a plugin
        now, and refusing because it is Wednesday would make it untestable.
        """
        return True


class PluginRegistry:
    """Holds plugins, their config, their run history and the scheduler."""

    def __init__(self, store: Any, db: Any) -> None:
        self._store = store
        self._db = db
        self._plugins: dict[str, Plugin] = {}
        self._running: set[str] = set()
        self._active: set[asyncio.Task] = set()
        self._task: asyncio.Task | None = None
        self._last_run: dict[str, float] = {}
        self._last_daily: dict[str, str] = {}
        self._last_retry: dict[str, float] = {}

    # -- registration --------------------------------------------------------

    def register(self, plugin: Plugin) -> None:
        spec = plugin.spec
        if spec.category not in CATEGORIES:
            raise ValueError(f"unknown category {spec.category!r} for {spec.id}")
        if spec.id in self._plugins:
            raise ValueError(f"plugin id already registered: {spec.id}")
        for f in spec.fields:
            if f.kind not in FIELD_KINDS:
                raise ValueError(f"{spec.id}.{f.key}: unknown field kind {f.kind!r}")
        self._plugins[spec.id] = plugin
        # Completed runs already have durable history; restore scheduling from
        # that history rather than treating every process start as a new day.
        last = self.last_result(spec.id)
        if last:
            self._last_run[spec.id] = float(last["started_at"])
            if last.get("trigger") == "schedule" and not last.get("ok"):
                self._last_retry[spec.id] = float(last["started_at"])
        with contextlib.suppress(Exception):
            daily = self._db.one(
                "SELECT * FROM plugin_runs WHERE plugin_id=? AND ok=1 "
                "AND trigger='schedule' ORDER BY started_at DESC, id DESC LIMIT 1",
                (spec.id,))
            if daily and daily["ok"] and daily["trigger"] == "schedule":
                self._last_daily[spec.id] = time.strftime(
                    "%Y-%m-%d", time.localtime(float(daily["started_at"])))

    def get(self, plugin_id: str) -> Plugin | None:
        return self._plugins.get(plugin_id)

    def ids(self) -> list[str]:
        return list(self._plugins)

    # -- config ----------------------------------------------------------------

    def _section(self) -> dict[str, Any]:
        return dict(self._store.section("plugins") or {})

    def config(self, plugin_id: str) -> dict[str, Any]:
        """Effective config: declared defaults overlaid with what was saved."""
        plugin = self._plugins[plugin_id]
        stored = self._section().get(plugin_id) or {}
        merged = plugin.defaults()
        merged.update(stored.get("config") or {})
        return merged

    def enabled(self, plugin_id: str) -> bool:
        stored = self._section().get(plugin_id) or {}
        return bool(stored.get("enabled", False))

    def save(self, plugin_id: str, enabled: bool | None = None,
             config: dict[str, Any] | None = None) -> dict[str, Any]:
        plugin = self._plugins[plugin_id]
        section = self._section()
        entry = dict(section.get(plugin_id) or {})
        if enabled is not None:
            entry["enabled"] = bool(enabled)

        if config is not None:
            current = self.config(plugin_id)
            by_key = {f.key: f for f in plugin.spec.fields}
            cleaned: dict[str, Any] = {}
            for key, raw in config.items():
                if key not in by_key:
                    continue  # unknown keys are dropped, never stored
                cleaned[key] = by_key[key].coerce(raw)
            current.update(cleaned)
            entry["config"] = current

        section[plugin_id] = entry
        self._store.set_section("plugins", section)
        return self.card(plugin_id)

    # -- run history -----------------------------------------------------------

    def _record(self, plugin_id: str, ok: bool, summary: dict[str, Any],
                started: float, trigger: str) -> None:
        encoded = json.dumps(summary, ensure_ascii=False, default=str)
        if len(encoded) > 4000:
            encoded = json.dumps({"truncated": True, "preview": encoded[:3000]},
                                 ensure_ascii=False)
        with contextlib.suppress(Exception):
            self._db.execute(
                "INSERT INTO plugin_runs(plugin_id,ok,summary,started_at,"
                "duration_ms,trigger) VALUES(?,?,?,?,?,?)",
                (plugin_id, 1 if ok else 0,
                 encoded,
                 int(started), int((time.time() - started) * 1000), trigger))

    def last_result(self, plugin_id: str) -> dict[str, Any] | None:
        row = None
        with contextlib.suppress(Exception):
            row = self._db.one(
                "SELECT * FROM plugin_runs WHERE plugin_id=? "
                "ORDER BY started_at DESC, id DESC LIMIT 1", (plugin_id,))
        if not row:
            return None
        out = dict(row)
        with contextlib.suppress(Exception):
            out["summary"] = json.loads(out.get("summary") or "{}")
        return out

    def history(self, plugin_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = []
        with contextlib.suppress(Exception):
            rows = self._db.query(
                "SELECT * FROM plugin_runs WHERE plugin_id=? "
                "ORDER BY started_at DESC, id DESC LIMIT ?",
                (plugin_id, max(1, min(limit, 200))))
        out = []
        for r in rows:
            entry = dict(r)
            with contextlib.suppress(Exception):
                entry["summary"] = json.loads(entry.get("summary") or "{}")
            out.append(entry)
        return out

    # -- cards -----------------------------------------------------------------

    def card(self, plugin_id: str) -> dict[str, Any]:
        """Everything the panel needs to draw one plugin, in one payload."""
        plugin = self._plugins[plugin_id]
        spec = plugin.spec
        last = self.last_result(plugin_id)
        return {
            "id": spec.id,
            "name": spec.name,
            "description": spec.description,
            "category": spec.category,
            "icon": spec.icon,
            "enabled": self.enabled(plugin_id),
            "fields": [f.to_public() for f in spec.fields],
            "config": self.config(plugin_id),
            "interval": spec.interval,
            "hour": spec.hour,
            "hidden": bool(spec.hidden),
            "running": plugin_id in self._running,
            "last_run": last,
        }

    def cards(self, category: str | None = None) -> list[dict[str, Any]]:
        out = []
        for pid, plugin in self._plugins.items():
            if category and plugin.spec.category != category:
                continue
            if plugin.spec.hidden:
                continue
            out.append(self.card(pid))
        return out

    # -- execution -------------------------------------------------------------

    async def run_now(self, plugin_id: str, trigger: str = "manual") -> dict[str, Any]:
        """Run one plugin regardless of schedule or enabled state.

        Enabled-state is deliberately ignored for manual runs: the point of the
        button is to try a plugin before switching it on.
        """
        plugin = self._plugins[plugin_id]
        if plugin_id in self._running:
            return {"ok": False, "error": "already running"}
        started = time.time()
        if trigger == "manual" and plugin.spec.background:
            self._running.add(plugin_id)
            asyncio.create_task(self._execute(plugin_id, trigger, started))
            return {"ok": True, "结果": "已开始后台发送，完成后写入运行历史"}
        return await self._execute(plugin_id, trigger, started)

    async def _execute(self, plugin_id: str, trigger: str, started: float) -> dict[str, Any]:
        plugin = self._plugins[plugin_id]
        self._running.add(plugin_id)
        owner = asyncio.current_task()
        if owner is not None:
            self._active.add(owner)
        try:
            summary = await asyncio.wait_for(
                plugin.run(self.config(plugin_id)), timeout=RUN_TIMEOUT)
            summary = dict(summary or {})
            ok = bool(summary.pop("ok", True))
        except asyncio.CancelledError:
            self._record(plugin_id, False, {"error": "cancelled"}, started, trigger)
            raise
        except TimeoutError:
            ok, summary = False, {"error": f"超过 {RUN_TIMEOUT}s 未完成"}
        except (httpx.HTTPError, OSError) as exc:
            # Transport exception strings include full URLs, query credentials,
            # or local paths. Keep the failure type, never that raw payload.
            ok, summary = False, {"error": type(exc).__name__}
        except Exception as exc:  # noqa: BLE001 - shown on the card, never raised
            ok, summary = False, {"error": f"{type(exc).__name__}: {exc}"[:300]}
        finally:
            self._running.discard(plugin_id)
            if owner is not None:
                self._active.discard(owner)
        self._last_run[plugin_id] = started
        self._record(plugin_id, ok, summary, started, trigger)
        return {"ok": ok, **summary}

    def _daily_hour(self, plugin_id: str) -> int:
        """The hour a daily job actually runs at.

        A plugin that declares an ``hour`` field is offering to let the
        operator move the time. Scheduling on ``spec.hour`` regardless would
        leave that field as decoration: the card would say 20:00 and the job
        would keep firing at the hard-coded default.
        """
        spec = self._plugins[plugin_id].spec
        hour = int(spec.hour or 0)
        if not any(f.key == "hour" for f in spec.fields):
            return hour
        with contextlib.suppress(Exception):
            hour = max(0, min(23, int(self.config(plugin_id).get("hour", hour))))
        return hour

    def _daily_minute(self, plugin_id: str) -> int:
        """Minute offset within the daily hour, mirroring _daily_hour."""
        spec = self._plugins[plugin_id].spec
        minute = 0
        if not any(f.key == "minute" for f in spec.fields):
            return minute
        with contextlib.suppress(Exception):
            minute = max(0, min(59, int(self.config(plugin_id).get("minute", minute))))
        return minute

    def _due(self, plugin_id: str, now: float) -> bool:
        plugin = self._plugins[plugin_id]
        spec = plugin.spec
        if not self.enabled(plugin_id) or plugin_id in self._running:
            return False
        if spec.hour is not None:
            retry = self._last_retry.get(plugin_id)
            if retry is not None and now - retry < RETRY_INTERVAL:
                return False
            # Daily job: once per calendar day, at or after the given time.
            today = time.strftime("%Y-%m-%d", time.localtime(now))
            if self._last_daily.get(plugin_id) == today:
                return False
            local = time.localtime(now)
            if (local.tm_hour, local.tm_min) < (self._daily_hour(plugin_id),
                                                self._daily_minute(plugin_id)):
                return False
            try:
                return bool(plugin.due_today(self.config(plugin_id), now))
            except Exception:  # noqa: BLE001 - a broken gate must not stop the loop
                return False
        if spec.interval <= 0:
            return False
        last = self._last_run.get(plugin_id, 0.0)
        return now - last >= spec.interval

    async def tick(self, now: float | None = None) -> list[str]:
        """Run whatever is due. Returns the ids that ran."""
        now = time.time() if now is None else now
        async def run_due(pid: str) -> str | None:
            # Recheck inside each task: overlapping ticks/manual runs may have
            # claimed the plugin after this tick was constructed.
            if not self._due(pid, now):
                return None
            result = await self.run_now(pid, trigger="schedule")
            if self._plugins[pid].spec.hour is not None:
                if result.get("ok"):
                    self._last_daily[pid] = time.strftime("%Y-%m-%d", time.localtime(now))
                    self._last_retry.pop(pid, None)
                else:
                    self._last_retry[pid] = now
            return pid

        results = await asyncio.gather(*(run_due(pid) for pid in list(self._plugins)))
        return [pid for pid in results if pid is not None]

    async def _loop(self) -> None:
        while True:
            # Tick on the wall-clock minute so minute-level schedules fire on
            # the minute instead of drifting with loop overhead.
            await asyncio.sleep(60 - (time.time() % 60))
            with contextlib.suppress(Exception):
                await self.tick()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        tasks = set(self._active)
        if self._task is not None:
            tasks.add(self._task)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
