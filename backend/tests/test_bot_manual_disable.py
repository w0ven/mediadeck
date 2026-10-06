"""/kk manual access: same bound target card, actor, thread and one-use confirmation."""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from test_manual_account_disable import service as manual_service  # noqa: F401

from app.modules.telegram import TelegramBot


@pytest.fixture
def bot(request):
    service = request.getfixturevalue('manual_service')
    members = service.members
    members.upsert("reviewer", "operator", {"roles": ["admin"]})
    members.bind_telegram("reviewer", "12")
    members.upsert("reviewer2", "operator2", {"roles": ["admin"]})
    members.bind_telegram("reviewer2", "13")
    service.emby._users["reviewer"] = {"Id": "reviewer", "Policy": {"IsAdministrator": True}}
    cfg = {"enabled": True, "bot_token": "123:test-only", "group_interaction_chats": ["-1001"]}
    result = TelegramBot(lambda: cfg, members, service.emby, db=service.db)
    result._restrictions = service
    result.calls = []
    result.mid = 70
    async def call(method, payload=None, timeout=20):
        result.calls.append((method, payload or {}))
        if method == "sendMessage":
            result.mid += 1
            return {"message_id": result.mid}
        if method.startswith("editMessage"):
            return {"message_id": payload["message_id"]}
        return True
    result._call = call
    return result


def message(group=False, *, text="/kk alice", user=12, thread=None):
    row = {"chat": {"id": -1001 if group else user, "type": "supergroup" if group else "private"},
           "from": {"id": user, "first_name": "operator"}, "text": text, "message_id": 1}
    if thread:
        row["message_thread_id"] = thread
        row["is_topic_message"] = True
    return row


def callback(data, group=False, *, user=12, mid=71, thread=None, chat=None):
    row = message(group, user=user, thread=thread)
    row.pop("from")
    row.pop("text")
    row["message_id"] = mid
    if chat is not None:
        row["chat"]["id"] = chat
    return {"id": "cb", "data": data, "from": {"id": user}, "message": row}


def nonce(bot, group=False, mid=None):
    panel = bot._admin_panels[(str(-1001 if group else 12), mid or bot.mid)]
    return panel["pending"][2]["nonce"]


@pytest.mark.parametrize("group", [False, True])
def test_kk_visible_explicit_buttons_confirmation_and_roundtrip(bot, group):
    async def run():
        await bot._handle_message(message(group))
        first = bot.mid
        assert "禁用账号" in str(bot.calls) and "解除禁用" not in str(bot.calls)
        assert "本地封禁：未封禁" in str(bot.calls) and "Emby：未禁用" in str(bot.calls)
        await bot._handle_callback(callback("admin_disable", group, mid=first))
        assert bot._members.get("u1")["status"] == "active"  # preview only
        key = nonce(bot, group, first)
        assert "不重置用量" in str(bot.calls)
        await bot._handle_callback(callback("admin_access_ok:" + key, group, mid=first))
        assert bot._members.get("u1")["status"] == "suspended"
        assert bot._emby._users["u1"]["Policy"]["IsDisabled"] is True
        await bot._handle_callback(callback("admin_access_ok:" + key, group, mid=first))
        assert len(bot._restrictions.events()) == 1
        await bot._handle_callback(callback("admin_enable", group, mid=first))
        await bot._handle_callback(callback("admin_access_ok:" + nonce(bot, group, first), group, mid=first))
        assert bot._members.get("u1")["status"] == "active"
        assert bot._emby._users["u1"]["Policy"]["IsDisabled"] is False
        assert "账号已可用" in str(bot.calls)
    asyncio.run(run())


@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("bad", ["other_admin", "other_user", "wrong_message", "wrong_chat", "old_nonce", "expired", "revoked", "rebound", "missing_target", "cancel"])
def test_confirmation_rejects_wrong_actor_binding_old_and_changed_authority(bot, group, bad):
    async def run():
        await bot._handle_message(message(group))
        first = bot.mid
        await bot._handle_callback(callback("admin_disable", group, mid=first))
        key = nonce(bot, group, first)
        data, args = "admin_access_ok:" + key, {}
        if bad == "other_admin":
            args["user"] = 13
        elif bad == "other_user":
            args["user"] = 123
        elif bad == "wrong_message":
            args["mid"] = first + 900
        elif bad == "wrong_chat":
            args["chat"] = -1009 if group else 90
        elif bad == "old_nonce":
            await bot._handle_callback(callback("admin_enable", group, mid=first))
        elif bad == "expired":
            panel = bot._admin_panels[(str(-1001 if group else 12), first)]
            panel["pending"] = ("admin_access_confirm", time.time() - 1, panel["pending"][2])
        elif bad == "revoked":
            bot._members.set_roles("reviewer", [])
        elif bad == "rebound":
            bot._members.bind_telegram("reviewer2", "12")
        elif bad == "missing_target":
            bot._members.delete("u1", cascade=False)
        elif bad == "cancel":
            await bot._handle_callback(callback("admin_card", group, mid=first))
        args.setdefault("mid", first)
        await bot._handle_callback(callback(data, group, **args))
        assert bot._emby._users["u1"]["Policy"]["IsDisabled"] is False
        assert bot._restrictions.events() == []
    asyncio.run(run())


