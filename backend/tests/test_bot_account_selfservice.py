"""Actual Bot handlers + LiveEmby with local-only HTTP/Telegram transports."""
import json
import time

import httpx
import pytest
from test_bot_password_choices import begin_reset, choose, tap, text
from test_bot_password_choices import bot as password_bot  # noqa: F401
from test_request_bot import bot as request_bot  # noqa: F401
from test_request_bot import message, run

from app.adapters.live import LiveEmby
from app.adapters.mock import MockEmby
from app.modules.account_deletion import deletion_in_progress
from app.modules.play_money import CashBook
from app.modules.play_schema import migrate
from app.modules.points import PointsService


@pytest.fixture
def bot(request):
    return request.getfixturevalue("password_bot")


@pytest.fixture
def remote(bot, monkeypatch):
    state = {"calls": [], "policy": {"IsAdministrator": False, "IsDisabled": False,
             "InvalidLoginAttemptCount": 10, "LockedOutDate": "2026-10-11T00:00:00Z",
             "EnableAllFolders": False, "EnabledFolders": ["keep-me"]},
             "get_status": 200, "delete_status": 204, "password_status": 204,
             "policy_status": 204, "on_get": None}

    def upstream(request):
        state["calls"].append((request.method, request.url.path,
                               json.loads(request.content) if request.content else None))
        if request.method == "GET":
            if state["on_get"]:
                state["on_get"]()
            return httpx.Response(state["get_status"], json={"Id": "u1", "Policy": state["policy"]})
        if request.url.path.endswith("/Delete"):
            return httpx.Response(state["delete_status"])
        if request.url.path.endswith("/Password"):
            return httpx.Response(state["password_status"])
        if request.url.path.endswith("/Policy"):
            return httpx.Response(state["policy_status"])
        raise AssertionError(request.url)

    adapter = LiveEmby(lambda: {"enabled": True, "url": "https://emby.invalid",
                               "api_key": "synthetic-test-key"})
    monkeypatch.setattr(adapter, "_client", lambda *_: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)))
    bot._emby = adapter
    return state


def test_mock_adapter_self_delete_guards():
    adapter = MockEmby()
    assert not run(adapter.delete_selfservice_user("admin", authorize=lambda: True))
    assert not run(adapter.delete_selfservice_user("u1", authorize=lambda: False))
    assert run(adapter.delete_selfservice_user("u1", authorize=lambda: True))
    assert not run(adapter.delete_selfservice_user("u1", authorize=lambda: True))
    assert {u["Id"] for u in run(adapter.list_users())} == {"u2", "admin"}


def test_account_button_navigation(bot, remote):
    message(bot, "/start")
    tap(bot, "me")
    card = bot.transport.messages[("900", bot._panel["900"])]
    actions = {b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row}
    assert {"resetpw", "delete_account"} <= actions
    tap(bot, "resetpw")
    assert bot._pending["900"][0] == "password_choice"
    choose(bot, "cancel")
    tap(bot, "me")
    tap(bot, "delete_account")
    assert bot._pending["900"][0] == "account_delete" and not remote["calls"]


def begin_delete(bot):
    message(bot, "/me")
    card = bot.transport.messages[("900", bot._panel["900"])]
    assert "修改密码" in str(card) and "删除账号" in str(card)
    tap(bot, "delete_account")
    p = bot._pending["900"][2]
    assert "不可恢复" in text(bot) and "不是解绑" in text(bot)
    return f"delself:{p['nonce']}:confirm", p["message_id"]


def deleted_calls(remote):
    return [c for c in remote["calls"] if c[1].endswith("/Delete")]


def test_delete_real_remote_and_local_once_keeps_inviter_history_and_settlement(bot, remote):
    points = PointsService(bot.db)
    points.add("u1", 80, "test")
    migrate(bot.db)
    cash = CashBook(points)
    with bot.db.write() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cash.reserve(conn, "poker", "synthetic-round", "u1", 20)
    bot.db.execute("INSERT INTO shop_orders(emby_user_id,item_id,item_name,cost,created_at) VALUES(?,?,?,?,?)",
                   ("u1", 9, "historical", 5, 1))
    bot.db.execute("UPDATE members SET inviter_id='u2' WHERE emby_user_id='u1'")
    bot.members.register_device("u1", "device-one")
    callback, mid = begin_delete(bot)
    assert not remote["calls"]
    protected = []
    remote["on_get"] = lambda: protected.append(deletion_in_progress(bot.db._conn, "u1"))
    tap(bot, callback, mid=mid)
    assert protected == [True]
    assert not deletion_in_progress(bot.db._conn, "u1")
    tap(bot, callback, mid=mid)
    assert len(deleted_calls(remote)) == 1
    assert not bot.members.get("u1") and bot.members.get("u2")
    assert not bot.db.query("SELECT * FROM devices WHERE emby_user_id='u1'")
    assert bot.db.query("SELECT * FROM shop_orders WHERE emby_user_id='u1'")
    assert points.balance("u1") == 60
    with bot.db.write() as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert cash.release(conn, "poker", "synthetic-round", "u1") == 20
    assert points.balance("u1") == 80  # old-account settlement still works; no ledger destruction
    assert "已永久删除" in text(bot)
    assert all("Items" not in c[1] for c in remote["calls"])


@pytest.mark.parametrize("interruption", ["cancel", "command_cancel", "expired", "replay_after_cancel", "home",
    "wrong_actor", "wrong_card", "wrong_nonce", "group", "rebound", "unbind_rebind",
    "promoted", "restart"])
