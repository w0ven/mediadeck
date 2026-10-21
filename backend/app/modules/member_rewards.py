"""Shared operator rewards, not purchasable catalogue products.

/gift remains an administrator action. Retiring the legacy shop must not
remove renewal, bandwidth or invitation management.
"""
from __future__ import annotations

import json
import time

from app.modules.economy_rules import economy_write

GB = 1024 ** 3
KBPS_PER_MBPS = 1024
REWARD_KINDS = ("days", "bandwidth", "invite")


class RewardError(ValueError):
    pass


def grant_reward(db, members, user_id, kind, amount, actor="operator"):
    if kind not in REWARD_KINDS:
        raise RewardError("类型必须是 days/bandwidth/invite 之一（仅管理奖励）")
    if type(amount) is not int or not 1 <= amount <= 1_000_000:
        raise RewardError("数量必须是1–1000000之间的整数")
    user_id = str(user_id)
    now = int(time.time())
    with economy_write(db) as conn:
        member = members.get(user_id)
        if not member:
            raise RewardError("账号不存在")
        overrides = dict(member.get("overrides") or {})
        if kind == "days":
            effective = member.get("expires_at_effective")
            if not effective:
                raise RewardError("永久用户无需增加天数，请先明确设置有效期")
            expires = max(now, int(effective)) + amount * 86400
            field, value = "expires_at", expires
            if "expires_at_override" in overrides:
                overrides["expires_at_override"] = expires
                field, value = "overrides_json", json.dumps(overrides, ensure_ascii=False, sort_keys=True)
            conn.execute(
                f"UPDATE members SET {field}=?,status=CASE WHEN status IN ('expired','exhausted') "
                "THEN 'active' ELSE status END,updated_at=? WHERE emby_user_id=?",
                (value, now, user_id),
            )
            note = f"+{amount} 天"
        elif kind == "bandwidth":
            current = int(overrides.get(
                "bandwidth_limit_kbps", (member.get("bandwidth_limit_kbps") or 0)
                - (member.get("card_contributions") or {}).get("bandwidth_limit_kbps", 0),
            ) or 0)
            if current <= 0:
                raise RewardError("账号已不限速，无需提速")
            overrides["bandwidth_limit_kbps"] = current + amount * KBPS_PER_MBPS
            conn.execute(
                "UPDATE members SET overrides_json=?,updated_at=? WHERE emby_user_id=?",
                (json.dumps(overrides, ensure_ascii=False, sort_keys=True), now, user_id),
            )
            note = f"+{amount}Mbps 限速"
        else:
            conn.execute(
                "UPDATE members SET invite_quota=COALESCE(invite_quota,0)+?,updated_at=? WHERE emby_user_id=?",
                (amount, now, user_id),
            )
            note = f"+{amount} 个邀请名额"
        members.audit(actor, "member.reward", user_id, f"{kind} {amount} {note}", conn=conn)
    return note
