"""The points shop: what points are for.

An item is data, not code. The four ``kind`` values map to the four things the
panel can already grant a member -- traffic, days, bandwidth, invite slots --
and everything else about an item (price, size, stock limit, whether it is
visible at all) is a row an operator edits. Adding a promotion is a form
submission, not a release.

The one rule the whole file exists to enforce: **points are never spent
without the reward being delivered.** Debit and fulfilment happen in one
transaction, so a failure at any step leaves the member exactly as they were.
The opposite failure -- delivering without charging -- is equally excluded by
the same transaction, but it is the cheap one; a member charged for nothing is
the one who stops trusting the shop.

Seeded items ship disabled. A default catalogue that is live on first boot
would let members spend points on prices the operator never chose, and the
first thing they would notice is the bill.
"""
from __future__ import annotations

import contextlib
import json
import time
from typing import Any

from app.modules.groups import needs_traffic

GB = 1024 ** 3
KBPS_PER_MBPS = 1024

KINDS = ("traffic", "days", "bandwidth", "invite")

KIND_LABELS = {
    "traffic": "流量包",
    "days": "会员天数",
    "bandwidth": "带宽提速",
    "invite": "邀请名额",
}

# Unit shown next to ``amount`` on the card, per kind.
KIND_UNITS = {
    "traffic": "GB",
    "days": "天",
    "bandwidth": "Mbps",
    "invite": "个",
}

SEED_ITEMS = (
    {"kind": "traffic", "name": "流量包 50GB",
     "description": "为当前计费周期增加 50GB 额外流量", "cost": 100, "amount": 50},
    {"kind": "days", "name": "会员 7 天",
     "description": "有效期延长 7 天", "cost": 200, "amount": 7},
    {"kind": "bandwidth", "name": "提速 10Mbps",
     "description": "在当前限速基础上提高 10Mbps；不限速的账号无需兑换",
     "cost": 300, "amount": 10},
    {"kind": "invite", "name": "邀请名额 1 个",
     "description": "获得 1 个邀请名额，可生成邀请码", "cost": 500, "amount": 1},
)


class ShopError(Exception):
    """Refusal a member is allowed to read: price, stock, or availability."""


