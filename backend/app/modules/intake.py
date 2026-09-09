"""Intake pipeline observability — one screen for "why is nothing arriving?".

The question this answers
-------------------------
A file becomes watchable only after a chain of independent steps: something
downloads it, something moves it into a staging area, an uploader pushes it to
cloud storage, a refresh queue tells the media server it exists, and finally a
notifier announces it. Every one of those steps is a separate process with its
own state directory, and none of them can see the others.

So when nothing shows up, the operator has no single place to look. The
failure could be an empty download queue (nothing to do — fine), a stalled
upload lane, a refresh queue that was suppressed hours ago and never resumed,
or a media server stuck re-probing the same directory in a loop. Answering
that meant opening shells on several machines and reading logs by hand, which
is slow at the exact moment speed matters.

This module reads all of those signals and reduces them to a handful of cards
plus a red/amber/green verdict.

Design constraints
------------------
**Every source is optional and every source is injected.** Paths are
configuration, not constants — the repository must never contain a real
deployment's layout. A missing directory, a truncated JSON file, a log in an
unexpected format and an unreachable media server are all *normal*: this page
exists to be read during an incident, which is exactly when parts of the
system are broken. Each section degrades to "unknown" on its own; nothing here
may raise.

**Reads only.** Nothing in this module writes, deletes, cancels a task or
touches a queue. It is a window, not a control.

**Bounded work.** The queue directories hold thousands of files and the media
server log is hundreds of megabytes. Every scan has an explicit cap, so a
snapshot costs the same on a healthy system and a badly backed-up one.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Bounds. Chosen so one collection stays cheap on a backed-up system: the
# queue directories have held five figures of files, and the media server log
# is hundreds of MB.
# ---------------------------------------------------------------------------
MAX_QUEUE_FILES = 4000       # queue entries stat-ed per collection
MAX_QUEUE_PARSE = 400        # of those, how many are opened and parsed
#: Directories that are counted but never parsed use this much higher cap.
#: Counting is one cheap syscall per entry, so the limit only exists to bound
#: a pathological directory -- and a cap that truncates turns a derived count
#: (claims minus receipts) into a fabricated number, which is worse than no
#: number at all. Measured against a real deployment: ~8k entries per side.
MAX_COUNT_FILES = 200_000
MAX_LOG_TAIL_BYTES = 512_000  # how much of the media-server log tail is read
MAX_PROBE_LINES = 300        # probe lines aggregated (per the owner's spec)
MAX_NOTIFY_TAIL_BYTES = 64_000
MAX_LANE_FILES = 20_000      # files walked when sizing upload lanes
MAX_STATE_BYTES = 4 * 1024 * 1024
TOP_N = 5                    # rows in every "top offenders" list


# ---------------------------------------------------------------------------
# Health thresholds. Configurable because the right value depends on how fast
# the deployment's pipeline normally runs, not on anything intrinsic.
# ---------------------------------------------------------------------------
DEFAULT_THRESHOLDS: dict[str, float] = {
    # No new library item for this long *and* notifications waiting = red.
    # Both halves matter: a quiet night with an empty queue is not a fault,
    # and pending notifications with fresh arrivals is just work in progress.
    "stale_intake_minutes": 90,
    # One directory dominating the probe log means the media server is
    # looping over it instead of making progress.
    "probe_hotspot_ratio": 0.5,
    # A refresh queue entry older than this has stopped being drained.
    "refresh_age_warn_hours": 6,
}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def unavailable(reason: str) -> dict[str, Any]:
    """A section that could not be read.

    Returned instead of raising, and instead of an empty-but-successful
    result: "no data" and "could not look" lead to opposite conclusions, and
    conflating them is how a broken collector reads as a healthy pipeline.
    """
    return {"available": False, "reason": reason}


# ---------------------------------------------------------------------------
# Readers. Injected so tests can drive every branch from a tmp directory,
# and so no production path is ever compiled into the package.
# ---------------------------------------------------------------------------
@dataclass
class IntakePaths:
    """Filesystem locations of the pipeline's state. All optional."""

    refresh_queue_dir: str = ""
    refresh_sent_dir: str = ""
    refresh_suppress_file: str = ""
    notify_pending_dir: str = ""
    notify_log: str = ""
    upload_lane_root: str = ""
    staging_dir: str = ""
    local_fallback_dir: str = ""
    quarantine_dir: str = ""
    upload_state_dir: str = ""
    cloud_claims_dir: str = ""
    cloud_done_dir: str = ""
    cloud_pending_dir: str = ""
    cloud_events_dir: str = ""
    cloud_backlog_file: str = ""
    cloud_queue_file: str = ""
    cloud_active_file: str = ""

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> IntakePaths:
        data = data or {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: str(v or "") for k, v in data.items() if k in known})


