"""Snapshot-based catalogue: every supported purchase enters the owner's bag.

Legacy products are retired; historical orders remain readable. Operator
account rewards are deliberately separate from purchase fulfilment.
"""
from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from typing import Any

from app.modules.economy_rules import (
    BAG_KINDS,
    DEFAULT_CARDS,
    DEFAULT_WHITELIST_CARD,
    economy_write,
    encode,
    receipt,
    save_receipt,
    validate_bag_spec,
)
from app.modules.inventory import InventoryService

KINDS = BAG_KINDS

KIND_LABELS = {
    "invite_card": "邀请码卡（入包）", "bandwidth_card": "带宽卡（入包）",
    "streams_card": "同播卡（入包）", "title_card": "称号卡（入包）",
    "whitelist_card": "白名单卡（入包）", "custom": "自定义商品",
}
KIND_UNITS = {"invite_card": "张", "bandwidth_card": "Mbps", "streams_card": "路", "title_card": "张", "whitelist_card": "张", "custom": "份"}
# Read-only labels for orders; never accepted by catalogue validation.
HISTORY_LABELS = {"traffic": "旧版流量包", "days": "旧版会员天数", "bandwidth": "旧版提速", "invite": "旧版邀请名额"}
HISTORY_UNITS = {"traffic": "GB", "days": "天", "bandwidth": "Mbps", "invite": "个"}


class ShopError(Exception):
    """Refusal a member is allowed to read: price, stock, or availability."""