def _as_int(raw: Any, label: str, lo: int, hi: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ShopError(f"{label}必须是整数") from None
    if not lo <= value <= hi:
        raise ShopError(f"{label}必须在 {lo}–{hi} 之间")
    return value


def validate_item(payload: Any, *, partial: bool = False) -> dict[str, Any]:
    """Whitelist and range-check one item. Raises ShopError on bad input."""
    if not isinstance(payload, dict):
        raise ShopError("商品格式错误")
    out: dict[str, Any] = {}

    if "kind" in payload or not partial:
        kind = str(payload.get("kind") or "").strip()
        if kind not in KINDS:
            raise ShopError(f"类型必须是 {'/'.join(KINDS)} 之一")
        out["kind"] = kind
    if "name" in payload or not partial:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ShopError("名称不能为空")
        out["name"] = name[:80]
    if "description" in payload:
        out["description"] = str(payload.get("description") or "").strip()[:300]
    if "cost" in payload or not partial:
        out["cost"] = _as_int(payload.get("cost"), "消耗积分", 1, 1_000_000)
    if "amount" in payload or not partial:
        out["amount"] = _as_int(payload.get("amount"), "数量", 1, 1_000_000)
    if "per_user_limit" in payload:
        out["per_user_limit"] = _as_int(
            payload.get("per_user_limit"), "每人限购", 0, 10_000)
    if "sort" in payload:
        out["sort"] = _as_int(payload.get("sort"), "排序", -10_000, 10_000)
    if "enabled" in payload:
        out["enabled"] = 1 if payload.get("enabled") else 0
    return out


def _decorate(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    kind = str(out.get("kind") or "")
    out["kind_label"] = KIND_LABELS.get(kind, kind)
    out["unit"] = KIND_UNITS.get(kind, "")
    out["enabled"] = bool(out.get("enabled"))
    return out


class ShopService:
    """Catalogue, orders, and the one method that spends points."""

    def __init__(self, db: Any, members: Any, points: Any) -> None:
        self._db = db
        self._members = members
        self._points = points

    # -- catalogue -----------------------------------------------------------

    def seed_defaults(self) -> int:
        """Insert the starter catalogue once, disabled. Returns rows added.

        Guarded on the table being empty rather than on a marker row: an
        operator who deleted every item meant to have no catalogue, and
        re-seeding on the next restart would silently undo that. It only runs
        on a shop nobody has touched.
        """
        existing = self._db.one("SELECT COUNT(*) AS n FROM shop_items") or {}
        if int(existing.get("n") or 0) > 0:
            return 0
        now = int(time.time())
        added = 0
        for index, item in enumerate(SEED_ITEMS):
            self._db.execute(
                "INSERT INTO shop_items"
                "(kind,name,description,cost,amount,enabled,per_user_limit,"
                "sort,created_at) VALUES(?,?,?,?,?,0,0,?,?)",
                (item["kind"], item["name"], item["description"],
                 item["cost"], item["amount"], index, now))
            added += 1
        return added

    def items(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM shop_items"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY sort ASC, id ASC"
        return [_decorate(r) for r in self._db.query(sql)]

    def get(self, item_id: int) -> dict[str, Any] | None:
        row = self._db.one("SELECT * FROM shop_items WHERE id=?", (int(item_id),))
        return _decorate(row) if row else None

    def create(self, payload: Any, actor: str = "operator") -> dict[str, Any]:
        clean = validate_item(payload)
        now = int(time.time())
        with self._db.write() as conn:
            cur = conn.execute(
                "INSERT INTO shop_items"
                "(kind,name,description,cost,amount,enabled,per_user_limit,"
                "sort,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (clean["kind"], clean["name"], clean.get("description", ""),
                 clean["cost"], clean["amount"],
                 int(clean.get("enabled", 1)), int(clean.get("per_user_limit", 0)),
                 int(clean.get("sort", 0)), now))
            item_id = int(cur.lastrowid or 0)
        self._audit(actor, "shop.item.create", str(item_id),
                    f"{clean['kind']} {clean['name']} cost={clean['cost']}")
        return self.get(item_id) or {}

    def update(self, item_id: int, payload: Any,
               actor: str = "operator") -> dict[str, Any]:
        item = self.get(item_id)
        if not item:
            raise KeyError(item_id)
        clean = validate_item(payload, partial=True)
        if not clean:
            return item
        sets = ", ".join(f"{k}=?" for k in clean)
        self._db.execute(
            f"UPDATE shop_items SET {sets} WHERE id=?",
            (*clean.values(), int(item_id)))
        self._audit(actor, "shop.item.update", str(item_id),
                    json.dumps(clean, ensure_ascii=False)[:300])
        return self.get(item_id) or {}

    def delete(self, item_id: int, actor: str = "operator") -> bool:
        item = self.get(item_id)
        if not item:
            return False
        # Orders survive on purpose: they answer "why does this member have
        # extra traffic" long after the item was retired.
        self._db.execute("DELETE FROM shop_items WHERE id=?", (int(item_id),))
        self._audit(actor, "shop.item.delete", str(item_id),
                    str(item.get("name") or ""))
        return True

    # -- orders --------------------------------------------------------------

    def orders(self, user_id: str | None = None,
               limit: int = 50) -> list[dict[str, Any]]:
        sql = ("SELECT o.*, COALESCE(m.username,'') AS username "
               "FROM shop_orders o "
               "LEFT JOIN members m ON m.emby_user_id = o.emby_user_id")
        params: list[Any] = []
        if user_id:
            sql += " WHERE o.emby_user_id=?"
            params.append(str(user_id))
        sql += " ORDER BY o.id DESC LIMIT ?"
        params.append(max(1, min(int(limit or 50), 500)))
        rows = self._db.query(sql, tuple(params))
        for row in rows:
            row["kind_label"] = KIND_LABELS.get(str(row.get("kind")), row.get("kind"))
            row["unit"] = KIND_UNITS.get(str(row.get("kind")), "")
        return rows

    def redeemed_count(self, user_id: str, item_id: int) -> int:
        row = self._db.one(
            "SELECT COUNT(*) AS n FROM shop_orders "
            "WHERE emby_user_id=? AND item_id=?", (str(user_id), int(item_id)))
        return int((row or {}).get("n") or 0)

    # -- the one method that spends points -----------------------------------

    def redeem(self, user_id: str, item_id: int,
               actor: str = "member") -> dict[str, Any]:
        """Charge points and deliver, or change nothing at all.

        The debit, the grant and the order row are one transaction. Ordering is
        deliberate: points are taken *first*, so an insufficient balance stops
        the reward before it is granted, and any later failure rolls the debit
        back with everything else.
        """
        # Hold the same DB lock for validation and fulfilment: concurrent
        # orders must see the preceding order's limits and personal overlay.
        with self._db.write() as conn:
            return self._redeem(conn, user_id, item_id, actor)

    def _redeem(self, conn: Any, user_id: str, item_id: int,
                actor: str) -> dict[str, Any]:
        user_id = str(user_id or "")
        item = self.get(item_id)
        if not item:
            raise ShopError("商品不存在")
        if not item["enabled"]:
            raise ShopError("该商品已下架")
        member = self._members.get(user_id) if self._members else None
        if not member:
            raise ShopError("账号不存在")

        limit = int(item.get("per_user_limit") or 0)
        if limit > 0 and self.redeemed_count(user_id, item_id) >= limit:
            raise ShopError(f"该商品每人限兑 {limit} 次，你已达上限")

        kind = str(item["kind"])
        amount = int(item["amount"])
        cost = int(item["cost"])

        # Checked before charging so a member is told "already unlimited"
        # rather than being billed for a no-op.
        if kind == "bandwidth":
            current = int((member.get("overrides") or {}).get(
                "bandwidth_limit_kbps",
                member.get("bandwidth_limit_kbps") or 0) or 0)
            if current <= 0:
                raise ShopError("你的账号已是不限速，无需兑换提速")

        now = int(time.time())
        balance = self._points._apply(
            conn, user_id, -cost, "shop.redeem", f"item:{item_id}", actor, now)
        note = self._grant(conn, user_id, member, kind, amount)
        conn.execute(
            "INSERT INTO shop_orders"
            "(emby_user_id,item_id,item_name,cost,kind,amount,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (user_id, int(item_id), str(item["name"]), cost, kind, amount, now))
        conn.execute(
            "INSERT INTO audit_log(ts,actor,action,subject,detail,ok) VALUES(?,?,?,?,?,1)",
            (now, actor, 'shop.redeem', user_id, f'item={item_id} cost={cost} {note}'))
        return {
            "ok": True,
            "item": item,
            "cost": cost,
            "balance": balance,
            "granted": note,
        }

    def grant(self, user_id: str, kind: str, amount: int,
              actor: str = "operator") -> str:
        """Deliver a reward without charging for it.

        Exists so the admin /gift command hands out exactly what the shop
        hands out. A second implementation would drift -- the shop's "+50GB"
        and an admin's "+50GB" have to be the same write, or the two paths
        eventually disagree about what a gift even is.
        """
        kind = str(kind or "").strip()
        if kind not in KINDS:
            raise ShopError(f"类型必须是 {'/'.join(KINDS)} 之一")
        amount = _as_int(amount, "数量", 1, 1_000_000)
        with self._db.write() as conn:
            member = self._members.get(str(user_id)) if self._members else None
            if not member:
                raise ShopError("账号不存在")
            note = self._grant(conn, str(user_id), member, kind, amount)
        self._audit(actor, "shop.grant", str(user_id), f"{kind} {amount} {note}")
        return note

    def _grant(self, conn: Any, user_id: str, member: dict[str, Any],
               kind: str, amount: int) -> str:
        """Deliver one reward on the caller's open transaction.

        Written against the connection rather than through MemberService
        because those methods commit on their own: calling them here would put
        the grant outside the transaction that guards the debit, which is
        exactly the split this class exists to prevent.
        """
        now = int(time.time())
        if kind == "traffic":
            if not needs_traffic(member.get('billing_mode') or ''):
                raise ShopError('账号不计流量，无需兑换流量包')
            overrides = dict(member.get("overrides") or {})
            before = int(overrides.get("extra_traffic_bytes") or 0)
            overrides["extra_traffic_bytes"] = before + amount * GB
            conn.execute(
                "UPDATE members SET overrides_json=?,updated_at=? "
                "WHERE emby_user_id=?",
                (json.dumps(overrides, ensure_ascii=False, sort_keys=True),
                 now, user_id))
            return f"+{amount}GB 流量"

        if kind == "days":
            effective = member.get('expires_at_effective')
            if not effective:
                raise ShopError('永久用户无需兑换天数，请先明确设置有效期')
            expires = max(now, int(effective)) + amount * 86400
            overrides = dict(member.get('overrides') or {})
            field, value = 'expires_at', expires
            if 'expires_at_override' in overrides:
                overrides['expires_at_override'] = expires
                field, value = 'overrides_json', json.dumps(overrides, ensure_ascii=False, sort_keys=True)
            conn.execute(
                f"UPDATE members SET {field}=?, status=CASE WHEN status IN "
                "('expired','exhausted') THEN 'active' ELSE status END,"
                "updated_at=? WHERE emby_user_id=?", (value, now, user_id))
            return f"+{amount} 天"

        if kind == "bandwidth":
            overrides = dict(member.get("overrides") or {})
            current = int(overrides.get(
                "bandwidth_limit_kbps",
                member.get("bandwidth_limit_kbps") or 0) or 0)
            if current <= 0:
                raise ShopError('账号已不限速，无需提速')
            overrides["bandwidth_limit_kbps"] = current + amount * KBPS_PER_MBPS
            conn.execute(
                "UPDATE members SET overrides_json=?,updated_at=? "
                "WHERE emby_user_id=?",
                (json.dumps(overrides, ensure_ascii=False, sort_keys=True),
                 now, user_id))
            return f"+{amount}Mbps 限速"

        if kind == "invite":
            conn.execute(
                "UPDATE members SET invite_quota=COALESCE(invite_quota,0)+?,"
                "updated_at=? WHERE emby_user_id=?", (amount, now, user_id))
            return f"+{amount} 个邀请名额"

        raise ShopError(f"未知商品类型：{kind}")

    def _audit(self, actor: str, action: str, subject: str,
               detail: str) -> None:
        if self._members is None:
            return
        # An audit write that fails must not undo a grant that already
        # committed: the member has the goods either way, and raising here
        # would only make the caller think they do not.
        with contextlib.suppress(Exception):
            self._members.audit(actor, action, subject, detail)
