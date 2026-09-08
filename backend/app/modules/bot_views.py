"""Small shared views: self-service and admin cards use the same measurements."""

from __future__ import annotations

import time
from html import escape
from typing import Any


def duration(seconds: int | None) -> str:
    if seconds is None:
        return "暂不可用"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}秒"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours}小时{minutes}分" if hours else f"{minutes}分"


def bytes_label(value: int | None) -> str:
    if value is None:
        return "暂无实测记录"
    n = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return ""


def quota_lines(member: dict[str, Any]) -> list[str]:
    measured = member.get("quota_source") == "measured"
    sample = member.get("metering") or {}
    used = member.get("measured_used_bytes") if measured else member.get("traffic_used_bytes")
    quota = int(member.get("traffic_quota_bytes") or 0)
    label = "本月实测流量" if measured else "旧估算流量"
    if used is None:
        lines = [
            f"{label}：本月尚无实测记录"
            if sample.get("measurement_status") == "no_usage_records"
            else f"{label}：暂未测得",
            f"流量配额：{bytes_label(quota) if quota else '不限'}",
        ]
    else:
        pct = float(used) / quota * 100 if quota else None
        bar = ""
        if pct is not None:
            n = round(min(100, max(0, pct)) / 10)
            bar = "▰" * n + "▱" * (10 - n) + f"  {pct:.1f}%\n"
        lines = [
            label,
            bar + bytes_label(int(used)) + (" / " + bytes_label(quota) if quota else " · 不限额"),
        ]
    if measured:
        cov = sample.get("coverage") or {}
        if cov.get("degraded"):
            missing = "、".join(str(n["name"]) for n in cov.get("nodes", []) if not n.get("ok"))
            lines.append(
                "⚠ 采集不完整" + ("：" + escape(missing) if missing else "") + "，用量待核实"
            )
        elif sample.get("as_of"):
            lines.append(f"采集正常 · 更新于 {max(0, int(time.time() - sample['as_of']))} 秒前")
    bw = int(member.get("bandwidth_limit_kbps") or 0)
    cap = f"{bw / 1000:g} Mbps" if bw >= 1000 else (f"{bw} kbps" if bw else "不限")
    lines.append("带宽上限：" + cap)
    return lines