def test_delete_confirmation_safety(bot, remote, interruption):
    callback, mid = begin_delete(bot)
    chat, actor = "900", "900"
    if interruption in ("cancel", "replay_after_cancel"):
        tap(bot, callback.replace(":confirm", ":cancel"), mid=mid)
    elif interruption == "command_cancel":
        message(bot, "/cancel")
    elif interruption == "expired":
        w = bot._pending["900"]
        bot._pending["900"] = (w[0], time.time() - 1, w[2])
    elif interruption == "home":
        tap(bot, "home")
    elif interruption == "wrong_actor":
        actor = "901"
    elif interruption == "wrong_card":
        mid += 1000
    elif interruption == "wrong_nonce":
        callback = "delself:not-the-token:confirm"
    elif interruption == "group":
        chat = "-10055"
    elif interruption == "rebound":
        bot.members.bind_telegram("u2", "900")
    elif interruption == "unbind_rebind":
        bot.members.unbind_telegram("u1")
        bot.members.bind_telegram("u1", "900")
    elif interruption == "promoted":
        bot.members.set_roles("u1", ["admin"])
    elif interruption == "restart":
        bot._pending.clear()  # confirmation is memory-only
    tap(bot, callback, chat=chat, mid=mid, actor=actor, group=interruption == "group")
    assert not deleted_calls(remote) and bot.members.get("u1")


@pytest.mark.parametrize("case", ["group", "foreign", "unbound", "admin", "disconnected"])
def test_delete_entry_requires_private_owner_nonadmin(bot, remote, case):
    chat, actor = "900", "900"
    if case == "group":
        chat = "-10055"
    elif case == "foreign":
        actor = "901"
    elif case == "unbound":
        bot.members.unbind_telegram("u1")
    elif case == "admin":
        bot.members.set_roles("u1", ["admin"])
    else:
        bot._emby = None
    tap(bot, "delete_account", chat=chat, actor=actor, group=case == "group")
    assert not bot._pending.get("900") and not remote["calls"]


@pytest.mark.parametrize("case", ["remote_admin", "unknown_admin", "get_404", "get_503",
    "delete_503", "delete_false", "rebind_during_get", "admin_during_get", "expired_during_get", "local_failure"])
def test_delete_backend_guards_and_partial_failure(bot, remote, monkeypatch, case):
    callback, mid = begin_delete(bot)
    if case == "remote_admin":
        remote["policy"]["IsAdministrator"] = True
    elif case == "unknown_admin":
        remote["policy"].pop("IsAdministrator")
    elif case.startswith("get_"):
        remote["get_status"] = int(case.split("_")[1])
    elif case == "delete_503":
        remote["delete_status"] = 503
    elif case == "delete_false":
        remote["delete_status"] = 400
    elif case == "rebind_during_get":
        remote["on_get"] = lambda: bot.members.bind_telegram("u2", "900")
    elif case == "admin_during_get":
        remote["on_get"] = lambda: bot.members.set_roles("u1", ["admin"])
    elif case == "expired_during_get":
        later = time.time() + 121
        remote["on_get"] = lambda: monkeypatch.setattr(time, "time", lambda: later)
    else:
        def fail(*a, **kw):
            raise RuntimeError("synthetic local write failure")
        monkeypatch.setattr(bot.members, "delete", fail)
    tap(bot, callback, mid=mid)
    assert bot.members.get("u1") and "已永久删除" not in text(bot)
    assert not deletion_in_progress(bot.db._conn, "u1")
    assert "未" in text(bot)
    assert len(deleted_calls(remote)) == (1 if case in ("delete_503", "delete_false", "local_failure") else 0)
    before = len(remote["calls"])
    tap(bot, callback, mid=mid)
    assert len(remote["calls"]) == before


@pytest.mark.parametrize("password_status,policy_status", [(204, 204), (400, 204), (503, 204), (204, 503), (204, 400)])
def test_password_unlock_merge_failure_and_input_cleanup(bot, remote, password_status, policy_status):
    remote.update(password_status=password_status, policy_status=policy_status)
    begin_reset(bot)
    choose(bot, "custom")
    secret = " Synthetic <password> 123 "
    message(bot, secret)
    assert ("deleteMessage", {"chat_id": "900", "message_id": 999}) in bot.transport.calls
    choose(bot, "confirm")
    calls = remote["calls"]
    assert calls[0] == ("POST", "/emby/Users/u1/Password",
                         {"Id": "u1", "NewPw": secret, "ResetPassword": False})
    if password_status == 204:
        assert calls[1][:2] == ("GET", "/emby/Users/u1")
        patch = calls[2][2]
        assert patch == dict(remote["policy"], InvalidLoginAttemptCount=0, LockedOutDate=None)
        assert bot.cache_changes == [True]
        assert ("解除登录锁定未确认" in text(bot)) == (policy_status != 204)
    else:
        assert len(calls) == 1 and not bot.cache_changes
        assert "失败" in text(bot)
    assert secret not in str(bot.db.query("SELECT * FROM audit_log"))
    assert secret not in str(bot.db.query("SELECT * FROM meta"))
    assert not bot._pending.get("900")


def test_password_invalid_input_cleanup_is_best_effort(bot, remote):
    begin_reset(bot)
    choose(bot, "custom")
    bot.transport.fail.add("deleteMessage")
    message(bot, "abc")
    assert "至少 6 位" in text(bot)
    message(bot, "A-valid-password")
    choose(bot, "confirm")
    assert remote["calls"][0][2]["NewPw"] == "A-valid-password"


def test_leaving_password_during_input_cleanup_does_not_restore_secret(bot, remote):
    begin_reset(bot)
    choose(bot, "custom")
    call = bot._call

    async def cancel_on_delete(method, payload=None, **kw):
        if method == "deleteMessage":
            bot._pending.pop("900", None)
        return await call(method, payload, **kw)

    bot._call = cancel_on_delete
    message(bot, "NeverRestoreSecret")
    assert not bot._pending.get("900") and not remote["calls"]