def test_group_topic_and_anonymous_admin_cannot_trigger(bot):
    async def run():
        anonymous = message(True)
        anonymous["sender_chat"] = {"id": -1001}
        await bot._handle_message(anonymous)
        assert not bot._admin_panels
        await bot._handle_message(message(True, thread=10))
        first = bot.mid
        await bot._handle_callback(callback("admin_disable", True, mid=first, thread=10))
        key = nonce(bot, True, first)
        await bot._handle_callback(callback("admin_access_ok:" + key, True, mid=first, thread=11))
        cb = callback("admin_access_ok:" + key, True, mid=first, thread=10)
        cb["message"]["sender_chat"] = {"id": -1001}
        await bot._handle_callback(cb)
        assert bot._restrictions.events() == []
        assert bot._emby._users["u1"]["Policy"]["IsDisabled"] is False
    asyncio.run(run())


def test_old_card_does_not_borrow_new_target_or_confirmation(bot):
    async def run():
        await bot._handle_message(message())
        first = bot.mid
        await bot._handle_callback(callback("admin_disable", mid=first))
        key = nonce(bot, mid=first)
        bot._members.upsert("u2", "bob", {"group_id": "standard"})
        await bot._handle_message(message(text="/kk bob"))
        second = bot.mid
        assert second != first
        await bot._handle_callback(callback("admin_access_ok:" + key, mid=second))
        await bot._handle_callback(callback("admin_access_ok:" + key, mid=first))
        assert bot._restrictions.events() == []
        assert bot._members.get("u1")["status"] == "active"
        assert bot._members.get("u2")["status"] == "active"
    asyncio.run(run())


def test_permission_revoked_during_fresh_target_read(bot):
    async def run():
        await bot._handle_message(message())
        first = bot.mid
        await bot._handle_callback(callback("admin_disable", mid=first))
        key = nonce(bot, mid=first)
        read = bot._emby.list_users
        async def changed():
            bot._members.set_roles("reviewer", [])
            return await read()
        bot._emby.list_users = changed
        await bot._handle_callback(callback("admin_access_ok:" + key, mid=first))
        assert bot._restrictions.events() == []
        assert bot._emby._users["u1"]["Policy"]["IsDisabled"] is False
    asyncio.run(run())


def test_duplicate_callback_concurrent_is_consumed_before_await(bot):
    async def run():
        await bot._handle_message(message())
        first = bot.mid
        await bot._handle_callback(callback("admin_disable", mid=first))
        key = nonce(bot, mid=first)
        write = AsyncMock(wraps=bot._emby.apply_member_policy)
        bot._emby.apply_member_policy = write
        await asyncio.gather(*[bot._handle_callback(callback("admin_access_ok:" + key, mid=first)) for _ in range(2)])
        assert write.await_count == 1
        assert len(bot._restrictions.events()) == 1
    asyncio.run(run())


def test_remote_only_disable_visible_release_and_failure_truthful(bot):
    async def run():
        bot._emby._users["u1"]["Policy"]["IsDisabled"] = True
        await bot._handle_message(message())
        first = bot.mid
        assert "本地封禁：未封禁 · Emby：已禁用" in str(bot.calls)
        buttons = next(p['reply_markup']['inline_keyboard'] for m,p in reversed(bot.calls)
                       if m in ('sendMessage','editMessageText'))
        assert any(b.get('callback_data') == 'admin_enable' for row in buttons for b in row)
        assert not any(b.get('callback_data') == 'admin_disable' for row in buttons for b in row)
        await bot._handle_callback(callback("admin_enable", mid=first))
        assert bot._restrictions.events() == []  # still a confirmation, no mutation
        key = nonce(bot, mid=first)
        bot._emby.apply_member_policy = AsyncMock(return_value={"status": "failed"})
        await bot._handle_callback(callback("admin_access_ok:" + key, mid=first))
        assert "可重试" in str(bot.calls)
        assert "账号已可用" not in str(bot.calls)
        assert "Emby：已禁用" in str(bot.calls)
    asyncio.run(run())


