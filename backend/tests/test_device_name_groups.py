"""Name grouping is observational; original login identities remain intact."""
from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.core.db import Database
from app.main import app
from app.modules.device_groups import count_device_groups, group_device_records
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.stats import StatsService
from app.modules.telegram import TelegramBot

AUTH = ("admin", "change-me")


def seed_33(members: MemberService, user_id: str = "viewer-a") -> None:
    """Synthetic 1 Windows + 9/23 same-name phone IDs across two players."""
    members.upsert(user_id, "DemoViewer", {"group_id": "standard"})
    members.register_device(user_id, "desktop-id", device_name="WIN-DEMO",
                            client="RodelPlayer", now=100)
    for index in range(32):
        members.register_device(user_id, str(UUID(int=index + 1, version=4)),
                                device_name="iPhone",
                                client="SenPlayer" if index < 9 else "EplayerX",
                                app_version="1.0", now=101 + index)


@pytest.fixture()
def members(tmp_path):
    db = Database(tmp_path / "devices.db")
    groups = GroupService(db)
    groups.seed_defaults()
    return MemberService(db, groups)


def test_33_to_2_across_players_without_changing_raw_records(members):
    seed_33(members)
    db = members._db
    before = db.query("SELECT * FROM devices ORDER BY device_id")
    assert len(before) == 33
    assert len({row["device_id"] for row in before}) == 33
    assert all(UUID(row["device_id"]).version == 4
               for row in before if row["device_name"] == "iPhone")
    assert members.get("viewer-a")["device_count"] == 2
    assert members.list()[0]["device_count"] == 2
    detail = members.detail("viewer-a")
    assert sorted(g["record_count"] for g in detail["device_groups"]) == [1, 32]
    phone = next(g for g in detail["device_groups"] if g["device_name"] == "iPhone")
    assert {row["client"] for row in phone["devices"]} == {"SenPlayer", "EplayerX"}
    assert detail["devices"] == members.devices("viewer-a")
    assert sorted((row for g in detail["device_groups"] for row in g["devices"]),
                  key=lambda r: r["device_id"]) == before
    assert StatsService(db).overview()["devices"] == 2
    assert len(StatsService(db).member_detail("viewer-a")["devices"]) == 33
    assert db.query("SELECT * FROM devices ORDER BY device_id") == before


def test_cross_account_same_name_and_id_never_merge(members):
    seed_33(members)
    seed_33(members, "viewer-b")
    assert members.get("viewer-a")["device_count"] == 2
    assert members.get("viewer-b")["device_count"] == 2
    assert count_device_groups(members._db) == 4
    assert StatsService(members._db).overview()["devices"] == 4
    groups = group_device_records(members._db.query("SELECT * FROM devices"))
    assert len(groups) == 4
    assert len(members.devices("viewer-a")) == len(members.devices("viewer-b")) == 33


@pytest.mark.parametrize("name", ["", " \t\r\n\v\f", "Unknown", "unknown",
                                 "Unknown device", " UNKNOWN DEVICE \t", "未知", "未知设备",
                                 "\u00a0\u3000\u2003", "\u3000Unknown device\u00a0"])
def test_empty_and_unknown_labels_count_each_id(members, name):
    members.upsert("viewer-a", "DemoViewer", {"group_id": "standard"})
    for device_id in ("id-a", "id-b"):
        members.register_device("viewer-a", device_id, device_name=name)
    assert members.get("viewer-a")["device_count"] == 2
    groups = members.detail("viewer-a")["device_groups"]
    assert len(groups) == 2
    assert all(g["grouping"] == "device_id" and g["record_count"] == 1 for g in groups)
    members.set_device_blocked("viewer-a", "id-a", True)
    assert count_device_groups(members._db) == 1
    assert len(members.devices("viewer-a")) == 2


def test_exact_names_trim_whitespace_without_fuzzy_matching(members):
    members.upsert("viewer-a", "DemoViewer", {"group_id": "standard"})
    for index, name in enumerate(["iPhone", " \tiPhone\r\n\v\f", "iphone", "iPhone 2",
                                  "iPhone\u00a0", "Viewer's TV", "Viewer's TV", "iPhone-X"]):
        members.register_device("viewer-a", f"id-{index}", device_name=name)
    assert members.get("viewer-a")["device_count"] == 5
    assert len(members.detail("viewer-a")["device_groups"]) == 5
    # A real name equal to an unknown record's ID is not the same group.
    members.register_device("viewer-a", "iPhone", device_name="Unknown")
    assert members.get("viewer-a")["device_count"] == 6
    assert len(members.detail("viewer-a")["device_groups"]) == 6


