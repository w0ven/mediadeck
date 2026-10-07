"""Retire purchasing code, not money, historical records or shared rewards."""
import json

import pytest

from app.core.db import Database
from app.modules.economy_rules import DEFAULT_CARDS, encode
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.points import PointsService
from app.modules.shop import ShopError, ShopService

LEGACY = ("traffic", "days", "bandwidth", "invite")


def test_upgrade_deletes_only_legacy_catalogue_preserves_orders_ledger_and_rights(tmp_path):
    path = tmp_path / "upgrade.db"
    db = Database(path)
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    members.upsert("owner", "history", {"group_id": "standard"})
    members.set_overrides("owner", {"extra_traffic_bytes": 555, "bandwidth_limit_kbps": 32100})
    db.execute("UPDATE members SET invite_quota=7 WHERE emby_user_id='owner'")
    points = PointsService(db)
    points.add("owner", 999, "historic.adjust")
    shop = ShopService(db, members, points)
    fresh = shop.create(dict(DEFAULT_CARDS[0], cost=1234, enabled=True))
    db.execute("DELETE FROM meta WHERE key='legacy_shop_retired'")
    for index, kind in enumerate(LEGACY, 100):
        db.execute(
            "INSERT INTO shop_items(id,kind,name,cost,amount,created_at) VALUES(?,?,?,?,?,?)",
            (index, kind, "原商品", 50, 8, 1),
        )
        db.execute(
            "INSERT INTO shop_orders(emby_user_id,item_id,item_name,cost,kind,amount,created_at,spec_json) "
            "VALUES(?,?,?,?,?,?,?,?)", ("owner", index, "买入时名称", 40, kind, 7, 2,
                                      encode({"old": "unaltered"}) if kind == "traffic" else "{}"),
        )
    before_member = db.one("SELECT * FROM members WHERE emby_user_id='owner'")
    before_orders = shop.orders()
    before_ledger = points.ledger("owner")
    db.close()
    db = Database(path)
    shop = ShopService(db, MemberService(db, GroupService(db)), PointsService(db))
    assert shop.items() == [fresh]
    assert db.one("SELECT * FROM members WHERE emby_user_id='owner'") == before_member
    assert shop._points.ledger("owner") == before_ledger
    after = shop.orders()
    assert len(after) == 4
    for old, row in zip(before_orders, after, strict=True):
        assert {k: v for k, v in row.items() if k != "spec_json"} == {
            k: v for k, v in old.items() if k != "spec_json"
        }
        if row["kind"] == "traffic":
            assert row["spec_json"] == old["spec_json"]
        else:
            assert json.loads(row["spec_json"]) == {
                "kind": row["kind"], "name": "买入时名称", "cost": 40, "amount": 7, "historical": True,
            }
    db._migrate()
    assert shop.orders() == after and shop.items() == [fresh]
    assert db.one("PRAGMA integrity_check")["integrity_check"] == "ok"
    assert db.query("PRAGMA foreign_key_check") == []
    db.close()


@pytest.mark.parametrize("kind", LEGACY)
def test_legacy_create_update_and_purchase_refused_even_for_manually_injected_rows(tmp_path, kind):
    db = Database(tmp_path / "refuse.db")
    shop = ShopService(db, None, PointsService(db))
    with pytest.raises(ShopError, match="类型"):
        shop.create({"kind": kind, "name": "不可再售", "cost": 1, "amount": 1})
    item = shop.create(dict(DEFAULT_CARDS[0]))
    with pytest.raises(ShopError, match="类型"):
        shop.update(item["id"], {"kind": kind})
    groups = GroupService(db)
    groups.seed_defaults()
    shop._members = MemberService(db, groups)
    shop._members.upsert("u", "u", {"group_id": "standard"})
    shop._points.add("u", 100, "test")
    db.execute("UPDATE shop_items SET kind=? WHERE id=?", (kind, item["id"]))
    with pytest.raises(ShopError, match="停用"):
        shop.redeem("u", item["id"])
    assert shop._points.balance("u") == 100 and not shop.orders()
    assert not hasattr(shop, "_grant") and not hasattr(shop, "grant")
    db.close()