@pytest.mark.parametrize('state', ['unknown', 'conflict'])
def test_held_disable_confirmation_rechecks_remote_state_and_refuses_changed_action_or_unknown(bot, state):
    async def run():
        await bot._handle_message(message())
        first = bot.mid
        await bot._handle_callback(callback('admin_disable', mid=first))
        key = nonce(bot, mid=first)
        if state == 'unknown':
            bot._emby._users['u1']['Policy'].pop('IsDisabled')
        else:
            bot._emby._users['u1']['Policy']['IsDisabled'] = True
        bot._emby.apply_member_policy = AsyncMock()
        await bot._handle_callback(callback('admin_access_ok:' + key, mid=first))
        assert bot._restrictions.events() == []
        bot._emby.apply_member_policy.assert_not_awaited()
        assert bot._members.get('u1')['status'] == 'active'
        assert '未执行，请核实' in str(bot.calls)
    asyncio.run(run())


def test_more_management_cancels_hidden_confirmation_and_keeps_bound_target(bot):
    async def run():
        await bot._handle_message(message())
        first = bot.mid
        await bot._handle_callback(callback('admin_disable', mid=first))
        key = nonce(bot, mid=first)
        await bot._handle_callback(callback('admin_more', mid=first))
        panel = bot._admin_panels[('12', first)]
        assert panel['user_id'] == 'u1' and panel['pending'] is None
        await bot._handle_callback(callback('admin_access_ok:' + key, mid=first))
        assert bot._restrictions.events() == []
    asyncio.run(run())


@pytest.mark.parametrize('group', [False, True])
@pytest.mark.parametrize('source,limit', [
    ('remote_only', None), ('local_only', None),
    ('remote_only', 'expired'), ('local_only', 'expired'),
    ('remote_only', 'exhausted'), ('local_only', 'exhausted'), ('remote_only', 'pending'),
])
def test_known_independent_disable_sources_have_enable_button_and_confirmed_execution(bot, group, source, limit):
    async def run():
        local = 'suspended' if source == 'local_only' else 'pending' if limit == 'pending' else 'active'
        bot._members.set_status('u1', local)
        if limit == 'expired':
            bot._members.upsert('u1', 'alice', {'expires_at': int(time.time()) - 10})
        elif limit == 'exhausted':
            bot._restrictions.db.execute("UPDATE members SET traffic_used_bytes=9999999999999 WHERE emby_user_id='u1'")
        bot._emby._users['u1']['Policy']['IsDisabled'] = source == 'remote_only'
        before = bot._members.get('u1')
        await bot._handle_message(message(group))
        first = bot.mid
        rows = next(p['reply_markup']['inline_keyboard'] for m,p in reversed(bot.calls)
                    if m in ('sendMessage','editMessageText'))
        actions = {b.get('callback_data') for row in rows for b in row}
        assert 'admin_enable' in actions and 'admin_disable' not in actions
        assert '一致前不可操作' not in str(bot.calls) and '未知或不一致' not in str(bot.calls)
        await bot._handle_callback(callback('admin_enable', group, mid=first))
        assert not bot._restrictions.events()  # necessary confirmation is retained
        assert '到期、耗尽、待开通限制仍适用' in str(bot.calls)
        key = nonce(bot, group, first)
        await bot._handle_callback(callback('admin_access_ok:' + key, group, mid=first))
        after = bot._members.get('u1')
        assert after['status'] == ('pending' if limit == 'pending' else 'active')
        for field in ('expires_at', 'traffic_used_bytes', 'group_id', 'roles', 'overrides'):
            assert after[field] == before[field]
        assert len(bot._restrictions.events()) == 1
        if limit:
            assert bot._emby._users['u1']['Policy']['IsDisabled'] is True
            assert '仍不可用' in str(bot.calls) and '账号已可用' not in str(bot.calls)
        else:
            assert bot._emby._users['u1']['Policy']['IsDisabled'] is False
            assert '账号已可用' in str(bot.calls)
        await bot._handle_callback(callback('admin_access_ok:' + key, group, mid=first))
        assert len(bot._restrictions.events()) == 1
    asyncio.run(run())
