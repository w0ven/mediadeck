"""Actual update dispatch and SQLite business path; no live Telegram writes."""
import asyncio
import copy
import time
from unittest.mock import AsyncMock, Mock

import pytest
from test_economy import proof
from test_economy_bot import env as economy_env  # noqa: F401
from test_group_points import confirm, dispatch, msg
from test_tg_interaction_context import GROUP, SECOND, VIEWER, click, command
from test_tg_interaction_context import env as interaction_env  # noqa: F401


@pytest.fixture
def env(request):
    return request.getfixturevalue("economy_env")


def message(text="签到", chat=VIEWER, user=VIEWER, mid=301, thread=None):
    row = {"chat": {"id": chat, "type": "supergroup" if chat < 0 else "private"},
           "from": {"id": user, "is_bot": False, "first_name": "<Viewer>"},
           "message_id": mid, "text": text}
    if thread:
        row.update(message_thread_id=thread, is_topic_message=True)
    return row


def posted(env):
    return [p for method, p in env.tg.calls if method == "sendMessage"]


def no_edits(env):
    assert not any(method.startswith("editMessage") for method, _ in env.tg.calls)


@pytest.mark.parametrize("text", ["签到", "/签到", "/checkin", "/签到@MediaDeckDemoBot",
                                 "/checkin@mediadeckdemobot"])
@pytest.mark.parametrize("chat,thread", [(VIEWER, None), (GROUP, None), (GROUP, 19)])
def test_text_dispatch_new_reply_with_progress_then_once_only_reward(env, text, chat, thread):
    async def run():
        old = await command(env, "/me", chat=chat, user=VIEWER, thread=thread)
        old_body = copy.deepcopy(env.tg.message(chat, old))
        env.tg.calls.clear()
        row = message(text, chat=chat, thread=thread)
        assert "entities" not in row
        await env.bot._dispatch_update({"message": row})
        payload = posted(env)[-1]
        assert "/600" in payload["text"] and payload["chat_id"] == chat
        assert payload["reply_parameters"] == {"message_id": row["message_id"]}
        assert payload.get("message_thread_id") == thread
        if chat == GROUP:
            assert f"tg://user?id={VIEWER}" in payload["text"]
            assert "&lt;Viewer&gt;" in payload["text"] and "ViewerB" not in payload["text"]
        no_edits(env)
        assert env.tg.message(chat, old) == old_body
        assert env.db.one("SELECT COUNT(*) n FROM checkins")["n"] == 0
        proof(env.db, "u1", now=time.time())
        row["message_id"] += 1
        await env.bot._dispatch_update({"message": row})
        assert "签到成功" in posted(env)[-1]["text"]
        balance = env.services.points.balance("u1")
        ledger = copy.deepcopy(env.services.points.ledger("u1"))
        await env.bot._dispatch_update({"message": row})  # duplicate update
        row["message_id"] += 1
        await env.bot._dispatch_update({"message": row})  # same day, new command
        assert "已签到" in posted(env)[-1]["text"]
        assert env.db.one("SELECT COUNT(*) n FROM checkins")["n"] == 1
        assert env.services.points.balance("u1") == balance
        assert env.services.points.ledger("u1") == ledger
        no_edits(env)
        assert env.tg.message(chat, old) == old_body
    asyncio.run(run())


@pytest.mark.parametrize("chat,thread", [(VIEWER, None), (GROUP, 29)])
def test_callback_ack_new_message_and_repeated_click_preserve_menu(env, chat, thread):
    async def run():
        old = await command(env, "/me", chat=chat, user=VIEWER, thread=thread)
        old_body = copy.deepcopy(env.tg.message(chat, old))
        env.tg.calls.clear()
        await click(env, "checkin", old, chat=chat, user=VIEWER, thread=thread)
        assert "/600" in posted(env)[-1]["text"]
        proof(env.db, "u1", now=time.time())
        await click(env, "checkin", old, chat=chat, user=VIEWER, thread=thread)
        assert any(m == "answerCallbackQuery" for m, _ in env.tg.calls)
        payload = posted(env)[-1]
        assert "签到成功" in payload["text"]
        assert payload["reply_parameters"] == {"message_id": old}
        assert payload.get("message_thread_id") == thread
        balance = env.services.points.balance("u1")
        await click(env, "checkin", old, chat=chat, user=VIEWER, thread=thread)
        assert "已签到" in posted(env)[-1]["text"]
        assert env.services.points.balance("u1") == balance
        assert env.db.one("SELECT COUNT(*) n FROM checkins")["n"] == 1
        no_edits(env)
        assert env.tg.message(chat, old) == old_body
    asyncio.run(run())


