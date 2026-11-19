"""Deterministic CI test partitioning with 100% file coverage.

Partitions ``backend/tests/test_*.py`` into three dependency buckets:
- ``browser``: tests that launch Playwright / Chromium
- ``proxy``: tests that launch nginx / caddy / network namespaces
- ``unit``: all remaining pure-Python / FastAPI tests

Each bucket can be further balanced across N shards by file line count so
every shard runs in roughly equal time while guaranteeing that every
``test_*.py`` file is assigned to exactly one shard.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BROWSER_RE = re.compile(
    r"playwright|chromium|CHROME|headless_shell|hardglass_ui_browser|workspace_ui_browser"
)
PROXY_RE = re.compile(
    r"proxy_runtime|shutil\.which\(['\"](caddy|nginx|unshare)"
    r"|subprocess\.(run|Popen).*?(caddy|nginx|unshare)|netns"
)


def classify(tests_dir: Path) -> dict[str, list[Path]]:
    files = sorted(tests_dir.glob("test_*.py"))
    browser: list[Path] = []
    proxy: list[Path] = []
    unit: list[Path] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if BROWSER_RE.search(text):
            browser.append(path)
        elif PROXY_RE.search(text):
            proxy.append(path)
        else:
            unit.append(path)
    return {"browser": browser, "proxy": proxy, "unit": unit}


def shard_files(items: list[Path], total: int, index: int) -> list[Path]:
    if total < 1 or not (1 <= index <= total):
        raise ValueError(f"Invalid shard {index}/{total}")
    if total == 1:
        return list(items)
    weights = {p: p.read_text(encoding="utf-8", errors="ignore").count("\n") + 1 for p in items}
    bins: list[list[Path]] = [[] for _ in range(total)]
    loads = [0] * total
    for path in sorted(items, key=lambda p: (-weights[p], p.name)):
        slot = min(range(total), key=lambda i: loads[i])
        bins[slot].append(path)
        loads[slot] += weights[path]
    return sorted(bins[index - 1], key=lambda p: p.name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests-dir", default="tests")
    parser.add_argument("--group", choices=("browser", "proxy", "unit", "verify"), required=True)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--index", type=int, default=1)
    args = parser.parse_args(argv)

    tests_dir = Path(args.tests_dir)
    buckets = classify(tests_dir)
    if args.group == "verify":
        all_files = sorted(tests_dir.glob("test_*.py"))
        combined = buckets["browser"] + buckets["proxy"] + buckets["unit"]
        assert len(combined) == len(all_files) and set(combined) == set(all_files), (
            "CI shard partition must cover 100% of test_*.py files without duplicates"
        )
        print(
            f"Verified {len(all_files)} test files: "
            f"unit={len(buckets['unit'])}, browser={len(buckets['browser'])}, proxy={len(buckets['proxy'])}"
        )
        return 0

    selected = shard_files(buckets[args.group], args.shards, args.index)
    for path in selected:
        print(path.as_posix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
