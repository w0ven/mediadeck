"""Recipient-bound /kk qualifications reuse the admin registration channel."""
import sqlite3

import pytest
from test_registration import _svc

from app.core.db import Database
from app.core.errors import ConfigError
from app.modules.registration import RegistrationService


def test_gift_repeated_issue_reuses_link_and_snapshots_terms(tmp_path):
    reg, _members, groups, cfg = _svc(tmp_path)
    gift = reg.issue_gift("555", "operator")
    cfg["register_days"] = 90
    again = reg.issue_gift("555", "operator")
    assert again["gift_code"] == gift["gift_code"]
    admission = reg.resolve("555", gift["gift_code"])
    assert admission.allowed and admission.days == 30
    assert admission.group_id == groups.default_group_id()
    assert not reg.resolve("556", gift["gift_code"]).allowed
    assert reg.consume(admission, "created")
    assert not reg.consume(admission, "duplicate")
    assert not reg.resolve("555", gift["gift_code"]).allowed


def test_revoked_or_regranted_link_cannot_recover_old_qualification(tmp_path):
    reg, _, _, _ = _svc(tmp_path)
    old = reg.issue_gift("555", "operator")
    admission = reg.resolve("555", old["gift_code"])
    reg.revoke_grant("555")
    new = reg.issue_gift("555", "operator")
    assert new["gift_code"] != old["gift_code"]
    assert not reg.resolve("555", old["gift_code"]).allowed
    assert not reg.consume(admission, "stale")
    assert reg.resolve("555", new["gift_code"]).allowed


def test_gift_obeys_admin_channel_and_rejects_existing_account(tmp_path):
    reg, members, _, cfg = _svc(tmp_path)
    members.upsert("u1", "alice", {"group_id": "standard"}, actor="test")
    members.bind_telegram("u1", "555", "alice", actor="test")
    with pytest.raises(ConfigError, match="已有账号"):
        reg.issue_gift("555", "operator")
    gift = reg.issue_gift("556", "operator")
    cfg["allow_admin_grant"] = False
    assert not reg.resolve("556", gift["gift_code"]).allowed
    with pytest.raises(ConfigError, match="通道已关闭"):
        reg.issue_gift("557", "operator")


def test_old_admin_grants_upgrade_without_changing_entitlements(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE admin_grants(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "tg_user_id TEXT NOT NULL UNIQUE, granted_by TEXT NOT NULL DEFAULT '', "
                 "created_at INTEGER NOT NULL, used_at INTEGER)")
    conn.execute("INSERT INTO admin_grants(tg_user_id,created_at) VALUES('555',123)")
    conn.commit()
    conn.close()
    db = Database(path)
    row = db.one("SELECT * FROM admin_grants WHERE tg_user_id='555'")
    assert row["created_at"] == 123 and row["used_at"] is None
    assert row["gift_code"] is None and row["gift_days"] is None
    reg = RegistrationService(db, config_provider=lambda: {"register_days": 45})
    admission = reg.resolve("555")
    assert admission.allowed and admission.days == 45


def test_rearming_plain_grant_does_not_reactivate_spent_gift_link(tmp_path):
    reg, _, _, cfg = _svc(tmp_path)
    gift = reg.issue_gift("555", "operator")
    assert reg.consume(reg.resolve("555", gift["gift_code"]), "u1")
    cfg["register_days"] = 60
    reg.grant_admin("555", "operator")
    assert not reg.resolve("555", gift["gift_code"]).allowed
    assert reg.resolve("555").days == 60