def _as_int(raw: Any, label: str, lo: int, hi: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ShopError(f"{label}必须是整数") from None
    if isinstance(raw, bool) or (isinstance(raw, float) and raw != value):
        raise ShopError(f"{label}必须是整数")
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
        out["amount"] = _as_int(payload.get("amount", 1 if out.get("kind") in ("custom", "whitelist_card") else None), "数量", 1, 1_000_000)
    if "duration_days" in payload:
        out["duration_days"] = _as_int(payload["duration_days"], "期限天数", 0, 36500)
    if "purchase_notice" in payload:
        if not isinstance(payload['purchase_notice'], str):
            raise ShopError('购买后说明须为文字')
        out['purchase_notice'] = payload['purchase_notice'].strip()
        if len(out['purchase_notice']) > 12000:
            raise ShopError('购买后说明最多12000字符')
    if "retention_days" in payload:
        out['retention_days'] = _as_int(payload['retention_days'], '背包保留天数', 1, 36500)
    if "per_user_limit" in payload:
        out["per_user_limit"] = _as_int(
            payload.get("per_user_limit"), "每人限购", 0, 10_000)
    if "sort" in payload:
        out["sort"] = _as_int(payload.get("sort"), "排序", -10_000, 10_000)
    if "enabled" in payload:
        out["enabled"] = 1 if payload.get("enabled") else 0
    if out.get("kind") in BAG_KINDS and not partial:
        out.setdefault('duration_days', 0 if out['kind'] in ('invite_card','title_card','whitelist_card','custom') else 30)
        if out['kind'] == 'title_card' and out['cost'] < 200:
            raise ShopError('称号卡至少200积分')
        out.setdefault("retention_days", 7)
        try: validate_bag_spec(out)
        except ValueError as exc: raise ShopError(str(exc)) from None
    return out


def _decorate(row: dict[str, Any], *, include_private=False) -> dict[str, Any]:
    out = dict(row)
    kind = str(out.get("kind") or "")
    out["kind_label"] = KIND_LABELS.get(kind, kind)
    out["unit"] = KIND_UNITS.get(kind, "")
    out["enabled"] = bool(out.get("enabled"))
    if not include_private or kind != 'custom':
        out.pop("purchase_notice", None)
    return out


class ShopService:
    """Catalogue, orders, and the one method that spends points."""

    def __init__(self, db: Any, members: Any, points: Any, *, notice_config=None) -> None:
        self._db = db
        self._members = members
        self._points = points
        self._notice_config = notice_config or (dict)

    # -- catalogue -----------------------------------------------------------

    def seed_cards(self):
        with economy_write(self._db) as conn:
            if not conn.execute("SELECT 1 FROM meta WHERE key='inventory_catalogue_seeded'").fetchone():
                for item in DEFAULT_CARDS:
                    conn.execute('INSERT INTO shop_items(kind,name,cost,amount,duration_days,enabled,created_at) VALUES(?,?,?,?,?,0,?)',
                                 (item['kind'],item['name'],item['cost'],item['amount'],item['duration_days'],int(time.time())))
                conn.execute("INSERT INTO meta VALUES('inventory_catalogue_seeded','1')")
            if not conn.execute("SELECT 1 FROM meta WHERE key='whitelist_catalogue_seeded'").fetchone():
                if not conn.execute("SELECT 1 FROM shop_items WHERE kind='whitelist_card'").fetchone():
                    item = DEFAULT_WHITELIST_CARD
                    conn.execute('INSERT INTO shop_items(kind,name,cost,amount,duration_days,enabled,created_at) VALUES(?,?,?,?,?,1,?)',
                                 (item['kind'],item['name'],item['cost'],item['amount'],item['duration_days'],int(time.time())))
                conn.execute("INSERT INTO meta VALUES('whitelist_catalogue_seeded','1')")

    def items(self, enabled_only: bool = False, *, include_private=False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM shop_items"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY sort ASC, id ASC"
        return [_decorate(r, include_private=include_private) for r in self._db.query(sql)]

    def get(self, item_id: int, *, include_private=False) -> dict[str, Any] | None:
        row = self._db.one("SELECT * FROM shop_items WHERE id=?", (int(item_id),))
        return _decorate(row, include_private=include_private) if row else None

    def create(self, payload: Any, actor: str = "operator") -> dict[str, Any]:
        clean = validate_item(payload)
        now = int(time.time())
        with economy_write(self._db) as conn:
            cur = conn.execute(
                "INSERT INTO shop_items"
                "(kind,name,description,cost,amount,enabled,per_user_limit,"
                "sort,created_at,duration_days,purchase_notice,retention_days) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (clean["kind"], clean["name"], clean.get("description", ""),
                 clean["cost"], clean["amount"],
                 int(clean.get("enabled", 1)), int(clean.get("per_user_limit", 0)),
                 int(clean.get("sort", 0)), now, clean.get("duration_days", 30),
                 clean.get("purchase_notice", ""), clean.get("retention_days", 7)))
            item_id = int(cur.lastrowid or 0)
        self._audit(actor, "shop.item.create", str(item_id),
                    f"{clean['kind']} {clean['name']} cost={clean['cost']}")
        return self.get(item_id, include_private=True) or {}

    def update(self, item_id: int, payload: Any,
               actor: str = "operator") -> dict[str, Any]:
        item = self.get(item_id, include_private=True)
        if not item:
            raise KeyError(item_id)
        clean = validate_item(payload, partial=True)
        if not clean:
            return item
        if clean.get("kind", item["kind"]) in BAG_KINDS:
            updated = dict(item, **clean)
            if updated['kind'] == 'title_card' and updated['cost'] < 200:
                raise ShopError('称号卡至少200积分')
            try: validate_bag_spec(updated)
            except ValueError as exc: raise ShopError(str(exc)) from None
        sets = ", ".join(f"{k}=?" for k in clean)
        self._db.execute(
            f"UPDATE shop_items SET {sets},revision=revision+1 WHERE id=?",
            (*clean.values(), int(item_id)))
        self._audit(actor, "shop.item.update", str(item_id),
                    json.dumps({k: v for k, v in clean.items() if k != "purchase_notice"}, ensure_ascii=False)[:300])
        return self.get(item_id, include_private=True) or {}

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
            row["kind_label"] = KIND_LABELS.get(str(row.get("kind")), HISTORY_LABELS.get(str(row.get("kind")), row.get("kind")))
            row["unit"] = KIND_UNITS.get(str(row.get("kind")), HISTORY_UNITS.get(str(row.get("kind")), ""))
        return rows

    def redeemed_count(self, user_id: str, item_id: int) -> int:
        row = self._db.one(
            "SELECT COUNT(*) AS n FROM shop_orders "
            "WHERE emby_user_id=? AND item_id=?", (str(user_id), int(item_id)))
        return int((row or {}).get("n") or 0)

    # -- the one method that spends points -----------------------------------

    def redeem(self, user_id: str, item_id: int,
               actor: str = "member", *, request_id: str | None = None,
               expected_spec: dict[str, Any] | None = None) -> dict[str, Any]:
        """Charge points and deliver, or change nothing at all.

        The debit, the grant and the order row are one transaction. Ordering is
        deliberate: points are taken *first*, so an insufficient balance stops
        the reward before it is granted, and any later failure rolls the debit
        back with everything else.
        """
        # Hold the same DB lock for validation and fulfilment: concurrent
        # orders must see the preceding order's limits and personal overlay.
        with economy_write(self._db) as conn:
            key = request_id or uuid.uuid4().hex
            request = {"item_id": int(item_id),"expected_spec": expected_spec}
            prior = receipt(conn, "purchase", key, user_id, request)
            if prior is not None: return prior
            if expected_spec is not None and self.get(item_id) != expected_spec:
                raise ShopError('商品规格/价格已变化，请重新确认')
            result = self._redeem(conn, user_id, item_id, actor)
            save_receipt(conn, "purchase", key, user_id, request, result)
            return result

    def _redeem(self, conn: Any, user_id: str, item_id: int,
                actor: str) -> dict[str, Any]:
        user_id = str(user_id or "")
        item = self.get(item_id, include_private=True)
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

        if kind not in KINDS:
            raise ShopError("此类型已停用，不能购买")

        now = int(time.time())
        balance = self._points._apply(
            conn, user_id, -cost, "shop.redeem", f"item:{item_id}", actor, now)
        card_id = InventoryService.add(conn,user_id,item,"purchase",now)
        note = (f"购买后说明已存入背包（#{card_id}），保留{item['retention_days']}天"
                if kind == "custom" else f"道具已入背包（#{card_id}），使用后生效")
        order = conn.execute(
            "INSERT INTO shop_orders"
            "(emby_user_id,item_id,item_name,cost,kind,amount,created_at,spec_json) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (user_id, int(item_id), str(item["name"]), cost, kind, amount, now, encode(item)))
        conn.execute(
            "INSERT INTO audit_log(ts,actor,action,subject,detail,ok) VALUES(?,?,?,?,?,1)",
            (now, actor, 'shop.redeem', user_id, f'item={item_id} cost={cost} {note}'))
        from app.modules.shop_notices import record
        conn.execute('SAVEPOINT purchase_notice')
        try:
            record(conn, order.lastrowid, member, item, self._notice_config())
        except Exception:  # noqa: BLE001 - optional public delivery must not undo a purchase
            conn.execute('ROLLBACK TO purchase_notice')
            logging.getLogger(__name__).warning('Purchase announcement scheduling failed')
        finally:
            conn.execute('RELEASE purchase_notice')
        return {
            "ok": True,
            "item": _decorate(item),
            "card_id": card_id,
            "cost": cost,
            "balance": balance,
            "granted": note,
        }

    def _audit(self, actor: str, action: str, subject: str,
               detail: str) -> None:
        if self._members is None:
            return
        # An audit write that fails must not undo a grant that already
        # committed: the member has the goods either way, and raising here
        # would only make the caller think they do not.
        with contextlib.suppress(Exception):
            self._members.audit(actor, action, subject, detail)
