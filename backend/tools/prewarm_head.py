#!/usr/bin/env python3
# ruff: noqa: EXE001 - invoked with Python; executable mode is not required
"""Bounded playback head/tail prewarm for the existing rclone VFS cache.

PlaybackInfo HTTP requests now trigger immediately (no IsPlayback pairing).
Observed coverage: 62% of starts have a same-item POST in the preceding 120s;
lead p25/p50/p75/p90 is 1/2/5/13s versus cold first-byte p50 of about 1.4s.
After item + 5-minute dedup, about 62% of POSTs convert to playback within 120s,
with or without IsPlayback=true: speculative work remains, and completion
before the client's GET is NOT guaranteed. The 302 trigger remains a fallback.
Disk and admission budgets are configurable; disk blocking logs are bounded.
Workers also read up to 2MiB at EOF, without rereading overlapping head bytes.

v2.1 fixes (in addition to v2's B1-B5): the pump timeout path now releases the
reservation exactly once for both immediate-reap branches (WNOHANG returning
pid, and ChildProcessError from an already-reaped child); budget-blocked skip
logging is rate-limited to one entry line + one unblock line + at most one
summary per 60s instead of one line per 20ms check.

v2 fixes (independent-review blockers):

B1 coverage: Emby-log PlaybackInfo HTTP request lines provide the earlier
trigger described above; the old same-thread IsPlayback pair covered only
about 10% of starts. Every parsable nginx 302 issuance remains a fallback
(~90% of items map to whitelisted mounts; other library roots are rejected).
Both resolve item->path through the LOCAL read-only Emby library.db INTEGER-PK
point query. The client can GET almost immediately after 302, and dispatch-log
buffering can delay observation further: this fallback has little useful lead.

B2 rotation: LogFollower detects rename+create (inode change), copytruncate
(size shrink), deletion and watcher restart; no manual restart ever needed.

B3 budget semantics (three separate things, honestly named):
- _reserved_bytes: admission-time reservation (PER_FILE_EXPANSION_BUDGET) per
  LIVE worker (running or killed-but-unreaped). Released only on true reap.
- rolling admission window: WINDOW_BYTES per WINDOW_SECONDS charged at spawn;
  old charges age out so the tool NEVER permanently fuses itself.
- measured growth: completion-time allocated-block delta is recorded for
  accounting; rclone async read-ahead means settled growth can exceed it
  (observed 61MiB measured -> 353MiB settled). The window+reservation bound the
  number of live/recent warms, hence bound total settled growth approximately;
  exact upstream bytes are NOT claimed.

B4 API/lookup failures: all item->path resolution runs on a single background
resolver thread; failures drop that event only, with exponential backoff
(2s..60s) that pauses API fallback while the local-DB path stays active. The
main pump never performs HTTP and never stops draining workers.

B5 leaked workers: a killed-but-unreaped worker keeps BOTH its concurrency
slot and its reservation until waitpid truly reaps it.

Safety properties retained from v1 (independently verified): watcher process
performs zero mount filesystem calls; realpath/symlink/isfile/open/read and
cache-entry stat happen only inside the killable worker process group;
per-connection rate caps, signing, admission and any user token are untouched;
log output is directory-level only.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import sys
import threading
import time
import urllib.request
from pathlib import Path


def _positive_bytes(name: str, default: int, unit: int) -> int:
    """Read a positive, finite size at startup; never hide invalid configuration."""
    try:
        value = float(os.environ.get(name, str(default)))
        if not math.isfinite(value) or value <= 0:
            raise ValueError
        size = int(value * unit)
        if size <= 0:
            raise ValueError
    except (ValueError, OverflowError) as exc:
        raise ValueError(f'{name} must be a positive finite number') from exc
    return size


HEAD_BYTES = 32 * 1024 * 1024                      # app-level read per file
TAIL_BYTES = 2 * 1024 * 1024                       # metadata near EOF
MAX_CONCURRENT = 2                                # running + leaked workers
PER_FILE_EXPANSION_BUDGET = _positive_bytes(
    'MEDIADECK_PREWARM_FILE_BUDGET_MB', 320, 1024 ** 2)  # reservation per live worker
TOTAL_RESERVATION_LIMIT = MAX_CONCURRENT * PER_FILE_EXPANSION_BUDGET
WINDOW_SECONDS = 600.0
WINDOW_BYTES = _positive_bytes(
    'MEDIADECK_PREWARM_WINDOW_MB', 1920, 1024 ** 2)  # admissions per rolling window
QUEUE_TTL = 120.0
DEDUP_TTL = 300.0
DISK_LOW_WATER_BYTES = _positive_bytes(
    'MEDIADECK_PREWARM_DISK_LOW_WATER_GB', 320, 1024 ** 3)  # emergency cache-disk floor
WORKER_TIMEOUT = 60.0
# Host layout comes from the environment (see docs/OPERATIONS-ENV.md); the
# defaults are neutral examples, not a real deployment.
DEFAULT_PREWARM_MOUNTS = {
    '/mnt/media-a/Media': '/var/cache/rclone/media-a/vfs/Media',
    '/mnt/media-b/Media': '/var/cache/rclone/media-b/vfs',
}


def _prewarm_mounts() -> dict[str, Path]:
    """Whitelisted mount point -> its rclone VFS cache directory, in order."""
    raw = os.environ.get('MEDIADECK_PREWARM_MOUNTS')
    mounts = json.loads(raw) if raw else DEFAULT_PREWARM_MOUNTS
    if (not isinstance(mounts, dict) or not mounts
            or any(not isinstance(k, str) or not k.startswith('/')
                   or not isinstance(v, str) or not v.startswith('/')
                   for k, v in mounts.items())):
        raise ValueError('MEDIADECK_PREWARM_MOUNTS must map absolute mount paths '
                         'to absolute cache paths')
    return {mount: Path(cache) for mount, cache in mounts.items()}


def _path_prefixes() -> tuple[str, ...]:
    raw = os.environ.get('MEDIADECK_EMBY_PATH_PREFIXES')
    if not raw:
        return ('/media/',)
    prefixes = tuple(p.strip() for p in raw.split(',') if p.strip())
    if not prefixes:
        raise ValueError('MEDIADECK_EMBY_PATH_PREFIXES has no prefixes')
    return prefixes


EMBY_LOG = Path(os.environ.get('MEDIADECK_EMBY_LOG')
                or '/var/lib/emby/logs/embyserver.txt')
DISPATCH_LOG = Path(os.environ.get('MEDIADECK_DISPATCH_LOG')
                    or '/var/log/nginx/mediadeck-dispatch.log')
EMBY_LIBRARY_DB = (os.environ.get('MEDIADECK_EMBY_LIBRARY_DB')
                   or '/var/lib/emby/data/library.db')
EMBY_PATH_PREFIXES = _path_prefixes()
CACHE_ROOTS = _prewarm_mounts()
MEDIA_MOUNTS = tuple(CACHE_ROOTS)
ALLOCATED_SLOP = 8 * 1024 * 1024                   # blocks dirty mid-write
API_BACKOFF_BASE = 2.0
API_BACKOFF_MAX = 60.0
RESOLVER_QUEUE_MAX = 64
BACKFILL_BYTES = 256 * 1024                        # bounded restart backfill
BLOCKED_LOG_INTERVAL = 60.0                       # max 1 summary log/min while blocked


def dir_of(path: str) -> str:
    """Directory-level logging only: no filenames, no query strings."""
    return str(Path(path).parent)[-60:]


def log(event: str, **fields) -> None:
    print(json.dumps({'at': round(time.time(), 3), 'event': event, **fields},
                     ensure_ascii=False), flush=True)


# ---------------------------------------------------------------- worker child
def _cache_allocated(worker_path: str) -> int:
    """Metadata-only probe on the local cache tree (never the mount)."""
    for mount, root in CACHE_ROOTS.items():
        if worker_path.startswith(mount + '/'):
            entry = root / worker_path[len(mount) + 1:]
            try:
                return entry.stat().st_blocks * 512 + ALLOCATED_SLOP
            except OSError:
                return 0                            # no cache entry: fully cold
    return 0


def worker_main(candidates: list[str], head_bytes: int, out_fd: int) -> None:
    """Only the killable worker probes media paths and per-file cache entries."""
    result: dict = {'used': None, 'error': None}
    try:
        os.setsid()
    except OSError:
        pass
    try:
        chosen = None
        for cand in candidates:
            if '..' in Path(cand).parts:
                continue
            rp = os.path.realpath(cand)
            if not any(rp == m or rp.startswith(m + '/') for m in MEDIA_MOUNTS):
                continue                            # realpath escaped whitelist
            if os.path.islink(cand) or not os.path.isfile(rp):
                continue
            chosen = rp
            break
        if chosen is None:
            raise FileNotFoundError('no whitelisted candidate')
        before = _cache_allocated(chosen)
        t0 = time.monotonic()
        with open(chosen, 'rb', buffering=0) as f:
            f.read(1)
            t_first = time.monotonic() - t0
            remaining = head_bytes - 1
            while remaining > 0:
                chunk = f.read(min(4 * 1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            t_tail = time.monotonic()
            size = os.fstat(f.fileno()).st_size
            tail_start = max(f.tell(), size - TAIL_BYTES)
            remaining = size - tail_start
            if remaining > 0:
                f.seek(tail_start)
                while remaining > 0:
                    chunk = f.read(min(4 * 1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            tail_read_s = time.monotonic() - t_tail
        after = _cache_allocated(chosen)
        result = {'used': chosen, 'open_to_first_byte_s': round(t_first, 3),
                  'tail_read_s': round(tail_read_s, 3),
                  'total_s': round(time.monotonic() - t0, 3),
                  'allocated_before': before, 'allocated_after': after}
    except Exception as exc:  # noqa: BLE001 - reported to parent, never fatal
        result = {'used': None, 'error': repr(exc)[:160]}
    try:
        os.write(out_fd, json.dumps(result, ensure_ascii=False).encode())
    except OSError:
        pass


# ------------------------------------------------------------- rotation-safe tail
class LogFollower:
    """Tails one append-only log, surviving rename+create, copytruncate,
    deletion and process restarts. Yields only complete lines."""

    def __init__(self, path: Path, backfill: int = 0):
        self.path = path
        self.fh = None
        self.inode = None
        self.pos = 0
        self.backfill = backfill

    def _open(self) -> None:
        try:
            st = self.path.stat()
        except OSError:
            self.fh = None
            self.inode = None
            self.pos = 0
            return
        # Kept open across polls; rotation and deletion explicitly close it.
        self.fh = open(self.path, 'r', encoding='utf-8', errors='replace')  # noqa: SIM115
        self.inode = (st.st_dev, st.st_ino)
        if self.backfill:
            if st.st_size > self.backfill:
                self.fh.seek(st.st_size - self.backfill)
                self.fh.readline()              # drop partial line
            else:
                self.fh.seek(0)                 # small file: read it whole
        else:
            self.fh.seek(0, os.SEEK_END)
        self.pos = self.fh.tell()

    def ensure_open(self) -> None:
        if self.fh is None:
            self._open()
            if self.fh is not None:
                log('tail_open', path=str(self.path), pos=self.pos)

    def poll(self) -> list[str]:
        """Return new complete lines; handle all rotation modes."""
        self.ensure_open()
        if self.fh is None:
            return []
        try:
            st = self.path.stat()
        except OSError:
            log('tail_gone', path=str(self.path))
            self.fh.close()
            self.fh = None
            return []
        cur = (st.st_dev, st.st_ino)
        if cur != self.inode:                       # rename+create / new file
            log('tail_rotated_reopen', path=str(self.path))
            self.fh.close()
            self._open()
            self.fh.seek(0)                         # read the fresh file head
            self.pos = 0
        elif st.st_size < self.pos:                 # copytruncate
            log('tail_truncated_rewind', path=str(self.path))
            self.fh.seek(0)
            self.pos = 0
        lines = []
        while True:
            line = self.fh.readline()
            if not line:
                break
            if line.endswith('\n'):
                lines.append(line)
                self.pos = self.fh.tell()
            else:
                # incomplete trailing line: rewind so it is re-read later
                self.fh.seek(self.pos)
                break
        return lines


# ------------------------------------------------------------------- watcher
class PrewarmWatcher:
    def __init__(self, settings_path: Path, once: bool = False,
                 log_path: Path | None = None, direct: list[str] | None = None,
                 state_out: Path | None = None,
                 dispatch_log_path: Path | None = None,
                 library_db: str | None = None):
        cfg = json.loads(settings_path.read_text())['emby']
        self._emby_url = cfg['url'].rstrip('/')
        self._headers = {'X-Emby-Token': cfg['api_key']}
        self.once = once
        self.log_path = log_path            # None => no Emby-log tail (tests)
        self.dispatch_log_path = dispatch_log_path   # None => no dispatch tail
        self.library_db = library_db or EMBY_LIBRARY_DB
        self.state_out = state_out
        self.queue: list[tuple[float, list[str]]] = []
        self.dedup: dict[str, float] = {}
        self._procs: list[tuple[str, int, int, float]] = []   # key,pid,fd,t0
        self._zombies: set[int] = set()              # killed, awaiting reap
        self._leaked: dict[int, tuple[str, float]] = {}       # B5: pid->(key,since)
        self._reserved_bytes = 0                     # B3: live reservations
        self._window_charges: list[tuple[float, int]] = []    # B3: (t, bytes)
        self.expansion_used = 0                      # measured total (accounting)
        self._api_backoff_until = 0.0                # B4
        self._api_backoff_seq = 0
        self._blocked_reason = None                   # v4: budget-block state
        self._blocked_since = 0.0
        self._last_blocked_log = 0.0
        self._disk_blocked_path: Path | None = None
        self._disk_blocked_since = 0.0
        self._last_disk_blocked_log = 0.0
        self._resolver_in: list[tuple[str, str]] = []           # (item_id, source)
        self._resolver_out: list[tuple[str, str | None]] = []   # (item_id, path)
        self._resolver_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.stats = {'playback_seen': 0, 'issuance_seen': 0, 'triggered': 0,
                      'resolved': 0, 'skipped_dedup': 0, 'skipped_ttl': 0,
                      'skipped_disk': 0, 'skipped_budget': 0,
                      'skipped_window': 0, 'skipped_slots': 0,
                      'skipped_backoff': 0, 'failed_lookup': 0,
                      'rejected_path': 0, 'started': 0, 'done': 0,
                      'failed': 0, 'timeout': 0, 'leaked_held': 0,
                      'grew_bytes': 0}
        if direct:
            for p in direct:
                self._enqueue_key(p, [p])

    # ---- resolution (B1/B4: background thread, local DB first) ----------
    def _resolve_local(self, item_id: str) -> str | None:
        try:
            con = sqlite3.connect('file:' + self.library_db + '?mode=ro',
                                  uri=True, timeout=5)
            try:
                row = con.execute('SELECT Path FROM MediaItems WHERE Id=?',
                                  (int(item_id),)).fetchone()
            finally:
                con.close()
            return row[0] if row else None
        except (sqlite3.Error, ValueError):
            return None

    def _resolve_api(self, item_id: str) -> str | None:
        if time.monotonic() < self._api_backoff_until:
            self.stats['skipped_backoff'] += 1
            log('skip', reason='api_backoff', item=item_id)
            return None
        url = self._emby_url + '/Items/' + item_id + '?Fields=Path'
        req = urllib.request.Request(url, headers=self._headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                path = json.load(resp).get('Path')
            self._api_backoff_seq = 0               # success resets backoff
            return path
        except Exception as exc:  # noqa: BLE001 - B4: drop event, back off
            self._api_backoff_seq += 1
            until = min(API_BACKOFF_MAX,
                        API_BACKOFF_BASE * 2 ** (self._api_backoff_seq - 1))
            self._api_backoff_until = time.monotonic() + until
            self.stats['failed_lookup'] += 1
            log('lookup_failed', item=item_id, backoff_s=until,
                error=repr(exc)[:120])
            return None

    def _resolver_loop(self) -> None:
        while not self._stop.is_set():
            if not self._resolver_in:
                time.sleep(0.05)
                continue
            item_id, source = self._resolver_in.pop(0)
            path = self._resolve_local(item_id)
            if path is None and source == 'emby_log':
                path = self._resolve_api(item_id)   # request trigger retains API fallback
            self._resolver_out.append((item_id, path))

    def _start_resolver(self) -> None:
        if self._resolver_thread is None:
            self._resolver_thread = threading.Thread(target=self._resolver_loop,
                                                     daemon=True)
            self._resolver_thread.start()

    # ---- triggers --------------------------------------------------------
    def _candidates(self, emby_path: str) -> list[str]:
        """Lexical mapping only: no mount filesystem access in the watcher."""
        for prefix in EMBY_PATH_PREFIXES:
            if not emby_path.startswith(prefix):
                continue
            rel = emby_path[len(prefix):].lstrip('/')
            if not rel or '..' in Path(rel).parts or rel.startswith('/'):
                return []
            return [os.path.join(m, rel) for m in MEDIA_MOUNTS]
        return []

    def _enqueue_key(self, key: str, candidates: list[str]) -> None:
        now = time.time()
        last = self.dedup.get(key)
        if last is not None and now - last < DEDUP_TTL:
            self.stats['skipped_dedup'] += 1
            return
        self.dedup[key] = now
        self.queue.append((now, candidates))
        self.stats['triggered'] += 1
        log('enqueued', path_dir=dir_of(candidates[0]))

    def _after_resolution(self, item_id: str, emby_path: str | None) -> None:
        if not emby_path:
            self.stats['rejected_path'] += 1
            return
        candidates = self._candidates(emby_path)
        if not candidates:
            self.stats['rejected_path'] += 1
            return
        self.stats['resolved'] += 1
        self._enqueue_key(emby_path, candidates)

    def on_log_line(self, line: str) -> None:
        """Early request trigger: 62% of starts, lead p25/p50/p75/p90 = 1/2/5/13s.

        About 62% of item + 5-minute-deduplicated POSTs convert within 120s;
        IsPlayback has no observed discrimination. Responses must not trigger.
        """
        if 'PlaybackInfo' not in line:
            return
        line = line.replace('\u200c', '').replace('\u200d', '')
        m = re.search(
            r'\bhttp/1\.[01]\s+POST\s+https?://[^/\s]+/'
            r'(?:emby/)?Items/(\d+)/PlaybackInfo(?:[?\s]|$)', line, re.IGNORECASE)
        if not m:
            return
        self.stats['playback_seen'] += 1
        self._resolver_in.append((m[1], 'emby_log'))
        if len(self._resolver_in) > RESOLVER_QUEUE_MAX:
            self._resolver_in.pop(0)

    def on_dispatch_line(self, line: str) -> None:
        """Fallback trigger: every parsable Deck 302 issuance line.

        The client can GET almost immediately; log buffering can delay this
        trigger further. Unlike the earlier POST, it offers little useful lead.
        """
        try:
            row = json.loads(line)
        except ValueError:
            return
        if row.get('status') != 302 or row.get('method') != 'GET':
            return
        m = re.match(r'^/(?:emby/)?(?:videos|audio)/(\d+)/(?:stream|original)(?:\.[A-Za-z0-9]+)?$',
                     (row.get('path') or ''), re.IGNORECASE)
        if not m:
            return
        self.stats['issuance_seen'] += 1
        self._resolver_in.append((m[1], 'dispatch'))
        if len(self._resolver_in) > RESOLVER_QUEUE_MAX:
            self._resolver_in.pop(0)

    def _drain_resolver(self) -> None:
        while self._resolver_out:
            item_id, path = self._resolver_out.pop(0)
            self._after_resolution(item_id, path)

    # ---- budget (B3) ------------------------------------------------------
    def _window_used(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        self._window_charges = [(t, b) for t, b in self._window_charges
                                if now - t <= WINDOW_SECONDS]
        return sum(b for _, b in self._window_charges)

    def _slots_used(self) -> int:
        return len(self._procs) + len(self._leaked)   # B5: leaked keeps its slot

    def _disk_ok(self) -> bool:
        """Check local cache filesystems, never media mounts; count blocked checks."""
        available = []
        for root in CACHE_ROOTS.values():
            path = root
            while not path.exists() and path != path.parent:
                path = path.parent
            available.append((shutil.disk_usage(path).free, path))
        free, path = min(available, key=lambda entry: entry[0])
        now = time.monotonic()
        if free < DISK_LOW_WATER_BYTES:
            self.stats['skipped_disk'] += 1
            if self._disk_blocked_path != path:
                log('disk_blocked', reason='disk_low_water', cache_dir=str(path),
                    free_bytes=free, low_water_bytes=DISK_LOW_WATER_BYTES)
                self._disk_blocked_path = path
                self._disk_blocked_since = now
                self._last_disk_blocked_log = now
            elif now - self._last_disk_blocked_log >= BLOCKED_LOG_INTERVAL:
                log('disk_blocked_summary', reason='disk_low_water', cache_dir=str(path),
                    free_bytes=free, blocked_for_s=int(now - self._disk_blocked_since))
                self._last_disk_blocked_log = now
            return False
        if self._disk_blocked_path is not None:
            log('disk_unblocked', cache_dir=str(self._disk_blocked_path), free_bytes=free,
                blocked_for_s=int(now - self._disk_blocked_since))
            self._disk_blocked_path = None
        return True

    def _budget_ok(self) -> bool:
        now = time.monotonic()
        blocked_reason = None
        if self._reserved_bytes + PER_FILE_EXPANSION_BUDGET > TOTAL_RESERVATION_LIMIT:
            blocked_reason = 'reservation_limit'
        elif self._window_used(now) + PER_FILE_EXPANSION_BUDGET > WINDOW_BYTES:
            blocked_reason = 'window_limit'
        if blocked_reason:
            if self._blocked_reason != blocked_reason:
                # state transition: entering (or switching) blocked state — log once
                log('budget_blocked', reason=blocked_reason,
                    reserved=self._reserved_bytes, window_used=self._window_used(now),
                    next_check_s=int(WINDOW_SECONDS))
                self._blocked_reason = blocked_reason
                self._blocked_since = now
                self._last_blocked_log = now
            elif now - self._last_blocked_log >= BLOCKED_LOG_INTERVAL:
                # periodic summary while blocked (once per interval, not per event)
                log('budget_blocked_summary', reason=blocked_reason,
                    reserved=self._reserved_bytes, window_used=self._window_used(now),
                    blocked_for_s=int(now - self._blocked_since))
                self._last_blocked_log = now
            if blocked_reason == 'reservation_limit':
                self.stats['skipped_budget'] += 1
            else:
                self.stats['skipped_window'] += 1
            return False
        if self._blocked_reason:
            log('budget_unblocked', was=self._blocked_reason,
                blocked_for_s=int(now - self._blocked_since))
            self._blocked_reason = None
        return True

    # ---- worker management -------------------------------------------------
    def _spawn(self, candidates: list[str]) -> None:
        r_fd, w_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r_fd)
            worker_main(candidates, HEAD_BYTES, w_fd)
            os._exit(0)
        os.close(w_fd)
        os.set_blocking(r_fd, False)
        self._procs.append(('|'.join(candidates), pid, r_fd, time.monotonic()))
        self._reserved_bytes += PER_FILE_EXPANSION_BUDGET          # B3 pre-charge
        self._window_charges.append((time.monotonic(), PER_FILE_EXPANSION_BUDGET))
        self.stats['started'] += 1

    def _expire_queue(self) -> None:
        now = time.time()
        kept = []
        for enq, candidates in self.queue:
            if now - enq > QUEUE_TTL:
                self.stats['skipped_ttl'] += 1
                log('skip', reason='queue_ttl', path_dir=dir_of(candidates[0]))
            else:
                kept.append((enq, candidates))
        self.queue = kept
        cutoff = time.time() - DEDUP_TTL * 4
        for k in [k for k, t in self.dedup.items() if t < cutoff]:
            self.dedup.pop(k, None)

    def _release_reservation(self, pid: int, measured_grew: int | None) -> None:
        if pid in self._leaked:
            self._leaked.pop(pid, None)
        self._reserved_bytes = max(0, self._reserved_bytes - PER_FILE_EXPANSION_BUDGET)
        if measured_grew is not None:
            self.expansion_used += measured_grew
            self.stats['grew_bytes'] += measured_grew

    def _reap_zombies(self) -> None:
        for pid in list(self._zombies):
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                self._zombies.discard(pid)
                self._release_reservation(pid, None)   # reaped by someone else
                continue
            if done == pid:
                self._zombies.discard(pid)
                self._release_reservation(pid, None)   # B5: hold ends at true reap

    def pump(self) -> bool:
        now = time.monotonic()
        self._reap_zombies()
        still, progressed = [], False
        for key, pid, r_fd, t0 in self._procs:
            try:
                data = os.read(r_fd, 65536)
            except BlockingIOError:
                if now - t0 > WORKER_TIMEOUT:
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    reaped = False
                    try:
                        done, _ = os.waitpid(pid, os.WNOHANG)
                        if done == pid:
                            reaped = True               # killed and reaped now
                    except ChildProcessError:
                        reaped = True                   # already reaped elsewhere
                    if reaped:
                        # v4 fix: immediate-reap branches must release exactly once
                        self._release_reservation(pid, None)
                    else:
                        self._zombies.add(pid)
                        self._leaked[pid] = (key, now)  # B5: keep slot+reservation
                        self.stats['leaked_held'] += 1
                        log('worker_leaked_held', pid=pid)
                    os.close(r_fd)
                    self.stats['timeout'] += 1
                    log('worker_timeout', path_dir=dir_of(key.split('|')[0]),
                        seconds=round(now - t0, 1))
                else:
                    still.append((key, pid, r_fd, t0))
                continue
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
            os.close(r_fd)
            result = {}
            if data:
                try:
                    result = json.loads(data)
                except ValueError:
                    result = {}
            if result.get('error') or not result.get('used'):
                self.stats['failed'] += 1
                grew = None
            else:
                self.stats['done'] += 1
                grew = max(0, result.get('allocated_after', 0)
                           - result.get('allocated_before', 0))
            self._release_reservation(pid, grew)
            log('worker_done', path_dir=dir_of(result.get('used') or key.split('|')[0]),
                **{k: result.get(k) for k in
                   ('open_to_first_byte_s', 'tail_read_s', 'total_s', 'error')})
            progressed = True
        self._procs = still
        return bool(still) or progressed

    # -- main loop ----------------------------------------------------------
    def run(self) -> int:
        log('watcher_start', once=self.once, emby_log=str(self.log_path),
            dispatch_log=str(self.dispatch_log_path))
        self._start_resolver()
        emby_tail = LogFollower(Path(self.log_path)) if self.log_path else None
        dispatch_tail = (LogFollower(Path(self.dispatch_log_path), backfill=BACKFILL_BYTES)
                         if self.dispatch_log_path else None)
        try:
            while True:
                for line in (emby_tail.poll() if emby_tail else []):
                    self.on_log_line(line)
                for line in (dispatch_tail.poll() if dispatch_tail else []):
                    self.on_dispatch_line(line)
                self._drain_resolver()
                self._expire_queue()
                while (self.queue and self._slots_used() < MAX_CONCURRENT
                       and self._budget_ok() and self._disk_ok()):
                    self._spawn(self.queue.pop(0)[1])
                if self.queue and self._slots_used() >= MAX_CONCURRENT:
                    self.stats['skipped_slots'] += 1
                active = self.pump()
                if self.once and not self.queue and not self._procs:
                    if self._resolver_in or self._resolver_out:
                        continue
                    break
                if not active and not self.queue:
                    time.sleep(0.05)
                else:
                    time.sleep(0.02)
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            for _, pid, r_fd, _ in self._procs:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    os.close(r_fd)
                except OSError:
                    pass
            log('watcher_stop', **self.stats, expansion_used=self.expansion_used,
                reserved=self._reserved_bytes, window_used=self._window_used())
            if self.state_out:
                self.state_out.write_text(json.dumps(
                    {'stats': self.stats, 'expansion_used': self.expansion_used,
                     'reserved_bytes': self._reserved_bytes},
                    indent=2))
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--settings',
                    default=str(Path(__file__).resolve().parents[1] / 'data/settings.json'))
    ap.add_argument('--once', action='store_true', help='drain queue then exit')
    ap.add_argument('--log', default=str(EMBY_LOG))
    ap.add_argument('--dispatch-log', default=str(DISPATCH_LOG))
    ap.add_argument('--library-db', default=EMBY_LIBRARY_DB)
    ap.add_argument('--direct', action='append', default=[],
                    help='prewarm these local candidate paths (tests/controlled runs)')
    ap.add_argument('--state-out')
    args = ap.parse_args()
    w = PrewarmWatcher(Path(args.settings), once=args.once,
                       log_path=Path(args.log), direct=args.direct,
                       state_out=Path(args.state_out) if args.state_out else None,
                       dispatch_log_path=Path(args.dispatch_log),
                       library_db=args.library_db)
    return w.run()


if __name__ == '__main__':
    sys.exit(main())