def test_mixed_blocked_ids_count_group_until_last_active_id_is_blocked(members):
    seed_33(members)
    phones = [row for row in members.devices("viewer-a") if row["device_name"] == "iPhone"]
    for row in phones[:-1]:
        members.set_device_blocked("viewer-a", row["device_id"], True)
    assert members.get("viewer-a")["device_count"] == 2
    phone = next(g for g in members.detail("viewer-a")["device_groups"]
                 if g["device_name"] == "iPhone")
    assert phone["record_count"] == 32 and phone["unblocked_count"] == 1
    members.set_device_blocked("viewer-a", phones[-1]["device_id"], True)
    assert members.get("viewer-a")["device_count"] == 1
    assert StatsService(members._db).overview()["devices"] == 1
    assert not members.register_device("viewer-a", phones[0]["device_id"],
                                       device_name="iPhone", now=500)
    assert members.device_blocked("viewer-a", phones[0]["device_id"])
    members.set_device_blocked("viewer-a", "desktop-id", True)
    assert members.get("viewer-a")["device_count"] == 0
    assert count_device_groups(members._db) == 0
    assert len(members.detail("viewer-a")["device_groups"]) == 2
    members.set_device_blocked("viewer-a", phones[0]["device_id"], False)
    assert members.get("viewer-a")["device_count"] == 1
    assert len(members.devices("viewer-a")) == 33


def test_member_list_detail_bot_and_global_api_share_count_with_raw_api_unchanged():
    with TestClient(app) as client:
        seed_33(app.state.members)
        before = app.state.members.devices("viewer-a")
        listing = client.get("/api/members", auth=AUTH).json()
        member = next(m for m in listing["members"] if m["emby_user_id"] == "viewer-a")
        detail = client.get("/api/members/viewer-a", auth=AUTH).json()
        assert member["device_count"] == detail["member"]["device_count"] == 2
        assert detail["devices"] == before
        assert len(detail["device_groups"]) == 2
        assert client.get("/api/members/viewer-a/devices", auth=AUTH).json() == before
        assert client.get("/api/stats/overview", auth=AUTH).json()["devices"] == 2
        bot = TelegramBot(dict, app.state.members, stats=app.state.stats)
        for text in (bot._usage_text(member), bot._account_card(member),
                     bot._account_card(member, public=True),
                     bot._account_card(member, managing=True), bot._admin_details(member)):
            assert "设备分组：2 组" in text
            assert "按设备名称归并" in text and "非硬件唯一识别" in text
            assert "空/未知按ID单计" in text
            assert "已登记设备" not in text
        target = next(row for row in before if row["device_name"] == "iPhone")
        response = client.post(f'/api/members/viewer-a/devices/{target["device_id"]}/block',
                               auth=AUTH)
        assert response.status_code == 200 and response.json()["blocked"] is True
        after = client.get("/api/members/viewer-a/devices", auth=AUTH).json()
        assert len(after) == 33
        assert sum(row["blocked"] for row in after) == 1
        assert next(row for row in after if row["device_id"] == target["device_id"])["blocked"] == 1
        for row in after:
            original = next(r for r in before if r["device_id"] == row["device_id"])
            assert {k: v for k, v in row.items() if k != "blocked"} == {
                k: v for k, v in original.items() if k != "blocked"}
        assert app.state.members.get("viewer-a")["device_count"] == 2
        assert bot._usage_text(app.state.members.get("viewer-a")).startswith("📊")
        assert client.get("/api/stats/overview", auth=AUTH).json()["devices"] == 2
        response = client.post(f'/api/members/viewer-a/devices/{target["device_id"]}/unblock',
                               auth=AUTH)
        assert response.status_code == 200
        assert client.get("/api/members/viewer-a/devices", auth=AUTH).json() == before


def test_bot_raw_identity_view_labels_count_as_records_not_hardware(members):
    seed_33(members)
    member = members.get("viewer-a")
    members.bind_telegram("viewer-a", "1001", "demo")
    bot = TelegramBot(lambda: {"enabled": True}, members)
    captured = []

    async def edit(chat, mid, text, keyboard=None):
        captured.append(text)
        return True

    bot._edit = edit
    asyncio.run(bot._handle_callback({
        "id": "synthetic-callback", "data": "devices", "from": {"id": 1001},
        "message": {"message_id": 1, "chat": {"id": 1001, "type": "private"}},
    }))
    assert captured
    assert "设备分组：2 组" in captured[-1]
    assert "原始登录标识记录：33 条" in captured[-1]
    assert "不是实体设备台数" in captured[-1]
    assert member["max_streams"] == members.get("viewer-a")["max_streams"]
