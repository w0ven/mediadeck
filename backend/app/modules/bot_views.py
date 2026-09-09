"""Small shared views: self-service and admin cards use the same measurements."""

from __future__ import annotations

import hashlib
import secrets
import time
from html import escape
from typing import Any

# Public-domain classical verse; attribution is kept with each exact line.
# This is presentation-only local text, not a configurable quotation service.
POEMS = (
    ('长风破浪会有时，直挂云帆济沧海。', '李白《行路难·其一》'),
    ('大鹏一日同风起，扶摇直上九万里。', '李白《上李邕》'),
    ('会当凌绝顶，一览众山小。', '杜甫《望岳》'),
    ('海内存知己，天涯若比邻。', '王勃《送杜少府之任蜀州》'),
    ('行到水穷处，坐看云起时。', '王维《终南别业》'),
    ('千淘万漉虽辛苦，吹尽狂沙始到金。', '刘禹锡《浪淘沙·其八》'),
    ('春风得意马蹄疾，一日看尽长安花。', '孟郊《登科后》'),
    ('欲穷千里目，更上一层楼。', '王之涣《登鹳雀楼》'),
    ('竹杖芒鞋轻胜马，谁怕？一蓑烟雨任平生。', '苏轼《定风波·莫听穿林打叶声》'),
    ('山重水复疑无路，柳暗花明又一村。', '陆游《游山西村》'),
    ('接天莲叶无穷碧，映日荷花别样红。', '杨万里《晓出净慈寺送林子方》'),
    ('随风潜入夜，润物细无声。', '杜甫《春夜喜雨》'),
    ('海上生明月，天涯共此时。', '张九龄《望月怀远》'),
    ('两岸猿声啼不住，轻舟已过万重山。', '李白《早发白帝城》'),
    ('大漠孤烟直，长河落日圆。', '王维《使至塞上》'),
)


def poetry_line(identity: str | None = None) -> str:
    if identity is None:
        verse, source = secrets.choice(POEMS)
    else:
        index = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:4], 'big') % len(POEMS)
        verse, source = POEMS[index]
    return f'「{escape(verse)}」\n<i>— {escape(source)}</i>'


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


def quota_lines(member: dict[str, Any], *, public: bool = False) -> list[str]:
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
    remaining = ('不限' if not quota else
                 bytes_label(max(0, quota - int(used))) if used is not None else '暂无法确认')
    lines.append('本月剩余：' + remaining)
    lines.append('来源：' + ('实测账本' if measured else '旧估算（非实测）'))
    if measured:
        cov = sample.get("coverage") or {}
        if cov.get("degraded"):
            missing = '' if public else "、".join(str(n["name"]) for n in cov.get("nodes", []) if not n.get("ok"))
            lines.append(
                "⚠ 采集不完整" + ("：" + escape(missing) if missing else "") + "，用量待核实"
            )
        elif sample.get("as_of"):
            lines.append(f"采集正常 · 更新于 {max(0, int(time.time() - sample['as_of']))} 秒前")
    bw = int(member.get("bandwidth_limit_kbps") or 0)
    cap = f"{bw / 1000:g} Mbps" if bw >= 1000 else (f"{bw} kbps" if bw else "不限")
    lines.append("带宽上限：" + cap)
    return lines
