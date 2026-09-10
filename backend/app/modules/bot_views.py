"""Small shared views: self-service and admin cards use the same measurements."""

from __future__ import annotations

import secrets
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


def poetry_line() -> str:
    verse, _source = secrets.choice(POEMS)
    return f'<b>{escape(verse)}</b>'


def member_mention(member: dict[str, Any]) -> str:
    name = escape(str(member.get('username') or '—'))
    tg_id = str(member.get('tg_user_id') or '')
    if tg_id.isascii() and tg_id.isdigit() and 0 < int(tg_id) < 2**63:
        return f'<a href="tg://user?id={int(tg_id)}">{name}</a>'
    return name


def watch_rank_label(row: dict[str, Any]) -> str:
    """Nickname first, then @username. Never the Emby account name."""
    nick = str(row.get("tg_display_name") or "").strip()
    if nick:
        return nick
    handle = str(row.get("tg_username") or "").strip().lstrip("@")
    if handle:
        return f"@{handle}"
    tg_id = str(row.get("tg_user_id") or "")
    if tg_id.isascii() and tg_id.isdigit() and 0 < int(tg_id) < 2**63:
        return "Telegram用户"
    return "未绑定"


def watch_rank_mention(row: dict[str, Any]) -> str:
    label = escape(watch_rank_label(row))
    tg_id = str(row.get("tg_user_id") or "")
    if tg_id.isascii() and tg_id.isdigit() and 0 < int(tg_id) < 2**63:
        return f'<a href="tg://user?id={int(tg_id)}">{label}</a>'
    return label


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
    label = '本月流量' + ('' if measured else '（估算）')
    no_records = (measured and used is None and sample.get('measurement_status') == 'no_usage_records'
                  and (sample.get('coverage') or {}).get('degraded') is False)
    used_text = (bytes_label(int(used)) if used is not None else
                 '本周期暂无播放记录' if no_records else '计量暂不可用')
    remaining = ('不限' if not quota else bytes_label(quota) if no_records else
                 bytes_label(max(0, quota - int(used))) if used is not None else '暂无法确认')
    lines = [f'📊 <b>{label}</b>', f'已用：<b>{used_text}</b>', f'剩余：<b>{remaining}</b>']
    if measured and (sample.get('coverage') or {}).get('degraded'):
        lines.append('⚠ 数据不完整')
    return lines


def bandwidth_lines(member: dict[str, Any]) -> list[str]:
    bw = int(member.get('bandwidth_limit_kbps') or 0)
    cap = f'{bw / 1000:g} Mbps' if bw >= 1000 else (f'{bw} kbps' if bw else '不限')
    return ['⚡ <b>带宽</b>', f'上限：<b>{cap}</b>']
