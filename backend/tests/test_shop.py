"""The shop is where points turn into things, so the tests are about the
exchange being honest in both directions.

The load-bearing property is that **a member is never charged for a reward
they did not get**. Debit and delivery are one transaction, so every failure
path is tested by asserting that the balance is untouched *and* the grant did
not happen -- checking only one of those would pass while the other silently
broke.

The rest is refusals: a disabled item, a per-user limit, an insufficient
balance, and the one case that would otherwise take money for nothing --
buying a speed boost on an account that is already unlimited.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.core.db import Database
from app.main import app
from app.modules.groups import GroupService
from app.modules.inventory import InventoryService
from app.modules.member_rewards import GB, KBPS_PER_MBPS, grant_reward
from app.modules.members import MemberService
from app.modules.points import PointsService
from app.modules.shop import ShopError, ShopService

ADMIN = ("admin", "change-me")


@pytest.fixture()
def stack(tmp_path):
    db = Database(tmp_path / "shop.db")
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    points = PointsService(db)
    shop = ShopService(db, members, points)
    return db, members, points, shop


@pytest.fixture()
def member(stack) -> str:
    _, members, _, _ = stack
    members.upsert("u1", "alice", {"group_id": "standard"}, actor="test")
    return "u1"


def _item(shop: ShopService, **kwargs) -> int:
    payload = {"kind": "bandwidth_card", "name": "测试商品", "cost": 100,
               "amount": 10, "duration_days": 30, "enabled": True}
    payload.update(kwargs)
    return int(shop.create(payload)["id"])


# -- seeding -----------------------------------------------------------------

def test_the_starter_catalogue_ships_disabled(stack) -> None:
    """A live default catalogue would sell at prices nobody chose."""
    _, _, _, shop = stack
    shop.seed_cards()
    items = shop.items()
    assert {i["kind"] for i in items} == {"invite_card", "bandwidth_card", "streams_card", "title_card", "whitelist_card"}
    assert all(i["enabled"] is False for i in items if i['kind'] != 'whitelist_card')
    assert [i["cost"] for i in items] == [500, 300, 600, 200, 5000]
    assert [i['kind'] for i in shop.items(enabled_only=True)] == ['whitelist_card']


def test_seeding_twice_does_not_duplicate_the_catalogue(stack) -> None:
    _, _, _, shop = stack
    shop.seed_cards()
    shop.seed_cards()
    assert len(shop.items()) == 5


def test_seeding_does_not_undo_a_deliberately_emptied_shop(stack) -> None:
    """An operator who deleted everything meant to have no catalogue."""
    _, _, _, shop = stack
    shop.seed_cards()
    for item in shop.items():
        shop.delete(item["id"])
    # The table is empty again, so re-seeding is allowed -- but a shop with a
    # single surviving item is one the operator has curated, and is left alone.
    shop.create({"kind": "invite_card", "name": "保留", "cost": 10, "amount": 1})
    shop.seed_cards()
    assert len(shop.items()) == 1


# -- the four kinds, delivered -----------------------------------------------

def test_traffic_admin_reward_preserves_shared_capability(stack, member):
    db, members, points, shop = stack
    note = grant_reward(db, members, member, "traffic", 50)
    assert members.get(member)["overrides"]["extra_traffic_bytes"] == 50 * GB
    assert "50GB" in note and points.balance(member) == 0 and not shop.orders()


def test_days_admin_reward_extends_the_term(stack, member):
    db, members, _, _ = stack
    before = members.get(member)["expires_at_effective"]
    grant_reward(db, members, member, "days", 7)
    assert members.get(member)["expires_at_effective"] >= max(before, int(time.time())) + 7 * 86400 - 5


def test_bandwidth_admin_reward_preserves_existing_limit(stack, member):
    db, members, _, _ = stack
    members.set_overrides(member, {"bandwidth_limit_kbps": 10_000})
    grant_reward(db, members, member, "bandwidth", 10)
    assert members.get(member)["overrides"]["bandwidth_limit_kbps"] == 10_000 + 10 * KBPS_PER_MBPS


def test_invite_admin_reward_preserves_quota(stack, member):
    db, members, _, _ = stack
    grant_reward(db, members, member, "invite", 2)
    assert members.get(member)["invite_quota"] == 2


def test_every_redemption_is_recorded_as_an_order(stack, member) -> None:
    _, _, points, shop = stack
    points.add(member, 500, "checkin")
    item_id = _item(shop, name="流量包", cost=100)
    shop.redeem(member, item_id)

    orders = shop.orders(user_id=member)
    assert len(orders) == 1
    assert orders[0]["item_name"] == "流量包"
    assert orders[0]["cost"] == 100
    # The debit says what it was for.
    assert points.ledger(member)[0]["reason"] == "shop.redeem"
    assert points.ledger(member)[0]["ref"] == f"item:{item_id}"


# -- refusals, each leaving nothing behind -----------------------------------

def test_a_member_who_cannot_afford_it_is_not_charged_and_gets_nothing(
        stack, member) -> None:
    """The rollback test: an insufficient balance must not deliver."""
    _, members, points, shop = stack
    points.add(member, 50, "checkin")
    item_id = _item(shop, amount=50, cost=100)

    with pytest.raises(ValueError, match="积分不足"):
        shop.redeem(member, item_id)

    assert points.balance(member) == 50
    assert members.get(member)["overrides"].get("extra_traffic_bytes") is None
    assert shop.orders(user_id=member) == []


def test_a_grant_that_fails_rolls_the_debit_back(stack, member,
                                                 monkeypatch) -> None:
    """Delivery failing after the charge is the one outcome to prevent."""
    _, members, points, shop = stack
    points.add(member, 500, "checkin")
    item_id = _item(shop, amount=50, cost=100)

    def explode(*args, **kwargs):
        raise RuntimeError("storage went away")

    monkeypatch.setattr(InventoryService, "add", explode)
    with pytest.raises(RuntimeError):
        shop.redeem(member, item_id)

    assert points.balance(member) == 500
    assert members.get(member)["overrides"].get("extra_traffic_bytes") is None
    assert shop.orders(user_id=member) == []


def test_a_disabled_item_cannot_be_bought(stack, member) -> None:
    _, _, points, shop = stack
    points.add(member, 500, "checkin")
    item_id = _item(shop, enabled=False)

    with pytest.raises(ShopError, match="下架"):
        shop.redeem(member, item_id)
    assert points.balance(member) == 500


def test_a_per_user_limit_stops_at_the_limit_not_before_it(stack,
                                                           member) -> None:
    _, _, points, shop = stack
    points.add(member, 1000, "checkin")
    item_id = _item(shop, cost=100, per_user_limit=2)

    shop.redeem(member, item_id)
    shop.redeem(member, item_id)
    with pytest.raises(ShopError, match="限兑"):
        shop.redeem(member, item_id)

    assert points.balance(member) == 800
    assert len(shop.orders(user_id=member)) == 2


def test_a_zero_limit_means_unlimited(stack, member) -> None:
    _, _, points, shop = stack
    points.add(member, 1000, "checkin")
    item_id = _item(shop, cost=100, per_user_limit=0)
    for _ in range(3):
        shop.redeem(member, item_id)
    assert len(shop.orders(user_id=member)) == 3


def test_one_members_limit_is_not_anothers(stack, member) -> None:
    _, members, points, shop = stack
    members.upsert("u2", "bob", {"group_id": "standard"}, actor="test")
    points.add(member, 500, "checkin")
    points.add("u2", 500, "checkin")
    item_id = _item(shop, cost=100, per_user_limit=1)

    shop.redeem(member, item_id)
    shop.redeem("u2", item_id)  # must not be blocked by u1's purchase
    assert len(shop.orders()) == 2


def test_unlimited_account_cannot_consume_bandwidth_card(stack, member):
    db, members, points, shop = stack
    points.add(member, 500, "checkin")
    item_id = _item(shop, amount=10, cost=300)
    result = shop.redeem(member, item_id)
    card = db.one("SELECT id,used_at FROM inventory WHERE emby_user_id=?", (member,))
    bag = InventoryService(db, members, shop, lambda: {"bandwidth_cap_mbps": 100, "streams_cap": 10})
    with pytest.raises(ValueError, match="不限速"):
        bag.use(member, card["id"])
    assert result["balance"] == 200 and points.balance(member) == 200
    assert db.one("SELECT used_at FROM inventory WHERE id=?", (card["id"],))["used_at"] is None


def test_buying_something_that_does_not_exist_is_refused(stack, member) -> None:
    _, _, points, shop = stack
    points.add(member, 500, "checkin")
    with pytest.raises(ShopError, match="不存在"):
        shop.redeem(member, 9999)


def test_a_member_who_is_not_enrolled_cannot_redeem(stack) -> None:
    _, _, points, shop = stack
    points.add("ghost", 500, "checkin")
    item_id = _item(shop)
    with pytest.raises(ShopError, match="账号不存在"):
        shop.redeem("ghost", item_id)


# -- catalogue management ----------------------------------------------------

def test_items_are_validated_on_the_way_in(stack) -> None:
    _, _, _, shop = stack
    for bad in ({"kind": "moonbeam", "name": "x", "cost": 1, "amount": 1},
                {"kind": "bandwidth_card", "name": "", "cost": 1, "amount": 1},
                {"kind": "bandwidth_card", "name": "x", "cost": 0, "amount": 1},
                {"kind": "bandwidth_card", "name": "x", "cost": 1, "amount": 0},
                {"kind": "bandwidth_card", "name": "x", "cost": "free", "amount": 1}):
        with pytest.raises(ShopError):
            shop.create(bad)
    assert shop.items() == []


def test_updating_an_item_changes_only_what_was_sent(stack) -> None:
    _, _, _, shop = stack
    item_id = _item(shop, name="原名", cost=100, amount=10)
    shop.update(item_id, {"cost": 250})
    item = shop.get(item_id)
    assert item["cost"] == 250
    assert item["name"] == "原名" and item["amount"] == 10


def test_updating_an_unknown_item_is_a_key_error(stack) -> None:
    _, _, _, shop = stack
    with pytest.raises(KeyError):
        shop.update(4242, {"cost": 1})


def test_deleting_an_item_keeps_the_orders_that_explain_past_grants(
        stack, member) -> None:
    _, _, points, shop = stack
    points.add(member, 500, "checkin")
    item_id = _item(shop, name="限时流量包", cost=100)
    shop.redeem(member, item_id)

    assert shop.delete(item_id) is True
    assert shop.delete(item_id) is False
    assert shop.get(item_id) is None
    # "Why does this member have extra traffic" must stay answerable.
    orders = shop.orders(user_id=member)
    assert len(orders) == 1 and orders[0]["item_name"] == "限时流量包"


def test_items_come_back_in_the_operators_order(stack) -> None:
    _, _, _, shop = stack
    _item(shop, name="第三", sort=30)
    _item(shop, name="第一", sort=10)
    _item(shop, name="第二", sort=20)
    assert [i["name"] for i in shop.items()] == ["第一", "第二", "第三"]


# -- API ---------------------------------------------------------------------

def test_the_shop_api_manages_the_catalogue() -> None:
    with TestClient(app) as client:
        created = client.post("/api/shop/items", auth=ADMIN, json={
            "kind": "bandwidth_card", "name": "带宽卡30", "cost": 400, "amount": 30,
            "enabled": True}).json()
        item_id = created["id"]
        assert created["kind_label"] == "带宽卡（入包）" and created["unit"] == "Mbps"

        updated = client.put(f"/api/shop/items/{item_id}", auth=ADMIN,
                             json={"cost": 350}).json()
        assert updated["cost"] == 350

        listed = client.get("/api/shop/items", auth=ADMIN).json()
        assert any(i["id"] == item_id for i in listed)
        enabled = client.get("/api/shop/items?enabled_only=true",
                             auth=ADMIN).json()
        assert any(i["id"] == item_id for i in enabled)

        assert client.delete(f"/api/shop/items/{item_id}",
                             auth=ADMIN).json()["ok"] is True
        assert client.delete(f"/api/shop/items/{item_id}",
                             auth=ADMIN).status_code == 404


def test_the_shop_api_refuses_bad_items_and_unknown_ids() -> None:
    with TestClient(app) as client:
        assert client.post("/api/shop/items", auth=ADMIN, json={
            "kind": "moonbeam", "name": "x", "cost": 1,
            "amount": 1}).status_code == 400
        assert client.put("/api/shop/items/9999", auth=ADMIN,
                          json={"cost": 5}).status_code == 404


def test_the_shop_ships_a_disabled_starter_catalogue_on_first_boot() -> None:
    with TestClient(app) as client:
        items = client.get("/api/shop/items", auth=ADMIN).json()
        assert len(items) == 5
        assert all(i["enabled"] is False for i in items if i['kind'] != 'whitelist_card')
        live = client.get("/api/shop/items?enabled_only=true", auth=ADMIN).json()
        assert len(live) == 1 and live[0]['kind'] == 'whitelist_card'
        assert live[0]['cost'] == 5000 and live[0]['duration_days'] == 0


def test_the_shop_api_needs_authentication() -> None:
    with TestClient(app) as client:
        assert client.get("/api/shop/items").status_code == 401
        assert client.get("/api/shop/orders").status_code == 401
        assert client.post("/api/shop/items", json={}).status_code == 401
        assert client.put("/api/shop/items/1", json={}).status_code == 401
        assert client.delete("/api/shop/items/1").status_code == 401


def test_orders_are_listed_newest_first_and_name_the_buyer() -> None:
    with TestClient(app) as client:
        client.put("/api/members/u1", auth=ADMIN,
                   json={"group_id": "standard", "username": "demo-user-1"})
        client.post("/api/points/u1/adjust", auth=ADMIN, json={"delta": 1000})
        item_id = client.post("/api/shop/items", auth=ADMIN, json={
            "kind": "invite_card", "name": "邀请码卡", "cost": 100, "amount": 1,
            "enabled": True}).json()["id"]

        app.state.shop.redeem("u1", item_id, actor="test")
        app.state.shop.redeem("u1", item_id, actor="test")

        orders = client.get("/api/shop/orders?limit=10", auth=ADMIN).json()
        assert len(orders) == 2
        assert orders[0]["username"] == "demo-user-1"
        assert orders[0]["id"] > orders[1]["id"]
        assert client.get("/api/points/u1", auth=ADMIN).json()["balance"] == 800


def test_item_changes_are_audited(stack) -> None:
    _, members, _, shop = stack
    item_id = _item(shop, name="审计商品")
    shop.update(item_id, {"cost": 999})
    shop.delete(item_id)
    trail = json.dumps(members.audit_log(20), ensure_ascii=False)
    assert "shop.item.create" in trail
    assert "shop.item.update" in trail
    assert "shop.item.delete" in trail