@dataclass
class FsReader:
    """Filesystem access, narrowed to what this module needs.

    A class rather than bare functions so a test can supply a temporary root
    and so the production path list stays data, never code.
    """

    def exists(self, path: str) -> bool:
        if not path:
            return False
        try:
            return Path(path).exists()
        except OSError:
            return False

    def count_names(self, path: str, suffix: str = "",
                    limit: int | None = None) -> tuple[set[str], bool] | None:
        """Entry names in a directory, and whether the cap was hit.

        Separate from :meth:`listdir` because the two have different failure
        modes. Truncating a *display* list drops rows nobody misses;
        truncating a set that a count is *derived* from (claims that have no
        receipt) invents work that does not exist -- observed live as "2103
        outstanding" against a true figure of five.
        """
        if not path:
            return None
        # Resolved here rather than as a default argument so the cap stays a
        # single module-level knob, adjustable in tests.
        cap = MAX_COUNT_FILES if limit is None else limit
        try:
            base = Path(path)
            if not base.is_dir():
                return None
            names: set[str] = set()
            for entry in base.iterdir():
                name = entry.name
                if suffix and not name.endswith(suffix):
                    continue
                names.add(name)
                if len(names) >= cap:
                    return names, True
            return names, False
        except OSError:
            return None

    def listdir(self, path: str, limit: int = MAX_QUEUE_FILES) -> list[Path] | None:
        """Entries in a directory, or None if it cannot be listed."""
        if not path:
            return None
        try:
            base = Path(path)
            if not base.is_dir():
                return None
            out: list[Path] = []
            for entry in base.iterdir():
                out.append(entry)
                if len(out) >= limit:
                    break
            return out
        except OSError:
            return None

    def read_json(self, path: str | Path) -> Any:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read(MAX_STATE_BYTES + 1)
                return json.loads(text) if len(text) <= MAX_STATE_BYTES else None
        except (OSError, ValueError):
            return None

    def read_text(self, path: str | Path) -> str | None:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return None

    def tail(self, path: str | Path, max_bytes: int) -> str | None:
        """Last ``max_bytes`` of a file, decoded leniently.

        The first (probably partial) line is dropped so a half-read record
        cannot be parsed into a wrong value.
        """
        try:
            with open(path, "rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - max_bytes))
                if size > max_bytes:
                    handle.readline(max_bytes)
                return handle.read(max_bytes).decode("utf-8", errors="replace")
        except OSError:
            return None

    def mtime(self, path: str | Path) -> float | None:
        try:
            return Path(path).stat().st_mtime
        except OSError:
            return None

    def walk_files(self, path: str, limit: int = MAX_LANE_FILES
                   ) -> tuple[int, int] | None:
        """(file count, total bytes) beneath a directory, bounded.

        Zero-byte placeholder files are excluded: lane directories are
        pre-created with markers to fix ownership, and counting those as
        queued work makes an idle lane look busy.
        """
        if not path:
            return None
        base = Path(path)
        try:
            if not base.is_dir():
                return None
        except OSError:
            return None
        count = 0
        total = 0
        stack = [base]
        seen_dirs: set[tuple[int, int]] = set()
        seen = 0
        while stack:
            current = stack.pop()
            try:
                stat = current.stat()
                identity = (stat.st_dev, stat.st_ino)
                if identity in seen_dirs:
                    continue  # avoid symlink cycles without discarding linked files
                seen_dirs.add(identity)
                with os.scandir(current) as entries:
                    for entry in entries:
                        seen += 1
                        if seen > limit:
                            return None  # a partial walk cannot assert an exact total
                        if entry.is_dir():
                            stack.append(Path(entry.path))
                            continue
                        size = entry.stat().st_size
                        if size <= 0:
                            continue
                        count += 1
                        total += size
            except OSError:
                return None
        return count, total


HttpGetter = Callable[[str, dict[str, str]], Any]


# ---------------------------------------------------------------------------
# Media-server log parsing
# ---------------------------------------------------------------------------
# Matches the media probe lines the server writes when it inspects a file.
# Anchored on the quoted path because everything before it varies by version.
PROBE_RE = re.compile(r"ProcessRun\s+'ffprobe'\s+Execute:.*?file:\"([^\"]+)\"")


def probe_group(path: str, depth: int = 2) -> str:
    """Group a probed file by its first ``depth`` directory levels.

    Aggregating by directory rather than by file is the whole point: a server
    working normally probes many files under many directories, while one stuck
    in a loop probes the same handful over and over. The former spreads out,
    the latter concentrates — and only the grouped view makes that visible.
    """
    parts = [p for p in str(path or "").split("/") if p]
    if not parts:
        return ""
    return "/" + "/".join(parts[:depth])


def parse_probe_hotspots(log_text: str, limit: int = MAX_PROBE_LINES,
                         depth: int = 2) -> dict[str, Any]:
    """Directory concentration among the most recent probe lines."""
    if not log_text:
        return {"available": False, "reason": "日志为空", "samples": 0,
                "groups": [], "top_ratio": 0.0, "top_group": ""}
    matches = PROBE_RE.findall(log_text)
    if not matches:
        return {"available": False, "reason": "日志中没有匹配的探测记录",
                "samples": 0, "groups": [], "top_ratio": 0.0, "top_group": ""}
    recent = matches[-limit:]
    counts: dict[str, int] = {}
    for item in recent:
        key = probe_group(item, depth) or "(unknown)"
        counts[key] = counts.get(key, 0) + 1
    total = len(recent)
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    groups = [{"path": name, "count": n, "ratio": round(n / total, 3)}
              for name, n in ordered[:TOP_N]]
    return {
        "available": True,
        "samples": total,
        "groups": groups,
        "top_group": groups[0]["path"] if groups else "",
        "top_ratio": groups[0]["ratio"] if groups else 0.0,
    }


NOTIFY_SENT_MARKERS = ("sent final=removed", "sent and removed")
# Leading "[YYYY-MM-DD HH:MM:SS]" stamp written by the notifier.
NOTIFY_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")


def parse_notify_tail(text: str) -> dict[str, Any]:
    """Most recent delivered notification from the notifier's log tail.

    Only lines carrying a completion marker count. A log full of retry chatter
    with no completion line means nothing has actually been delivered, and
    reporting the newest line regardless would hide precisely that.
    """
    if not text:
        return {"available": False, "reason": "日志为空"}
    for line in reversed(text.splitlines()):
        if not any(marker in line for marker in NOTIFY_SENT_MARKERS):
            continue
        match = NOTIFY_TS_RE.match(line.strip())
        ts = None
        if match:
            stamp = match.group(1).replace("T", " ")
            try:
                ts = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                ts = None
        return {"available": True, "line": line.strip()[:300], "ts": ts,
                "age_seconds": round(time.time() - ts, 1) if ts else None}
    return {"available": False, "reason": "日志尾部没有已发送记录"}


def _iso_to_epoch(value: str) -> float | None:
    """Parse the media server's UTC timestamps.

    Fractional seconds vary in length between endpoints, and the trailing Z is
    not accepted by fromisoformat on every supported runtime, so both are
    normalised rather than trusted.
    """
    text = str(value or "").strip()
    if not text:
        return None
    text = text.rstrip("Zz")
    if "." in text:
        head, _, frac = text.partition(".")
        frac = "".join(ch for ch in frac if ch.isdigit())[:6]
        text = f"{head}.{frac}" if frac else head
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = time.strptime(text, fmt)
        except ValueError:
            continue
        # The server reports UTC; timegm avoids a local-timezone shift that
        # would make a fresh item look hours old.
        import calendar
        return calendar.timegm(parsed)
    return None


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------
@dataclass
class IntakeCollector:
    """Builds one snapshot of the intake pipeline.

    Everything it touches arrives through ``paths``, ``fs`` or ``emby``, so
    the same code runs against a production host and against a tmp directory
    in tests.
    """

    paths: IntakePaths = field(default_factory=IntakePaths)
    fs: FsReader = field(default_factory=FsReader)
    emby: Any = None
    thresholds: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    now: Callable[[], float] = time.time

    # -- media server ------------------------------------------------------
    async def collect_emby(self) -> dict[str, Any]:
        """Library scan state, newest item age, and probe concentration.

        Three independent calls, each guarded separately: a server that
        answers the task list but refuses the log should still show the task
        list rather than collapsing the whole card.
        """
        if self.emby is None:
            return unavailable("未配置媒体服务器")
        out: dict[str, Any] = {"available": True}

        try:
            tasks = await self.emby.scheduled_tasks()
            out["scan"] = self._scan_state(tasks)
        except Exception as exc:  # noqa: BLE001 - degraded, never fatal
            out["scan"] = unavailable(f"无法读取任务: {type(exc).__name__}")

        try:
            latest = await self.emby.latest_created(limit=1)
            out["latest"] = self._latest_state(latest)
        except Exception as exc:  # noqa: BLE001
            out["latest"] = unavailable(f"无法读取最新入库: {type(exc).__name__}")

        try:
            log_text = await self.emby.server_log_tail(MAX_LOG_TAIL_BYTES)
            out["probe"] = parse_probe_hotspots(log_text or "")
        except Exception as exc:  # noqa: BLE001
            out["probe"] = unavailable(f"无法读取日志: {type(exc).__name__}")
        return out

    def _scan_state(self, tasks: Any) -> dict[str, Any]:
        """Find the library scan among the server's scheduled tasks.

        Matched on the stable task key first and only then on the display
        name: the name is localised and there are several tasks whose names
        contain "scan", so name-first matching picks the wrong row.
        """
        if not isinstance(tasks, list):
            return unavailable("任务列表格式异常")
        chosen = None
        for task in tasks:
            if not isinstance(task, dict):
                continue
            if str(task.get("Key") or "") == "RefreshLibrary":
                chosen = task
                break
        if chosen is None:
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                name = str(task.get("Name") or "").lower()
                if "scan media library" in name:
                    chosen = task
                    break
        if chosen is None:
            return unavailable("未找到媒体库扫描任务")
        result = chosen.get("LastExecutionResult") or {}
        end_ts = _iso_to_epoch(result.get("EndTimeUtc") or "")
        progress = chosen.get("CurrentProgressPercentage")
        return {
            "available": True,
            "name": str(chosen.get("Name") or ""),
            "state": str(chosen.get("State") or ""),
            "running": str(chosen.get("State") or "").lower() == "running",
            "progress": round(_float(progress), 2) if progress is not None else None,
            "last_status": str(result.get("Status") or ""),
            "last_end_ts": end_ts,
            "last_end_age_seconds": (
                round(self.now() - end_ts, 1) if end_ts else None),
        }

    def _latest_state(self, payload: Any) -> dict[str, Any]:
        items = []
        if isinstance(payload, dict):
            items = payload.get("Items") or []
        elif isinstance(payload, list):
            items = payload
        if not items:
            return unavailable("没有可用的入库记录")
        first = items[0] if isinstance(items[0], dict) else {}
        created = _iso_to_epoch(first.get("DateCreated") or "")
        if created is None:
            return unavailable("入库时间缺失")
        age = self.now() - created
        return {
            "available": True,
            "type": str(first.get("Type") or ""),
            "created_ts": created,
            # Negative ages happen when the server clock is slightly ahead;
            # clamping avoids rendering "-2 分钟前".
            "age_seconds": round(max(0.0, age), 1),
            "age_minutes": round(max(0.0, age) / 60, 1),
        }

    # -- refresh queue -----------------------------------------------------
    def collect_refresh(self) -> dict[str, Any]:
        # Counted with the cheap counter and listed separately: the depth is a
        # headline number and must be exact even when the queue is deep, while
        # only the handful of rows actually rendered need to be parsed.
        counted = self.fs.count_names(self.paths.refresh_queue_dir, ".json")
        entries = self.fs.listdir(self.paths.refresh_queue_dir)
        suppressed = self.fs.exists(self.paths.refresh_suppress_file)
        if entries is None or counted is None:
            out = unavailable("刷新队列目录不存在")
            out["suppressed"] = suppressed
            return out
        queue_names, queue_capped = counted
        files = [p for p in entries if p.name.endswith(".json")]
        now = self.now()
        oldest_age = 0.0
        rows: list[dict[str, Any]] = []
        parsed = 0
        broken = 0
        for path in files[:MAX_QUEUE_PARSE]:
            data = self.fs.read_json(path)
            if (not isinstance(data, dict)
                    or not isinstance(data.get("paths") or [], list)):
                broken += 1
                continue
            parsed += 1
            first = _float(data.get("first_event_ts"))
            age = max(0.0, now - first) if first else 0.0
            oldest_age = max(oldest_age, age)
            rows.append({
                # relative_dir is the sanitised form the collector already
                # writes; container_dir is the absolute one and is only used
                # when the relative form is missing.
                "label": str(data.get("relative_dir")
                             or data.get("container_dir") or "?"),
                "events": _int(data.get("event_count")),
                "paths": len(data.get("paths") or []),
                "age_seconds": round(age, 1),
            })
        rows.sort(key=lambda r: (-r["age_seconds"], r["label"]))
        sent = self.fs.count_names(self.paths.refresh_sent_dir)
        return {
            "available": True,
            "total": len(queue_names),
            "truncated": queue_capped,
            "parsed": parsed,
            "unreadable": broken,
            "oldest_age_seconds": round(oldest_age, 1),
            "suppressed": suppressed,
            "sent_total": len(sent[0]) if sent is not None else None,
            "top": rows[:TOP_N],
        }

    # -- notifications -----------------------------------------------------
    def collect_notify(self) -> dict[str, Any]:
        entries = self.fs.listdir(self.paths.notify_pending_dir)
        out: dict[str, Any] = {}
        if entries is None:
            out["pending"] = unavailable("通知队列目录不存在")
        else:
            files = [p for p in entries if p.name.endswith(".json")]
            now = self.now()
            oldest = 0.0
            for path in files[:MAX_QUEUE_PARSE]:
                mtime = self.fs.mtime(path)
                if mtime:
                    oldest = max(oldest, max(0.0, now - mtime))
            out["pending"] = {
                "available": True,
                "total": len(files),
                "oldest_age_seconds": round(oldest, 1),
            }
        tail = (self.fs.tail(self.paths.notify_log, MAX_NOTIFY_TAIL_BYTES)
                if self.paths.notify_log else None)
        out["last_sent"] = (parse_notify_tail(tail) if tail is not None
                            else unavailable("通知日志不可读"))
        return out

    # -- upload lanes and local buffers ------------------------------------
    def collect_upload(self) -> dict[str, Any]:
        lanes: list[dict[str, Any]] = []
        lane_entries = self.fs.listdir(self.paths.upload_lane_root)
        if lane_entries is None:
            lanes_section: Any = unavailable("上传通道目录不存在")
        else:
            complete = True
            for entry in sorted(lane_entries, key=lambda p: p.name):
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    complete = False
                    continue
                walked = self.fs.walk_files(str(entry))
                if walked is None:
                    complete = False
                    continue
                count, total = walked
                lanes.append({"name": entry.name, "items": count, "bytes": total})
            lanes_section = {
                "available": complete,
                "lanes": lanes,
                "items": sum(x["items"] for x in lanes) if complete else None,
                "bytes": sum(x["bytes"] for x in lanes) if complete else None,
                **({} if complete else {"reason": "部分通道不可读或超过扫描上限"}),
            }

        def buffer_of(path: str, label: str) -> dict[str, Any]:
            walked = self.fs.walk_files(path)
            if walked is None:
                return {"name": label, "available": False}
            count, total = walked
            return {"name": label, "available": True,
                    "items": count, "bytes": total}

        rate_limited: list[str] = []
        state_entries = self.fs.listdir(self.paths.upload_state_dir)
        if state_entries is not None:
            rate_limited = sorted(
                p.name.rsplit(".", 1)[0] for p in state_entries
                if p.name.endswith(".rate-limited"))
        return {
            "lanes": lanes_section,
            "buffers": [
                buffer_of(self.paths.staging_dir, "staging"),
                buffer_of(self.paths.local_fallback_dir, "local-fallback"),
                buffer_of(self.paths.quarantine_dir, "quarantine"),
            ],
            "rate_limited": rate_limited,
            "rate_limited_known": state_entries is not None,
        }

    # -- cloud pull --------------------------------------------------------
    def collect_cloud(self) -> dict[str, Any]:
        """Cloud-source pull state: unfinished claims, backlog and queue.

        A claim is one job; a matching ``done`` receipt closes it. Unfinished
        work is therefore claims with no receipt, which is derived rather than
        stored — there is no counter to drift out of step with the files.
        """
        claims = self.fs.count_names(self.paths.cloud_claims_dir, ".json")
        done = self.fs.count_names(self.paths.cloud_done_dir, ".json")
        out: dict[str, Any] = {}
        if claims is None or done is None:
            out["claims"] = unavailable("拉取状态目录不存在")
        else:
            claim_names, claims_capped = claims
            done_names, done_capped = done
            claim_ids = {name.rsplit(".", 1)[0] for name in claim_names}
            # "<job_id>-done-<stamp>.json" -> job id
            done_ids = {name.split("-done-", 1)[0] for name in done_names}
            truncated = claims_capped or done_capped
            entry: dict[str, Any] = {
                "available": True,
                "total": len(claim_ids),
                "done": len(done_ids & claim_ids),
                "truncated": truncated,
            }
            # A partial listing cannot support a subtraction: every claim whose
            # receipt fell outside the cap would be reported as unfinished
            # work. Report the count as unknown rather than confidently wrong.
            entry["outstanding"] = (None if truncated
                                    else len(claim_ids - done_ids))
            out["claims"] = entry

        pending = self.fs.listdir(self.paths.cloud_pending_dir)
        events = self.fs.listdir(self.paths.cloud_events_dir)
        out["pending_identity"] = (len(pending) if pending is not None else None)
        out["events"] = (len(events) if events is not None else None)

        backlog = (self.fs.read_json(self.paths.cloud_backlog_file)
                   if self.paths.cloud_backlog_file else None)
        if isinstance(backlog, dict):
            rows = backlog.get("rows")
            out["backlog"] = {
                "available": True,
                "rows": len(rows) if isinstance(rows, list) else 0,
                "generated_ts": _float(backlog.get("at")) or None,
            }
        else:
            out["backlog"] = unavailable("积压文件不可读")

        queue = (self.fs.read_json(self.paths.cloud_queue_file)
                 if self.paths.cloud_queue_file else None)
        out["queue"] = ({"available": True, "depth": len(queue)}
                        if isinstance(queue, list)
                        else unavailable("队列文件不可读"))

        active = (self.fs.read_json(self.paths.cloud_active_file)
                  if self.paths.cloud_active_file else None)
        if isinstance(active, dict):
            manifest = active.get("manifest")
            out["active"] = {
                "available": True,
                "mode": str(active.get("mode") or ""),
                "manifest_items": len(manifest) if isinstance(manifest, list) else 0,
                "created_ts": _float(active.get("created_at")) or None,
            }
        else:
            out["active"] = unavailable("当前任务文件不可读")
        return out

    # -- downloader --------------------------------------------------------
    async def collect_downloader(self, clients: Iterable[Any]) -> dict[str, Any]:
        rows = []
        for client in clients or []:
            try:
                summary = await client.summary()
            except Exception as exc:  # noqa: BLE001 - one bad client, one bad row
                rows.append({"name": getattr(client, "name", "?"),
                             "available": False,
                             "reason": f"{type(exc).__name__}"})
                continue
            rows.append({"name": getattr(client, "name", "?"),
                         "available": True, **(summary or {})})
        return {"clients": rows}

    # -- verdict -----------------------------------------------------------
    def evaluate(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Reduce the snapshot to one colour plus the reasons behind it.

        Rules are deliberately conjunctive where a single signal would lie.
        "Nothing arrived recently" alone is a quiet night; combined with
        notifications that cannot be delivered it is a stall.
        """
        alerts: list[dict[str, str]] = []
        emby = snapshot.get("emby") or {}
        latest = emby.get("latest") or {}
        notify = snapshot.get("notify") or {}
        pending = notify.get("pending") or {}
        refresh = snapshot.get("refresh") or {}
        upload = snapshot.get("upload") or {}

        stale_minutes = _float(self.thresholds.get("stale_intake_minutes"),
                               DEFAULT_THRESHOLDS["stale_intake_minutes"])
        if (latest.get("available") and pending.get("available")
                and _float(latest.get("age_minutes")) > stale_minutes
                and _int(pending.get("total")) > 0):
            alerts.append({
                "level": "bad",
                "message": (f"最新入库已过 {int(_float(latest.get('age_minutes')))} 分钟，"
                            f"且有 {_int(pending.get('total'))} 条通知待发"),
            })

        probe = emby.get("probe") or {}
        scan = emby.get("scan") or {}
        hotspot = _float(self.thresholds.get("probe_hotspot_ratio"),
                         DEFAULT_THRESHOLDS["probe_hotspot_ratio"])
        if probe.get("available") and _float(probe.get("top_ratio")) > hotspot:
            # A running full-library scan walks one directory at a time, so it
            # concentrates probes by construction -- observed live at 100% on a
            # perfectly healthy server. Raising the alarm then would light the
            # page red for the entire duration of a routine scan, which is how
            # an indicator stops being read. Concentration is only evidence of
            # a loop when nothing is scanning.
            scanning = bool(scan.get("available")) and bool(scan.get("running"))
            alerts.append({
                "level": "idle" if scanning else "bad",
                "message": (f"探测集中在 {probe.get('top_group')}"
                            f"（{round(_float(probe.get('top_ratio')) * 100)}%）"
                            + ("，媒体库扫描进行中，属预期" if scanning
                               else "，疑似探测循环")),
            })

        warn_hours = _float(self.thresholds.get("refresh_age_warn_hours"),
                            DEFAULT_THRESHOLDS["refresh_age_warn_hours"])
        if (refresh.get("available")
                and _float(refresh.get("oldest_age_seconds")) > warn_hours * 3600):
            hours = _float(refresh.get("oldest_age_seconds")) / 3600
            # An ageing queue is only a fault when something is meant to be
            # draining it. With the suppression switch deliberately on, a
            # growing queue is the switch working as intended, and flagging it
            # amber would train the operator to ignore the colour.
            suppressed = bool(refresh.get("suppressed"))
            alerts.append({
                "level": "idle" if suppressed else "warn",
                "message": (f"刷新队列最老条目已等待 {hours:.1f} 小时"
                            + ("（推送已抑制，属预期）" if suppressed else "")),
            })

        if upload.get("rate_limited"):
            alerts.append({
                "level": "warn",
                "message": "存在上传限速标记: " + ", ".join(upload["rate_limited"][:5]),
            })

        if refresh.get("suppressed"):
            alerts.append({"level": "idle", "message": "刷新推送已被抑制开关关闭"})

        level = "ok"
        if any(a["level"] == "bad" for a in alerts):
            level = "bad"
        elif any(a["level"] == "warn" for a in alerts):
            level = "warn"
        elif alerts:
            level = "idle"
        return {"level": level, "alerts": alerts}

    # -- full snapshot -----------------------------------------------------
    async def snapshot(self, downloaders: Iterable[Any] = ()) -> dict[str, Any]:
        started = self.now()
        def local_sections() -> dict[str, Any]:
            out = {}
            for name, collect in (("refresh", self.collect_refresh), ("notify", self.collect_notify),
                                  ("upload", self.collect_upload), ("cloud", self.collect_cloud)):
                try:
                    out[name] = collect()
                except Exception as exc:  # noqa: BLE001 - isolate broken local state
                    out[name] = unavailable(f"采集失败: {type(exc).__name__}")
            return out

        data: dict[str, Any] = {
            "generated_at": started,
            "emby": await self.collect_emby(),
            **await asyncio.to_thread(local_sections),
            "downloader": await self.collect_downloader(downloaders),
            "thresholds": dict(self.thresholds),
        }
        data["health"] = self.evaluate(data)
        data["collect_ms"] = int((self.now() - started) * 1000)
        return data