@pytest.mark.parametrize("text", ["我想签到", "签到啦", "签到 100", "/checkin 100",
                                 "/签到 extra", "/checkin@OtherBot", "/签到@OtherBot"])
def test_non_entire_commands_or_other_bot_never_enter_checkin(env, monkeypatch, text):
    check = Mock()
    monkeypatch.setattr(env.services.checkin, "checkin", check)
    asyncio.run(env.bot._dispatch_update({"message": message(text, chat=GROUP)}))
    check.assert_not_called()
    assert env.db.one("SELECT COUNT(*) n FROM checkins")["n"] == 0


@pytest.mark.parametrize("mutation", ["bot", "missing_bot_flag", "sender_chat", "auto_forward",
                                     "missing_id", "string_id", "bool_id", "channel", "bad_mid"])
def test_unreliable_message_identity_is_ignored(env, monkeypatch, mutation):
    row = message(chat=GROUP)
    if mutation == "bot": row["from"]["is_bot"] = True
    elif mutation == "missing_bot_flag": row["from"].pop("is_bot")
    elif mutation == "sender_chat": row["sender_chat"] = {"id": GROUP}
    elif mutation == "auto_forward": row["is_automatic_forward"] = True
    elif mutation == "missing_id": row["from"].pop("id")
    elif mutation == "string_id": row["from"]["id"] = str(VIEWER)
    elif mutation == "bool_id": row["from"]["id"] = True
    elif mutation == "channel": row["chat"]["type"] = "channel"
    else: row["message_id"] = "301"
    check = Mock()
    monkeypatch.setattr(env.services.checkin, "checkin", check)
    asyncio.run(env.bot._dispatch_update({"message": row}))
    check.assert_not_called()
    assert not posted(env)


@pytest.mark.parametrize("callback", [False, True])
def test_unbound_user_gets_new_binding_feedback_no_business(env, callback):
    row = message(user=999, chat=999)
    if callback:
        update = {"callback_query": {"id": "cb", "data": "checkin", "from": row["from"], "message": row}}
    else:
        update = {"message": row}
    asyncio.run(env.bot._dispatch_update(update))
    assert "绑定" in posted(env)[-1]["text"]
    assert "余额" not in posted(env)[-1]["text"]
    no_edits(env)
    assert env.db.one("SELECT COUNT(*) n FROM checkins")["n"] == 0


@pytest.mark.parametrize("callback", [False, True])
def test_non_allowed_group_does_not_checkin(env, callback):
    row = message(chat=-99999)
    if callback:
        update = {"callback_query": {"id": "cb", "data": "checkin", "from": row["from"], "message": row}}
    else: update = {"message": row}
    asyncio.run(env.bot._dispatch_update(update))
    assert not posted(env)
    assert env.db.one("SELECT COUNT(*) n FROM checkins")["n"] == 0


@pytest.mark.parametrize("state,expected", [("disabled", "未开启"), ("missing", "不可用"),
                                          ("failure", "签到失败")])
@pytest.mark.parametrize("callback", [False, True])
def test_unavailable_or_failed_business_sends_new_clear_feedback(env, monkeypatch, state, expected, callback):
    if state == "disabled": env.services.registry.save("checkin", enabled=False)
    elif state == "missing": env.services.registry._plugins.pop("checkin")
    else:
        def broken(_): raise RuntimeError("do-not-expose-local-credential")
        monkeypatch.setattr(env.services.checkin, "checkin", broken)
    async def run():
        old = await command(env, "/me", chat=VIEWER, user=VIEWER)
        before = copy.deepcopy(env.tg.message(VIEWER, old))
        env.tg.calls.clear()
        if callback: await click(env, "checkin", old, chat=VIEWER, user=VIEWER)
        else: await env.bot._dispatch_update({"message": message()})
        assert expected in posted(env)[-1]["text"]
        assert "do-not-expose-local-credential" not in posted(env)[-1]["text"]
        no_edits(env)
        assert env.tg.message(VIEWER, old) == before
        assert env.db.one("SELECT COUNT(*) n FROM checkins")["n"] == 0
    asyncio.run(run())


