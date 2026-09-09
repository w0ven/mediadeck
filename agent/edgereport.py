#!/usr/bin/env python3
"""mediadeck edge traffic reporter — single-file agent for streaming nodes.

Ships the node's own access log to the panel so per-user traffic can be
accounted from bytes that actually left the wire, rather than estimated from
what the media server happened to observe. Playback is served by signed direct
links straight from this node, so the media server never sees most of it.

Deployment is purely additive: this file plus one systemd unit. It reads the
log, writes nothing on the node, and touches no nginx configuration — in
particular nothing to do with secure_link or rate limiting, so a mistake here
cannot break playback.

Usage:
    edgereport.py --panel https://panel.example --node node-a \
                  --token-file /etc/mediadeck/report.token \
                  [--log /var/log/nginx/mediadeck-speed.log] \
                  [--interval 300] [--once]

Only stdlib. The credential is read from a file, never passed on the command
line where it would be visible in the process list.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import itertools
import json
import os
import sys
import time
import urllib.error
import urllib.request

#: Sent on every request. urllib's default ("Python-urllib/3.x") is blocked
#: outright by common WAF rules -- measured against the production edge, the
#: default agent got 403 while an identical request with any other agent got
#: through. Declaring what this actually is fixes that without asking anyone
#: to weaken a security rule.
USER_AGENT = "mediadeck-edgereport/1.0"

#: Lines sent in one request. Large enough that a busy node drains quickly,
#: small enough that a failed POST retries cheaply.
BATCH_LINES = 20_000
#: Bound on one pass, so a first run against a full archive cannot run for an
#: unbounded time or build an unbounded request body.
MAX_LINES_PER_PASS = 400_000


def log_paths(base: str) -> list[str]:
    """The live log and its rotations, oldest first.

    Ordering matters: rotations are consumed before the live file so a cursor
    is never advanced past data that has not been read yet.
    """
    found = [p for p in glob.glob(base + "*") if os.path.isfile(p)]

    def sort_key(path: str) -> tuple[int, str]:
        if path == base:
            return (0, path)          # live file last
        return (1, path)

    rotations = sorted((p for p in found if p != base), reverse=True)
    return rotations + [base] if base in found else rotations


def read_since(path: str, offset: int) -> tuple[list[str], int, int]:
    """Lines from ``offset``, the new offset, and the file's inode.

    Compressed rotations are read whole: they are immutable once written, and
    seeking inside a gzip stream costs more than reading it.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return [], offset, 0
    inode = stat.st_ino

    if path.endswith(".gz"):
        if offset:
            return [], offset, inode      # already consumed
        try:
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                lines = [line.rstrip("\n") for line in itertools.islice(fh, MAX_LINES_PER_PASS + 1)]
        except OSError:
            return [], offset, inode
        if len(lines) > MAX_LINES_PER_PASS:
            # Legacy gzip cursors mean "consumed whole archive". Never claim
            # that after shipping only its prefix; leave it unacknowledged.
            raise ValueError("archive exceeds one batch; explicit offline import required")
        return lines, stat.st_size, inode

    # A file shorter than the cursor was truncated or replaced: it is a
    # different file that happens to share a name, so start over.
    start = 0 if stat.st_size < offset else offset
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            fh.seek(start)
            lines = []
            # readline() rather than iteration: Python disables tell() on a
            # file being iterated, and the new offset must be exact or the
            # cursor either repeats or skips data.
            position = start
            while len(lines) < MAX_LINES_PER_PASS:
                line = fh.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    # Partial trailing write: leave it, and leave the offset
                    # before it so the completed line is read next pass.
                    break
                lines.append(line.rstrip("\n"))
                position = fh.tell()
            return lines, position, inode
    except OSError:
        return [], offset, inode


def post(panel: str, node: str, creds: str, payload: dict,
         timeout: float = 60.0) -> dict:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{panel.rstrip('/')}/api/edge/{node}/report",
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {creds}",
                 "User-Agent": USER_AGENT},
        method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def cursors_from(panel: str, node: str, creds: str, timeout: float = 30.0) -> dict:
    """Ask the panel where it left off.

    The panel owns the cursors rather than the node: it is the thing that must
    not double count, and a node-side cursor would silently diverge from it
    after any panel restore.
    """
    request = urllib.request.Request(
        f"{panel.rstrip('/')}/api/edge/{node}/cursors",
        headers={"Authorization": f"Bearer {creds}",
                 "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8", "replace"))
    return {c["path"]: c for c in data.get("cursors", [])}


def run_once(panel: str, node: str, creds: str, base_log: str,
             verbose: bool = True) -> int:
    try:
        cursors = cursors_from(panel, node, creds)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"cursor fetch failed: {type(exc).__name__}: {exc}", flush=True)
        return 1

    sent_total = 0
    archive_problem = False
    for path in log_paths(base_log):
        known = cursors.get(path) or {}
        try:
            current_inode = os.stat(path).st_ino
            aliases = [c for c in cursors.values() if int(c.get("inode") or 0) == current_inode]
            if aliases:
                known = max(aliases, key=lambda c: float(c.get("updated_at") or 0))
        except OSError:
            continue
        offset = int(known.get("offset") or 0)
        if int(known.get("inode") or 0) and os.path.exists(path):
            try:
                if os.stat(path).st_ino != int(known["inode"]):
                    offset = 0       # rotated into place under the same name
            except OSError:
                offset = 0

        try:
            lines, new_offset, inode = read_since(path, offset)
        except ValueError:
            print("archive exceeds supported batch; cursor not advanced", flush=True)
            archive_problem = True
            continue
        if not lines:
            continue

        # One bounded read -> one atomic panel commit. Splitting it while
        # holding the old cursor would re-bill all preceding chunks on retry.
        payload = {"path": path, "inode": inode, "offset": new_offset,
                   "previous_offset": offset, "lines": lines}
        try:
            result = post(panel, node, creds, payload)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"post failed: {type(exc).__name__}", flush=True)
            return 1
        if not result.get("ok"):
            return 1
        cursors[path] = {"inode": inode, "offset": new_offset, "updated_at": time.time()}
        sent_total += len(lines)
        if verbose:
            print(f"{path}: sent {len(lines)} lines, "
                  f"accepted={result.get('events')} "
                  f"bytes={result.get('bytes')}", flush=True)

    if verbose:
        print(f"done: {sent_total} lines", flush=True)
    return 1 if archive_problem else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--panel", required=True, help="panel base URL")
    parser.add_argument("--node", required=True, help="node name in the panel")
    parser.add_argument("--token-file", required=True,
                        help="file holding the node's report credential")
    parser.add_argument("--log", default="/var/log/nginx/mediadeck-speed.log")
    parser.add_argument("--interval", type=int, default=300,
                        help="seconds between passes; 0 with --once")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    try:
        with open(args.token_file, encoding="utf-8") as fh:
            creds = fh.read().strip()
    except OSError as exc:
        print(f"cannot read token file: {exc}", flush=True)
        return 2
    if not creds:
        print("token file is empty", flush=True)
        return 2

    if args.once:
        return run_once(args.panel, args.node, creds, args.log)

    while True:
        try:
            run_once(args.panel, args.node, creds, args.log)
        except Exception as exc:  # noqa: BLE001 - a reporter must not die
            print(f"pass failed: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    sys.exit(main())
