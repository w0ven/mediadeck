"""Opt-in normal → luck check-in; the final reward is positive, adjustments may not be."""
from __future__ import annotations

import json
import time

from app.core.config import settings
from app.modules.economy_rules import (
    PPM,
    activity_for,
    day_bounds,
    draw,
    encode,
    validate_checkin,
    watch_progress,
)
from app.modules.inventory import InventoryService

VERSION = "dual-checkin-v1"
LUCK_SIZE = 1000
LUCK_BANDS = ((1, 10, 65), (11, 20, 25), (21, 30, 10))
WARNING = "结果可能更低，覆盖后不可恢复；运气签到当天不可重抽。"


def luck_value(roll):
    if type(roll) is not int or not 0 <= roll < LUCK_SIZE:
        raise ValueError("运气签到抽样范围无效")
    for low, high, weight in LUCK_BANDS:
        width = (high-low+1) * weight
        if roll < width:
            return low + roll // weight
        roll -= width
    raise AssertionError("unreachable")


def checkin(plugin, conn, points, uid, now=None, mode="normal"):
    """Caller owns economy_write; all gates precede drawing or awarding any item."""
    if mode not in ("normal", "luck"):
        raise ValueError("签到类型无效")
    now = time.time() if now is None else now
    db = plugin.ctx.db
    day = day_bounds(now)[0]
    prior_row = conn.execute("SELECT * FROM checkins WHERE emby_user_id=? AND day=?", (uid, day)).fetchone()
    prior = dict(prior_row) if prior_row else None
    saved = json.loads(prior["result_json"] or "{}") if prior else None
    if prior and (saved.get("calculation_version") != VERSION or saved.get("mode") == "luck" or mode == "normal"):
        return {"ok": False, "reason": "今天已完成运气签到，不可重抽或恢复普通" if saved.get("mode") == "luck"
                else "今天已签到" if saved.get("calculation_version") == VERSION
                else "今天已按旧规则结算，明天可使用双签到",
                "streak": prior["streak"], "balance": points.balance(uid), "saved_result": saved}
    member = plugin.ctx.members.get(uid) if plugin.ctx.members else None
    if not member:
        return {"ok": False, "reason": "账号不存在"}
    if member.get('activity_managed') and member.get('state') != 'active':
        return {'ok': False, 'reason': '账号当前不可用；到期保留期间仅可查看与达标续费', 'balance': points.balance(uid)}
    _, watched = watch_progress(db, uid, now)
    if watched < 600:
        return {"ok": False, "reason": f"今日有效观影 {int(watched)}/600 秒，还需 {max(0, int(600-watched+0.999))} 秒",
                "watched_seconds": watched, "required_seconds": 600, "balance": points.balance(uid)}
    previous_points = int(prior["points"]) if prior else 0
    if prior:
        recovery = max(0, previous_points-1)
        available = points.balance(uid)  # play_money escrow already leaves the spendable ledger
        if available < recovery:
            return {"ok": False, "reason": f"覆盖最多回收{recovery}积分，可用余额{available}，还缺{recovery-available}积分；未抽取",
                    "balance": available, "required_balance": recovery, "shortfall": recovery-available}
    config = plugin._config()
    validate_checkin(config)
    secret = settings().mediadeck_checkin_secret
    roll = draw(secret, uid, day, VERSION+":luck", LUCK_SIZE) if mode == "luck" else None
    award = luck_value(roll) if mode == "luck" else 10
    if prior:
        result = dict(saved)
    else:
        yesterday = day_bounds(now-86400)[0]
        yesterday_row = conn.execute("SELECT streak FROM checkins WHERE emby_user_id=? AND day=?", (uid, yesterday)).fetchone()
        streak = (int(yesterday_row[0]) if yesterday_row else 0) + 1
        activity = activity_for(config, now)
        drop_roll = draw(secret, uid, day, VERSION+":drop", PPM)
        drop = None
        cumulative = 0
        for candidate in activity["drops"]:
            cumulative += candidate["ppm"]
            if drop_roll < cumulative:
                if "item_id" in candidate:
                    shop = getattr(plugin.ctx, "shop", None)
                    drop = shop.get(candidate["item_id"]) if shop else None
                    if not drop:
                        raise ValueError("活动掉落商品不存在，请管理员修正配置")
                else:
                    drop = dict(candidate["spec"])
                break
        result = {"streak": streak, "activity": activity["name"], "rule_snapshot": config,
                  "drop_spec": drop, "drop_roll": drop_roll}
        if drop:
            result["card_id"] = InventoryService.add(conn, uid, drop, "checkin:"+day, now)
    delta = award - previous_points
    balance = points._apply(conn, uid, delta, "checkin.adjust" if prior else "checkin",
                            day, "checkin", int(now)) if delta else points.balance(uid)
    result.update(ok=True, points=award, delta=delta, previous_points=previous_points,
                  base=award, bonus=0, multiplier=1, streak_percent=0, balance=balance,
                  calculation_version=VERSION, mode=mode, luck_roll=roll,
                  watched_seconds=watched, warning=WARNING, rule_version=VERSION,
                  normal_points=10, luck_bands=LUCK_BANDS)
    if prior:
        conn.execute("UPDATE checkins SET points=?,result_json=? WHERE emby_user_id=? AND day=?",
                     (award, encode(result), uid, day))
    else:
        conn.execute("INSERT INTO checkins(emby_user_id,day,streak,points,created_at,result_json) VALUES(?,?,?,?,?,?)",
                     (uid, day, result["streak"], award, int(now), encode(result)))
    return result
