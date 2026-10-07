"""Auditable, domain-separated HMAC draws; no RNG state or configuration rerolls."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
RULE_VERSION = "watch-checkin-v1"
PPM = 1_000_000
CARD_KINDS = ("invite_card", "bandwidth_card", "streams_card", "title_card", "whitelist_card")
BAG_KINDS = CARD_KINDS + ("custom",)
DEFAULT_WHITELIST_CARD = {"kind": "whitelist_card", "name": "白名单卡", "cost": 5000,
                          "amount": 1, "duration_days": 0}
DEFAULT_CARDS = [
    {"kind": "invite_card", "name": "邀请码卡", "cost": 500, "amount": 1, "duration_days": 0},
    {"kind": "bandwidth_card", "name": "带宽 +10Mbps 30天", "cost": 300, "amount": 10, "duration_days": 30},
    {"kind": "streams_card", "name": "同播 +1 30天", "cost": 600, "amount": 1, "duration_days": 30},
    {"kind": "title_card", "name": "自定义称号卡", "cost": 200, "amount": 1, "duration_days": 0},
]


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def day_bounds(now):
    current = datetime.fromtimestamp(now, BEIJING)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.date().isoformat(), start.timestamp(), (start + timedelta(days=1)).timestamp()


def unbiased_map(value: int, size: int, bits: int = 256):
    """Reject the incomplete top bucket, rather than introducing modulo bias."""
    if not 1 <= size <= 2**bits or not 0 <= value < 2**bits:
        raise ValueError("抽样范围无效")
    limit = 2**bits - (2**bits % size)
    return value % size if value < limit else None


def draw(secret: str, user: str, day: str, domain: str, size: int):
    if len(secret.encode()) < 32:
        raise ValueError("签到密钥未配置（需至少32字节持久secret）")
    counter = 0
    while True:
        message = encode([RULE_VERSION, str(user), day, domain, counter]).encode()
        value = int.from_bytes(hmac.new(secret.encode(), message, hashlib.sha256).digest(), "big")
        result = unbiased_map(value, size)
        if result is not None:
            return result
        counter += 1


def watch_progress(db, user, now):
    day, start, end = day_bounds(now)
    # Clip each independent proof to the Beijing calendar day, then add. Never
    # infer time from a live wall clock or from an unfinished play's duration.
    row = (
        db.one(
            "SELECT COALESCE(SUM(MIN(ended_at,?)-MAX(started_at,?)),0) AS seconds "
            "FROM watch_verified_samples WHERE emby_user_id=? AND ended_at>? AND started_at<?",
            (min(now, end), start, str(user), start, min(now, end)),
        )
        or {}
    )
    return day, max(0.0, float(row.get("seconds") or 0))


def default_holidays():
    rows = []
    lunar = {2026: ("02-17", "06-19", "09-25"), 2027: ("02-06", "06-09", "09-15")}
    for year in (2026, 2027):
        dates = [
            ("01-01", "元旦"),
            ("05-01", "劳动节"),
            ("10-01", "国庆节"),
            ("12-25", "圣诞节"),
            ("07-04", "美国独立日"),
        ]
        dates += list(zip(lunar[year], ("春节", "端午节", "中秋节")))
        first = date(year, 11, 1)
        thanksgiving = first + timedelta(days=(3 - first.weekday()) % 7 + 21)
        dates.append((thanksgiving.strftime("%m-%d"), "感恩节"))
        # Gregorian Easter (annual dates, not a fixed Gregorian approximation).
        a = year % 19
        b = year // 100
        c = year % 100
        d = b // 4
        e = b % 4
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i = c // 4
        k = c % 4
        l = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * l) // 451
        n = h + l - 7 * m + 114
        dates.append((f"{n // 31:02d}-{n % 31 + 1:02d}", "复活节"))
        rows.extend({"date": f"{year}-{md}", "name": name} for md, name in dates)
    return sorted(rows, key=lambda r: r["date"])


def percent_tiers(raw):
    """Old fixed bonus numbers become percentages, never rewrite settled results."""
    tiers = json.loads(raw)
    if isinstance(tiers, list):
        tiers = [{'days': t['days'], 'percent': t['bonus']}
                 if isinstance(t, dict) and set(t) == {'days', 'bonus'} else t for t in tiers]
    return tiers


def validate_checkin(config):
    tiers = percent_tiers(config["streak_tiers"])
    if not isinstance(tiers, list) or not tiers:
        raise ValueError("连签阶梯必须是非空JSON数组")
    last = 0
    for tier in tiers:
        if (
            not isinstance(tier, dict)
            or set(tier) != {"days", "percent"}
            or type(tier["days"]) is not int
            or type(tier["percent"]) is not int
        ):
            raise ValueError('阶梯格式为 {"days":天数,"percent":加成百分比}')
        if not last < tier["days"] <= 100000 or not 0 <= tier["percent"] <= 100000:
            raise ValueError("连签天数须递增、加成百分比为非负整数")
        last = tier["days"]
    if tiers[0]["days"] != 1:
        raise ValueError("连签阶梯必须从第1天开始")
    config['streak_tiers'] = encode(tiers)
    holidays = json.loads(config["holidays"])
    if not isinstance(holidays, list):
        raise ValueError("节日必须是JSON数组")  # noqa: TRY004 - public validation contract
    seen = set()
    for holiday in holidays:
        if not isinstance(holiday, dict) or not str(holiday.get("name") or "").strip():
            raise ValueError("节日必须包含date/name")
        if set(holiday) - {"date", "name", "double_ppm", "multiplier", "drops"}:
            raise ValueError("节日包含未知字段")
        day = date.fromisoformat(holiday.get("date", "")).isoformat()
        if day in seen:
            raise ValueError("同日节日只能配置一条（不叠加）")
        seen.add(day)
        validate_activity(
            {
                **activity_defaults(config),
                **{k: v for k, v in holiday.items() if k not in ("name", "date")},
            }
        )
    validate_activity(activity_defaults(config))
    return config


def activity_defaults(config):
    return {
        "double_ppm": config["double_ppm"],
        "multiplier": config["multiplier"],
        "drops": json.loads(config["drops"]),
    }


def validate_activity(activity):
    for key, lo, hi in (("double_ppm", 0, PPM), ("multiplier", 1, 100)):
        if type(activity.get(key)) is not int or not lo <= activity[key] <= hi:
            raise ValueError(f"{key}必须是{lo}..{hi}的整数")
    drops = activity.get("drops")
    if not isinstance(drops, list):
        raise ValueError("drops必须是数组")  # noqa: TRY004 - public validation contract
    total = 0
    for drop in drops:
        if not isinstance(drop, dict) or set(drop) not in ({"ppm", "item_id"}, {"ppm", "spec"}):
            raise ValueError("掉落格式为ppm加item_id或spec")
        probability = drop.get("ppm")
        if type(probability) is not int or not 0 <= probability <= PPM:
            raise ValueError("掉落ppm必须为0..1000000整数")
        total += probability
        if "item_id" in drop:
            if type(drop["item_id"]) is not int or drop["item_id"] <= 0:
                raise ValueError("item_id无效")
        else:
            validate_card(drop.get("spec"))
    if total > PPM:
        raise ValueError("掉落概率总和不能超过100%")


def validate_card(spec):
    if not isinstance(spec, dict) or spec.get("kind") not in CARD_KINDS:
        raise ValueError("掉落/背包必须使用道具商品")
    if not str(spec.get("name") or "").strip():
        raise ValueError("道具名不能为空")
    for key, lo, hi in (("amount", 1, 1000000), ("duration_days", 0, 36500)):
        if type(spec.get(key)) is not int or not lo <= spec[key] <= hi:
            raise ValueError(f"{key}无效")
    if spec["kind"] in ("bandwidth_card", "streams_card") and not spec["duration_days"]:
        raise ValueError("带宽/同播卡期限必须大于0")
    if spec["kind"] == "invite_card" and spec["duration_days"] != 0:
        raise ValueError("邀请码卡增加现有邀请名额，不设置到期（期限须为0）")
    if spec["kind"] in ("title_card", "invite_card", "whitelist_card") and spec["amount"] != 1:
        raise ValueError("每张称号/邀请码卡数量必须为1")
    return spec


def validate_bag_spec(spec):
    if not isinstance(spec, dict) or spec.get('kind') != 'custom':
        return validate_card(spec)
    if not str(spec.get('name') or '').strip() or spec.get('amount') != 1:
        raise ValueError('自定义商品须有名称且数量为1')
    if not isinstance(spec.get('purchase_notice'), str) or not 1 <= len(spec['purchase_notice'].strip()) <= 12000:
        raise ValueError('购买后说明须为1..12000字符')
    if type(spec.get('retention_days')) is not int or not 1 <= spec['retention_days'] <= 36500:
        raise ValueError('背包保留天数须为1..36500整数')
    return spec


def activity_for(config, now):
    today = datetime.fromtimestamp(now, BEIJING).date()
    defaults = activity_defaults(config)
    for holiday in json.loads(config["holidays"]):
        if holiday["date"] == today.isoformat():
            return dict(
                defaults,
                **{k: v for k, v in holiday.items() if k not in ("date",)},
                event="holiday",
            )
    if config["weekends"] and today.weekday() >= 5:
        return dict(defaults, name="周末", event="weekend")
    return {"double_ppm": 0, "multiplier": 1, "drops": [], "name": "平日", "event": "normal"}


@contextmanager
def economy_write(db):
    # Serialize read/check/write across connections and processes, not merely
    # the Python lock. No nested calls to db.execute are allowed in this block.
    with db.write() as conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        yield conn


def receipt(conn, scope, key, user, request):
    if not key or len(str(key)) > 120:
        raise ValueError("缺少有效幂等请求ID")
    row = conn.execute(
        "SELECT * FROM economy_receipts WHERE scope=? AND request_id=? AND user_id=?",
        (scope, str(key), str(user)),
    ).fetchone()
    if row:
        if row["request_json"] != encode(request):
            raise ValueError("请求ID已用于不同操作")
        return json.loads(row["result_json"])
    return None


def save_receipt(conn, scope, key, user, request, result):
    conn.execute(
        "INSERT INTO economy_receipts VALUES(?,?,?,?,?)",
        (scope, str(key), str(user), encode(request), encode(result)),
    )
