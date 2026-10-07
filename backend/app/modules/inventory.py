"""Non-transferable cards and additive, independently expiring contributions."""

from __future__ import annotations

import json
import re
import time
import unicodedata
from typing import Any

from app.modules.economy_rules import economy_write, encode, validate_card


def validate_title(raw):
    tag = unicodedata.normalize("NFKC", str(raw or "")).strip()
    if not 1 <= len(tag) <= 16:
        raise ValueError("称号须为1..16字符")
    # Strict text alphabet rejects all emoji (including keycaps, flags, ZWJ,
    # variation selectors), URLs, controls, and invisible impersonation text.
    if any(unicodedata.category(c)[0] not in ("L", "N") and c not in " -_·" for c in tag):
        raise ValueError("称号只能包含文字、数字、空格和 -_·，不允许emoji/链接")
    if re.search(
        r"admin|owner|moderator|https?|www|管理员|管理員|群主|官方|客服|站长|站長", tag, re.IGNORECASE
    ):
        raise ValueError("称号不可冒充管理或包含链接")
    if any(c in tag for c in (".", "@", "/")):
        raise ValueError("称号不可包含链接")
    return tag


def contributions(db, user, now=None):
    now = int(time.time()) if now is None else now
    out = {"bandwidth_limit_kbps": 0, "max_streams": 0}
    for row in db.query(
        "SELECT spec_json FROM inventory WHERE emby_user_id=? AND used_at IS NOT NULL "
        "AND expires_at>?",
        (str(user), now),
    ):
        spec = json.loads(row["spec_json"])
        if spec["kind"] == "bandwidth_card":
            out["bandwidth_limit_kbps"] += spec["amount"] * 1024
        if spec["kind"] == "streams_card":
            out["max_streams"] += spec["amount"]
    return out


class InventoryService:
    def __init__(self, db, members, shop, config):
        self.db, self.members, self.shop, self.config = db, members, shop, config

    def items(self, user):
        rows = self.db.query(
            "SELECT * FROM inventory WHERE emby_user_id=? ORDER BY id DESC", (str(user),)
        )
        for row in rows:
            row["spec"] = json.loads(row.pop("spec_json"))
            row["result"] = json.loads(row.pop("result_json"))
            row["active"] = bool(
                row["used_at"] is not None and row["expires_at"] and row["expires_at"] > time.time()
            )
        return rows

    async def reconcile_expired(self, sync_effects):
        """Reissue existing signed rate caps after expiry; durable retry on failure.

        The effective limits already exclude expired rows. Synchronizing only
        touches the existing enforcement/cache path, never the original rights.
        """
        now = int(time.time())
        due = self.db.query(
            "SELECT * FROM inventory WHERE used_at IS NOT NULL AND expires_at<=? "
            "AND expiry_synced_at IS NULL AND expiry_retry_at<=? ORDER BY id LIMIT 100",
            (now, now),
        )
        by_user = {}
        for row in due:
            spec = json.loads(row["spec_json"])
            if spec["kind"] in ("bandwidth_card", "streams_card"):
                by_user.setdefault(row["emby_user_id"], []).append(row)
        for user, cards in by_user.items():
            try:
                result = await sync_effects(
                    user, {json.loads(r["spec_json"])["kind"] for r in cards}
                )
                ok = result.get("ok") is not False and result.get("remote_ok") is not False
            except Exception:  # noqa: BLE001 - failed effect remains durable and retryable
                ok = False
            with economy_write(self.db) as conn:
                for row in cards:
                    conn.execute(
                        "UPDATE inventory SET expiry_synced_at=?,expiry_retry_at=?,expiry_error=? WHERE id=?",
                        (
                            now if ok else None,
                            0 if ok else now + 60,
                            "" if ok else "到期权益远端同步未确认，待重试",
                            row["id"],
                        ),
                    )

    @staticmethod
    def add(conn, user, spec, source, now):
        validate_card(spec)
        cur = conn.execute(
            "INSERT INTO inventory(emby_user_id,spec_json,source,created_at) VALUES(?,?,?,?)",
            (str(user), encode(spec), source, int(now)),
        )
        return int(cur.lastrowid)

    def use(self, user, card_id, *, title="", now=None):
        now = int(time.time()) if now is None else int(now)
        user = str(user)
        with economy_write(self.db) as conn:
            row = conn.execute(
                "SELECT * FROM inventory WHERE id=? AND emby_user_id=?", (int(card_id), user)
            ).fetchone()
            if not row:
                raise ValueError("道具不存在或不属于你")
            if row["used_at"] is not None:
                result = json.loads(row["result_json"])
                if result.get("tag") and validate_title(title) != result["tag"]:
                    raise ValueError("称号卡已用于其他称号")
                return result
            member = self.members.get(user)
            if not member:
                raise ValueError("账号不存在")
            spec = json.loads(row["spec_json"])
            validate_card(spec)
            kind, amount = spec["kind"], spec["amount"]
            expires = now + spec["duration_days"] * 86400 if spec["duration_days"] else None
            result: dict[str, Any] = {
                "ok": True, "card_id": int(card_id), "expires_at": expires, "kind": kind
            }
            if kind in ("bandwidth_card", "streams_card"):
                field = "bandwidth_limit_kbps" if kind == "bandwidth_card" else "max_streams"
                current = member[field]
                if current <= 0:
                    raise ValueError("原权益已不限速/不限同播，无需使用")
                cfg = self.config()
                cap = (
                    cfg["bandwidth_cap_mbps"] * 1024
                    if kind == "bandwidth_card"
                    else cfg["streams_cap"]
                )
                added = amount * 1024 if kind == "bandwidth_card" else amount
                if current + added > cap:
                    limit = (
                        f"{cfg['bandwidth_cap_mbps']}Mbps"
                        if kind == "bandwidth_card"
                        else f"{cap}路"
                    )
                    raise ValueError(f"超过可叠加上限，未消耗道具（上限{limit}）")
                result.update(
                    granted=f"+{amount}{'Mbps' if kind == 'bandwidth_card' else '路'}",
                    effective=current + added,
                )
            elif kind == "invite_card":
                result["granted"] = self.shop._grant(conn, user, member, "invite", 1)
            elif kind == "title_card":
                tag = validate_title(title)
                cur = conn.execute(
                    "INSERT INTO member_titles(emby_user_id,tag,created_at,expires_at,actor) VALUES(?,?,?,?,?)",
                    (user, tag, now, expires, "member"),
                )
                result.update(
                    title_id=int(cur.lastrowid), tag=tag, granted="称号已创建，可免费佩戴/切换"
                )
            conn.execute(
                "UPDATE inventory SET used_at=?,expires_at=?,result_json=?,expiry_synced_at=? WHERE id=?",
                (
                    now,
                    expires,
                    encode(result),
                    now if kind not in ("bandwidth_card", "streams_card") else None,
                    int(card_id),
                ),
            )
            conn.execute(
                "INSERT INTO audit_log(ts,actor,action,subject,detail,ok) VALUES(?,?,?,?,?,1)",
                (now, "member", "inventory.use", user, encode(result)),
            )
            return result