def test_membership_denial_stays_enforced_but_posts_new_topic_reply(env, monkeypatch):
    rules = env.bot.membership.rules()
    rules["gate_enabled"] = True
    monkeypatch.setattr(env.bot.membership, "rules", lambda: rules)
    monkeypatch.setattr(env.bot.membership, "admin_state", AsyncMock(return_value="ordinary"))
    monkeypatch.setattr(env.bot.membership, "check", AsyncMock(return_value={"state": "absent", "targets": []}))
    async def run():
        # Create an existing card without invoking a gated update.
        with env.bot._bind_session(GROUP, str(VIEWER), group=True, thread_id=39):
            old = await env.bot.send_message(GROUP, "unchanged old menu",
                [[{"text": "签到", "callback_data": "checkin"}]])
        old_body = copy.deepcopy(env.tg.message(GROUP, old))
        env.tg.calls.clear()
        await click(env, "checkin", old, chat=GROUP, user=VIEWER, thread=39)
        payload = posted(env)[-1]
        assert "签到验证" in payload["text"] and "请先加入" in payload["text"]
        assert payload["message_thread_id"] == 39 and payload["reply_parameters"]["message_id"] == old
        no_edits(env)
        assert env.tg.message(GROUP, old) == old_body
        assert env.db.one("SELECT COUNT(*) n FROM checkins")["n"] == 0
    asyncio.run(run())


def test_group_callback_owner_and_bot_identity_cannot_sign_for_other_user(env):
    async def run():
        old = await command(env, "/me", chat=GROUP, user=VIEWER, thread=49)
        env.tg.calls.clear()
        await click(env, "checkin", old, chat=GROUP, user=SECOND, thread=49)
        assert not posted(env)
        for sender in ({"id": VIEWER, "is_bot": True}, {"id": str(VIEWER), "is_bot": False}, {"id": VIEWER}):
            await env.bot._dispatch_update({"callback_query": {"id": "cb", "data": "checkin", "from": sender,
                "message": message(chat=GROUP, mid=old, thread=49)}})
        assert not posted(env)
        assert env.db.one("SELECT COUNT(*) n FROM checkins")["n"] == 0
    asyncio.run(run())


@pytest.mark.parametrize("state", ["unknown", "rotated"])
def test_checkin_suffix_fails_closed_for_unknown_or_changed_bot_identity(env, state):
    if state == "unknown": env.bot._bot_username = ""
    else: env.cfg["bot_token"] = "456:local-only"
    asyncio.run(env.bot._dispatch_update({"message": message("/checkin@MediaDeckDemoBot")}))
    assert not posted(env)
    assert env.db.one("SELECT COUNT(*) n FROM checkins")["n"] == 0


def test_lost_feedback_then_text_retry_does_not_replay_callback_reward(env):
    async def run():
        old = await command(env, "/me", chat=VIEWER, user=VIEWER)
        proof(env.db, "u1", now=time.time())
        env.tg.fail_send = True
        await click(env, "checkin", old, chat=VIEWER, user=VIEWER)
        balance = env.services.points.balance("u1")
        ledger = env.services.points.ledger("u1")
        env.tg.fail_send = False
        await env.bot._dispatch_update({"message": message("签到")})
        assert "已签到" in posted(env)[-1]["text"]
        assert env.services.points.balance("u1") == balance
        assert env.services.points.ledger("u1") == ledger
        assert env.db.one("SELECT COUNT(*) n FROM checkins")["n"] == 1
    asyncio.run(run())


def test_other_menu_callbacks_keep_original_edit_strategy(env):
    async def run():
        old = await command(env, "/me", chat=VIEWER, user=VIEWER)
        env.tg.calls.clear()
        await click(env, "bag", old, chat=VIEWER, user=VIEWER)
        assert any(m == "editMessageText" for m, _ in env.tg.calls)
        assert not posted(env)
    asyncio.run(run())


@pytest.mark.parametrize("text", ["/转账 20", "/转账@MediaDeckDemoBot 20", "/transfer 20",
                                 "/transfer@mediadeckdemobot 20"])
def test_group_transfer_without_entities_uses_original_confirmation(env, text):
    async def run():
        row = msg(text, mid=501, thread=59)
        assert "entities" not in row
        intent = await dispatch(env, row)
        assert intent and intent["mode"] == "transfer"
        payload = next(p for m,p in env.tg.calls if m == "sendMessage" and "普通积分转账" in p.get("text", ""))
        assert payload["reply_parameters"]["message_id"] == 501 and payload["message_thread_id"] == 59
        await confirm(env, intent, actor=SECOND)
        assert env.services.points.balance("u2") == 0
        await confirm(env, intent)
        await confirm(env, intent)
        assert env.services.points.balance("u1") == 30 and env.services.points.balance("u2") == 20
    asyncio.run(run())


@pytest.mark.parametrize("text", ["转账 20", "/转账@OtherBot 20", "/transfer@OtherBot 20"])
def test_transfer_does_not_add_bare_alias_or_accept_other_bot(env, text):
    asyncio.run(dispatch(env, msg(text)))
    assert env.db.one("SELECT COUNT(*) n FROM group_points_intents")["n"] == 0
