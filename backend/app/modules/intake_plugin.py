"""Background collection for the intake pipeline page.

Why a plugin and not an on-demand read
--------------------------------------
Building the snapshot walks several thousand files, tails a large log and
makes three calls to the media server. At a few hundred milliseconds that is
fine once a minute and wrong on every page load: the page auto-refreshes, and
an operator watching an incident would put that cost on the media server
exactly when it is already struggling.

So collection runs on the plugin scheduler like every other periodic job, and
the API serves the last completed snapshot with its age attached. A stale
number labelled with its age is honest; a fresh number that costs a live
system more than it is worth is not.

The snapshot is held in memory. It is a view of state that lives elsewhere, so
persisting it would only create a second copy to go stale — after a restart
the next tick rebuilds it from the source of truth.
"""
from __future__ import annotations

import asyncio
import copy
import time
from typing import Any

from app.modules.intake import (
    DEFAULT_THRESHOLDS,
    FsReader,
    IntakeCollector,
    IntakePaths,
    unavailable,
)
from app.modules.plugins import Field, Plugin, Spec

# Serving a snapshot older than this without comment would be misleading: the
# page is read during incidents, when "two minutes ago" and "an hour ago" mean
# very different things.
STALE_AFTER = 180.0


class IntakeStore:
    """Holds the newest snapshot and hands out copies with an age attached."""

    def __init__(self) -> None:
        self._snapshot: dict[str, Any] | None = None
        self._taken_at: float = 0.0
        self._last_error: str = ""

    def put(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = copy.deepcopy(snapshot)
        self._taken_at = time.time()
        self._last_error = ""

    def fail(self, error: str) -> None:
        """Record a failed collection without discarding the last good one.

        The previous snapshot stays visible with its true age; dropping it
        would replace real (if old) information with nothing at the moment it
        is most needed.
        """
        self._last_error = error[:200]

    def get(self) -> dict[str, Any]:
        if self._snapshot is None:
            out = unavailable("尚未采集，请稍候")
            if self._last_error:
                out["error"] = self._last_error
            return out
        age = time.time() - self._taken_at
        out = {
            "available": True,
            "snapshot_age_seconds": round(age, 1),
            "stale": age > STALE_AFTER,
            "data": copy.deepcopy(self._snapshot),
        }
        if self._last_error:
            out["error"] = self._last_error
        return out


class IntakePipelinePlugin(Plugin):
    """Collects the intake snapshot on a timer."""

    spec = Spec(
        id="intake_pipeline",
        name="入库流水线采集",
        description="定时采集扫描进度、刷新队列、通知积压、上传通道与拉取状态，供「入库流水线」页面展示。",
        category="task",
        interval=60,
        icon="⇉",
        fields=[
            Field(key="stale_intake_minutes", label="入库停滞判定（分钟）",
                  kind="int", default=int(DEFAULT_THRESHOLDS["stale_intake_minutes"]),
                  min=5, max=1440,
                  help="超过该时长没有新入库，且存在待发通知时判红灯"),
            Field(key="probe_hotspot_ratio_percent", label="探测集中度阈值（%）",
                  kind="int",
                  default=int(DEFAULT_THRESHOLDS["probe_hotspot_ratio"] * 100),
                  min=10, max=100,
                  help="单个目录占最近探测记录的比例超过该值时判红灯"),
            Field(key="refresh_age_warn_hours", label="刷新队列积压告警（小时）",
                  kind="int",
                  default=int(DEFAULT_THRESHOLDS["refresh_age_warn_hours"]),
                  min=1, max=72,
                  help="队列中最老条目超过该时长时判黄灯"),
        ],
    )

    def thresholds(self, config: dict[str, Any]) -> dict[str, float]:
        def number(key: str, fallback: float) -> float:
            try:
                return float(config.get(key, fallback))
            except (TypeError, ValueError):
                return fallback

        return {
            "stale_intake_minutes": number(
                "stale_intake_minutes", DEFAULT_THRESHOLDS["stale_intake_minutes"]),
            "probe_hotspot_ratio": number(
                "probe_hotspot_ratio_percent",
                DEFAULT_THRESHOLDS["probe_hotspot_ratio"] * 100) / 100.0,
            "refresh_age_warn_hours": number(
                "refresh_age_warn_hours",
                DEFAULT_THRESHOLDS["refresh_age_warn_hours"]),
        }

    async def run(self, config: dict[str, Any]) -> dict[str, Any]:
        store = getattr(self.ctx, "intake_store", None)
        if store is None:
            return {"ok": False, "error": "采集存储未初始化"}
        collector = IntakeCollector(
            paths=getattr(self.ctx, "intake_paths", None) or IntakePaths(),
            fs=getattr(self.ctx, "intake_fs", None) or FsReader(),
            emby=getattr(self.ctx, "intake_emby", None),
            thresholds=self.thresholds(config),
        )
        downloaders = list(getattr(self.ctx, "intake_downloaders", None) or [])
        try:
            snapshot = await collector.snapshot(downloaders)
        except asyncio.CancelledError:
            store.fail("collection cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - reported on the card
            # Filesystem/HTTP exceptions can contain credentials or full URLs.
            error = f"collection failed: {type(exc).__name__}"
            store.fail(error)
            return {"ok": False, "error": error}
        store.put(snapshot)
        health = snapshot.get("health") or {}
        return {
            "健康": health.get("level", "?"),
            "告警": len(health.get("alerts") or []),
            "耗时毫秒": snapshot.get("collect_ms"),
        }
