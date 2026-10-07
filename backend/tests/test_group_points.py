"""Real update dispatch + multi-connection financial intent safety.

No live Telegram writes. FakeTelegram models only received updates: privacy
mode upstream non-delivery is explicitly NOT claimed to be locally verifiable.
"""

import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_economy import build
from test_economy_bot import env as economy_env  # noqa: F401
from test_tg_interaction_context import ADMIN, GROUP, SECOND, VIEWER, click, command
from test_tg_interaction_context import env as interaction_env  # noqa: F401

from app.core.db import Database
from app.modules.group_points import GroupPointsError, GroupPointsService

TARGET = 904


@pytest.fixture
def env(request):
    return request.getfixturevalue("economy_env")


def msg(text="/transfer 20", actor=VIEWER, mid=201, chat=GROUP, target=TARGET, thread=0):
    row = dict(
        chat={"id": chat, "type": "supergroup" if chat < 0 else "private"},
        message_id=mid,
        text=text,
        from_user=None,
        reply_to_message=dict(
            message_id=190,
            chat={"id": chat},
            **{"from": {"id": target, "is_bot": False, "first_name": "ViewerA"}},
        ),
        **{"from": {"id": actor, "is_bot": False, "first_name": "Operator"}},
    )
    row.pop("from_user")
    if thread:
        row.update(message_thread_id=thread, is_topic_message=True)
    return row


async def dispatch(e, message):
    await e.bot._dispatch_update({"message": message})
    return e.db.one(
        "SELECT * FROM group_points_intents WHERE chat_id=? AND command_message_id=?",
        (str(message["chat"]["id"]), message["message_id"]),
    )


async def confirm(
    e, intent, actor=None, action="gpok:", chat=None, mid=None, thread=None, is_bot=False
):
    chat = int(intent["chat_id"]) if chat is None else chat
    message = {
        "chat": {"id": chat, "type": "supergroup" if chat < 0 else "private"},
        "message_id": mid or intent["confirmation_message_id"],
    }
    thread = intent["thread_id"] if thread is None else thread
    if thread:
        message.update(message_thread_id=thread, is_topic_message=True)
    await e.bot._dispatch_update(
        {
            "callback_query": dict(
                id="cb",
                data=action + intent["nonce"],
                message=message,
                **{"from": {"id": actor or int(intent["actor_tg_id"]), "is_bot": is_bot}},
            )
        }
    )


def responses(e):
    return "\n".join(
        str(p.get("text", ""))
        for method, p in e.tg.calls
        if method in ("sendMessage", "editMessageText", "answerCallbackQuery")
    )


def test_real_member_transfer_and_system_admin_mint_have_distinct_confirmations_and_ledger(env):
    env.services.registry.save("points_transfer", enabled=True, config={"fee_percent": 10})

    async def run():
        ordinary = await dispatch(env, msg())
        assert ordinary and ordinary["mode"] == "transfer"
        card = env.tg.text(GROUP, ordinary["confirmation_message_id"])
        assert (
            "普通积分转账" in card
            and "数量：<b>20</b>" in card
            and "手续费：2" in card
            and "到账：<b>18</b>" in card
        )
        assert "904" in card and "ViewerB" in card  # not sender's forged display name
        assert env.services.points.balance("u2") == 0
        await asyncio.gather(*[confirm(env, ordinary) for _ in range(5)])
        assert env.services.points.balance("u1") == 30 and env.services.points.balance("u2") == 18
        minted = await dispatch(env, msg("/转账 100", actor=ADMIN, mid=202))
        card = env.tg.text(GROUP, minted["confirmation_message_id"])
        assert "管理员发放" in card and "手续费：0" in card
        assert '不扣' not in card and 'mint' not in card
        assert '904' in card and 'ViewerB' in card
        await asyncio.gather(*[confirm(env, minted) for _ in range(5)])
        success = env.tg.text(GROUP, minted['confirmation_message_id'])
        assert '不扣' not in success and 'mint' not in success
        assert 'ViewerB' in success and '904' in success and '对方到账：100' in success
        assert (
            env.services.points.balance("admin") == 0 and env.services.points.balance("u2") == 118
        )
        assert env.services.points.spent_since("admin", "transfer.out", 0) == 0
        before = len(env.tg.messages)
        duplicate = await dispatch(env, msg("/转账 100", actor=ADMIN, mid=202))
        assert duplicate["nonce"] == minted["nonce"] and len(env.tg.messages) == before

    asyncio.run(run())
    rows = env.services.points.ledger("u2")
    assert (
        len(rows) == 2 and rows[0]["reason"] == "admin.mint" and rows[0]["actor"] == f"tg:{ADMIN}"
    )
    audit = env.db.query("SELECT * FROM audit_log WHERE action LIKE 'points.group.%'")
    assert len(audit) == 2
    mint = next(row for row in audit if row["action"] == "points.group.mint")
    details = json.loads(mint["detail"])
    assert mint["actor"] == f"tg:{ADMIN}" and mint["subject"] == "u2"
    assert (
        details["actor_user_id"] == "admin"
        and details["to_tg_id"] == str(TARGET)
        and details["chat_id"] == str(GROUP)
    )
    assert not {"from_balance", "to_balance"} & json.loads(duplicate_result(env)).keys()


def duplicate_result(e):
    return e.db.one("SELECT result_json FROM group_points_intents WHERE mode='mint'")["result_json"]


@pytest.mark.parametrize(
    "text",
    [
        "/transfer 20",
        "/transfer@MediaDeckDemoBot 20",
        "/TRANSFER@mediadeckdemobot 20",
        "/转账 20",
        "/转账@MediaDeckDemoBot 20",
    ],
)
def test_english_registered_slash_and_chinese_alias_received_updates(env, text):
    async def run():
        intent = await dispatch(env, msg(text))
        assert intent and intent["confirmation_message_id"]
        await confirm(env, intent)

    asyncio.run(run())
    assert env.services.points.balance("u2") == 20


@pytest.mark.parametrize(
    "text", ["/transfer@OtherBot 20", "/转账@OtherBot 20", "转账 20", "transfer 20"]
)
def test_other_bot_mentions_and_noncommand_chatter_do_not_start_financial_intents(env, text):
    asyncio.run(dispatch(env, msg(text)))
    assert not env.db.query("SELECT * FROM group_points_intents")


@pytest.mark.parametrize(
    "text",
    [
        "/transfer",
        "/transfer 0",
        "/transfer -100",
        "/transfer +10",
        "/transfer 1.5",
        "/transfer 1e2",
        "/transfer 100 alice",
        "/transfer 9223372036854775808",
        "/transfer １２",
        "/转账 -1",
        "/transfer 10_000",
    ],
)
def test_invalid_amount_or_extra_target_never_mints_or_debits(env, text):
    asyncio.run(dispatch(env, msg(text, actor=ADMIN)))
    assert not env.db.query("SELECT * FROM group_points_intents")
    assert env.services.points.balance("u2") == 0
    assert "❌" in responses(env)


@pytest.mark.parametrize(
    "defect",
    [
        "no_reply",
        "bot",
        "anonymous",
        "channel",
        "auto_forward",
        "missing_user",
        "missing_bot_flag",
        "unbound",
        "external_only",
        "wrong_chat",
        "deleted_user",
    ],
)
def test_unreliable_targets_rejected_without_username_guessing(env, defect):
    message = msg(actor=ADMIN)
    reply = message["reply_to_message"]
    if defect == "no_reply":
        message.pop("reply_to_message")
    elif defect == "bot":
        reply["from"]["is_bot"] = True
    elif defect in ("anonymous", "channel"):
        reply["sender_chat"] = {"id": GROUP if defect == "anonymous" else -100123}
    elif defect == "auto_forward":
        reply["is_automatic_forward"] = True
    elif defect == "missing_user":
        reply.pop("from")
    elif defect == "missing_bot_flag":
        reply["from"].pop("is_bot")
    elif defect == "unbound":
        reply["from"]["id"] = 7788
        reply["from"]["username"] = "ViewerB"
    elif defect == "external_only":
        message["external_reply"] = message.pop("reply_to_message")
    elif defect == "wrong_chat":
        reply["chat"]["id"] = GROUP - 1
    elif defect == "deleted_user":
        reply["from"]["id"] = 0
    asyncio.run(dispatch(env, message))
    assert not env.db.query("SELECT * FROM group_points_intents")
    assert env.services.points.balance("u2") == 0 and "❌" in responses(env)


def test_forwarded_real_author_not_forward_origin_is_recipient(env):
    message = msg(actor=ADMIN)
    message["reply_to_message"]["forward_origin"] = {
        "type": "user",
        "sender_user": {"id": VIEWER, "is_bot": False},
    }

    async def run():
        intent = await dispatch(env, message)
        assert intent["to_tg_id"] == str(TARGET)
        await confirm(env, intent)

    asyncio.run(run())
    assert env.services.points.balance("u2") == 20 and env.services.points.balance("u1") == 50


@pytest.mark.parametrize("defect", ["anonymous", "channel", "bot", "unbound"])
def test_actor_must_be_reliable_linked_system_identity(env, defect):
    message = msg(actor=ADMIN)
    if defect in ("anonymous", "channel"):
        message["sender_chat"] = {"id": GROUP}
    elif defect == "bot":
        message["from"]["is_bot"] = True
    elif defect == "unbound":
        message["from"]["id"] = 3333
    asyncio.run(dispatch(env, message))
    assert not env.db.query("SELECT * FROM group_points_intents")


def test_tg_group_admin_status_never_confers_mint_permission(env):
    original = env.bot._call

    async def call(method, payload=None, **kw):
        if method == "getChatMember":
            return {"status": "administrator", "can_manage_chat": True, "user": {"id": VIEWER}}
        return await original(method, payload, **kw)

    env.bot._call = call

    async def run():
        intent = await dispatch(env, msg())
        assert intent["mode"] == "transfer"
        await confirm(env, intent)

    asyncio.run(run())
    assert env.services.points.balance("u1") == 30 and env.services.points.balance("u2") == 20
    assert not env.db.query("SELECT * FROM points_ledger WHERE reason='admin.mint'")


@pytest.mark.parametrize(
    "defect", ["actor", "other_admin", "chat", "card", "thread", "bot", "private"]
)
def test_only_initiator_original_group_card_topic_can_confirm_or_cancel(env, defect):
    async def run():
        intent = await dispatch(env, msg(actor=ADMIN, thread=17))
        kwargs = {}
        if defect == "actor":
            kwargs["actor"] = TARGET
        elif defect == "other_admin":
            kwargs["actor"] = SECOND
        elif defect == "chat":
            env.cfg["group_interaction_chats"].append(str(GROUP - 1))
            kwargs["chat"] = GROUP - 1
        elif defect == "private":
            kwargs["chat"] = ADMIN
        elif defect == "card":
            kwargs["mid"] = intent["confirmation_message_id"] + 1
        elif defect == "thread":
            kwargs["thread"] = 18
        elif defect == "bot":
            kwargs["is_bot"] = True
        card = copy.deepcopy(env.tg.message(GROUP, intent["confirmation_message_id"]))
        await confirm(env, intent, **kwargs)
        await confirm(env, intent, action="gpcancel:", **kwargs)
        assert env.bot._group_points_service().get(intent["nonce"])["status"] == "pending"
        assert env.tg.message(GROUP, intent["confirmation_message_id"]) == card
        assert env.services.points.balance("u2") == 0
        await confirm(env, intent)
        assert env.services.points.balance("u2") == 20

    asyncio.run(run())


@pytest.mark.parametrize(
    "change",
    [
        "admin_demoted",
        "member_promoted",
        "actor_unbound",
        "target_unbound",
        "target_swapped",
        "allowlist_removed",
        "fee_changed",
        "expired",
        "gate_denied",
    ],
)
def test_revalidate_authority_binding_configuration_and_expiry_on_confirmation(env, change):
    async def run():
        actor = VIEWER if change in ("member_promoted", "fee_changed") else ADMIN
        intent = await dispatch(env, msg(actor=actor))
        if change == "admin_demoted":
            env.members.set_roles("admin", [])
        elif change == "member_promoted":
            env.members.set_roles("u1", ["admin"])
        elif change == "actor_unbound":
            env.members.unbind_telegram("admin")
        elif change == "target_unbound":
            env.members.unbind_telegram("u2")
        elif change == "target_swapped":
            env.members.unbind_telegram("u2")
            env.members.unbind_telegram("u1")
            env.members.bind_telegram("u1", str(TARGET))
        elif change == "allowlist_removed":
            env.cfg["group_interaction_chats"] = []
        elif change == "fee_changed":
            env.services.registry.save("points_transfer", enabled=True, config={"fee_percent": 10})
        elif change == "expired":
            env.db.execute("UPDATE group_points_intents SET expires_at=0")
        elif change == "gate_denied":

            async def denied(*args):
                return False

            env.bot.membership.gate = denied
        await confirm(env, intent)
        assert env.services.points.balance("u2") == 0 and env.services.points.balance("u1") == 50
        assert env.bot._group_points_service().get(intent["nonce"])["status"] == "pending"

    asyncio.run(run())


def test_restart_retries_confirm_cancel_expired_and_changed_request_payload(env):
    async def run():
        intent = await dispatch(env, msg(actor=ADMIN))
        # Remove all volatile actor-card / session state as a process restart
        # would. Authority lives in DB + current system roles, not these maps.
        env.bot._pending.clear()
        env.bot._card_actor.clear()
        env.bot._panel.clear()
        env.bot._menu_panel.clear()
        await confirm(env, intent)
        await confirm(env, intent)
        assert env.services.points.balance("u2") == 20
        await dispatch(env, msg("/transfer 25", actor=ADMIN))
        assert "同一命令消息" in responses(env) and env.services.points.balance("u2") == 20
        other = await dispatch(env, msg(actor=ADMIN, mid=202))
        await confirm(env, other, action="gpcancel:")
        await confirm(env, other)
        assert env.services.points.balance("u2") == 20

    asyncio.run(run())


def test_unapproved_groups_channel_updates_and_other_bot_commands_are_ignored(env):
    async def run():
        await dispatch(env, msg(actor=ADMIN, chat=GROUP - 1))
        message = msg(actor=ADMIN)
        message["chat"]["type"] = "channel"
        await env.bot._dispatch_update({"channel_post": message})

    asyncio.run(run())
    assert not env.db.query("SELECT * FROM group_points_intents")


def test_group_failure_messages_do_not_disclose_balances_or_prior_sent_totals(env):
    env.services.points.add("u1", 123456, "admin.adjust")
    env.services.registry.save("points_transfer", enabled=True, config={"daily_limit": 20})

    async def run():
        first = await dispatch(env, msg())
        await confirm(env, first)
        await dispatch(env, msg("/transfer 20", mid=202))
        env.services.registry.save("points_transfer", enabled=True, config={"daily_limit": 0})
        await dispatch(env, msg("/transfer 999999", mid=203))
        assert "123486" not in responses(env) and "123506" not in responses(env)
        assert "当前余额" not in responses(env) and "今日已转" not in responses(env)

    asyncio.run(run())
    assert env.services.points.balance("u2") == 20


def test_private_admin_transfer_is_still_ordinary_not_mint(env):
    env.services.points.add("admin", 100, "admin.adjust")

    async def run():
        mid = await command(env, "/transfer 20", chat=ADMIN, user=ADMIN)
        assert "私聊仍为扣本人余额" in env.tg.text(ADMIN, mid)
        await click(env, "transfer", mid, chat=ADMIN, user=ADMIN)
        await command(env, "ViewerB", chat=ADMIN, user=ADMIN)
        await command(env, "20", chat=ADMIN, user=ADMIN)
        mid = env.bot._panel[str(ADMIN)]
        await click(env, "transfer_ok", mid, chat=ADMIN, user=ADMIN)

    asyncio.run(run())
    assert env.services.points.balance("admin") == 80 and env.services.points.balance("u2") == 20
    assert not env.db.query("SELECT * FROM points_ledger WHERE reason='admin.mint'")


@pytest.mark.parametrize("can_read_all", [False, True])
def test_privacy_mode_boundary_registered_english_mention_help_and_no_upstream_state_change(
    env, can_read_all
):
    assert any(c["command"] == "transfer" for c in env.bot._command_list(group=True))
    assert not any("转账" == c["command"] for c in env.bot._command_list(group=True))
    help_text = env.bot._help_text(env.members.get("u1"))
    assert "/transfer@MediaDeckDemoBot" in help_text and "隐私模式" in help_text

    async def run():
        original = env.bot._call

        async def call(method, payload=None, **kwargs):
            if method == "getMe":
                return {
                    "id": 123,
                    "is_bot": True,
                    "username": "MediaDeckDemoBot",
                    "can_read_all_group_messages": can_read_all,
                }
            return await original(method, payload, **kwargs)

        env.bot._call = call
        assert (await env.bot.verify())["ok"]
        group_menus = [
            p
            for method, p in env.tg.calls
            if method == "setMyCommands" and p.get("scope", {}).get("chat_id") == GROUP
        ]
        assert group_menus and any(c["command"] == "transfer" for c in group_menus[-1]["commands"])
        # No update received => no intent, regardless of Telegram capability.
        assert not env.db.query("SELECT * FROM group_points_intents")
        intent = await dispatch(env, msg("/transfer@MediaDeckDemoBot 20"))
        await confirm(env, intent)

    asyncio.run(run())
    assert not any(
        m in ("setChatPermissions", "promoteChatMember", "setWebhook") for m, p in env.tg.calls
    )


def services(e, n=5):
    path = e.db._conn.execute("PRAGMA database_list").fetchone()[2]
    dbs = [Database(path) for _ in range(n)]
    out = []
    for db in dbs:
        s = build(db)
        s.registry.save("points_transfer", enabled=True)
        out.append(
            GroupPointsService(
                db,
                s.members,
                s.points,
                s.transfer,
                e.bot.is_admin,
                e.bot._group_chat_allowed,
                lambda: True,
            )
        )
    return dbs, out


@pytest.mark.parametrize("actor", [ADMIN, VIEWER])
def test_multiple_connections_same_message_and_callback_mint_or_transfer_once(env, actor):
    dbs, workers = services(env)
    try:
        with ThreadPoolExecutor(max_workers=5) as pool:
            intents = list(pool.map(lambda s: s.prepare(msg(actor=actor), 20), workers))
        assert len({i["nonce"] for i in intents}) == 1
        intent = intents[0]
        workers[0].bind_card(intent["nonce"], 401)
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(
                pool.map(
                    lambda s: s.resolve(intent["nonce"], str(actor), msg()["chat"], 401), workers
                )
            )
        assert all(r == results[0] for r in results)
        assert env.services.points.balance("u2") == 20
        assert env.services.points.balance("u1") == (30 if actor == VIEWER else 50)
        assert len(env.db.query("SELECT * FROM audit_log WHERE action LIKE 'points.group.%'")) == 1
    finally:
        for db in dbs:
            db.close()


@pytest.mark.parametrize("cap,successes", [(0, 2), (30, 1)])
def test_multiple_connections_distinct_requests_cannot_overdraw_or_evade_daily_limit(
    env, cap, successes
):
    dbs, workers = services(env)
    try:
        intents = []
        for i, s in enumerate(workers):
            s.transfer.ctx.registry.save(
                "points_transfer", enabled=True, config={"daily_limit": cap}
            )
            intent = s.prepare(msg(mid=201 + i), 20)
            s.bind_card(intent["nonce"], 401 + i)
            intents.append(s.get(intent["nonce"]))

        def resolve(args):
            s, i = args
            try:
                return s.resolve(
                    i["nonce"], str(VIEWER), msg()["chat"], i["confirmation_message_id"]
                )
            except GroupPointsError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(resolve, zip(workers, intents)))
        assert sum(isinstance(r, dict) for r in results) == successes
        assert (
            env.services.points.balance("u1") == 50 - 20 * successes
            and env.services.points.balance("u2") == 20 * successes
        )
    finally:
        for db in dbs:
            db.close()


@pytest.mark.parametrize("mode", ["mint", "transfer"])
def test_failed_audit_rolls_back_credit_debit_receipt_and_confirmation(env, mode):
    service = env.bot._group_points_service()
    actor = ADMIN if mode == "mint" else VIEWER
    intent = service.prepare(msg(actor=actor), 20)
    service.bind_card(intent["nonce"], 401)
    env.db.execute(
        "CREATE TRIGGER fail_group_audit BEFORE INSERT ON audit_log WHEN NEW.action LIKE 'points.group.%' BEGIN SELECT RAISE(ABORT,'injected audit failure'); END"
    )
    with pytest.raises(Exception, match="injected audit failure"):
        service.resolve(intent["nonce"], str(actor), msg()["chat"], 401)
    assert env.services.points.balance("u1") == 50 and env.services.points.balance("u2") == 0
    assert service.get(intent["nonce"])["status"] == "pending"
    assert not env.db.query("SELECT * FROM economy_receipts")
    env.db.execute("DROP TRIGGER fail_group_audit")
    service.resolve(intent["nonce"], str(actor), msg()["chat"], 401)
    assert env.services.points.balance("u2") == 20


def test_admin_mint_remains_independent_of_member_switch_and_limit(env):
    env.services.registry.save(
        "points_transfer", enabled=False, config={"enabled_for_members": False, "daily_limit": 1}
    )

    async def run():
        mint = await dispatch(env, msg("/transfer 100", actor=ADMIN))
        assert mint["mode"] == "mint"
        await confirm(env, mint)
        await dispatch(env, msg(mid=202))

    asyncio.run(run())
    assert env.services.points.balance("u2") == 100
    assert len(env.db.query("SELECT * FROM group_points_intents")) == 1


def test_confirmation_send_failure_cannot_commit_and_duplicate_delivery_can_recover(env):
    env.tg.fail_send = True

    async def run():
        pending = await dispatch(env, msg(actor=ADMIN))
        assert pending and pending["confirmation_message_id"] is None
        assert env.services.points.balance("u2") == 0
        env.tg.fail_send = False
        intent = await dispatch(env, msg(actor=ADMIN))
        assert intent["nonce"] == pending["nonce"] and intent["confirmation_message_id"]
        await confirm(env, intent)

    asyncio.run(run())
    assert env.services.points.balance("u2") == 20


@pytest.mark.parametrize(
    "restriction", ["suspended", "expired", "missing", "negative", "minimum", "receiver"]
)
def test_group_ordinary_transfer_retains_original_account_and_minimum_rules(env, restriction):
    async def run():
        intent = await dispatch(env, msg())
        if restriction == "suspended":
            env.members.set_status("u1", "suspended")
        elif restriction == "expired":
            env.db.execute("UPDATE members SET expires_at=1 WHERE emby_user_id='u1'")
        elif restriction == "missing":
            env.db.execute("UPDATE members SET emby_missing_since=1 WHERE emby_user_id='u1'")
        elif restriction == "negative":
            env.services.points.add("u1", -60, "checkin")
        elif restriction == "minimum":
            env.services.registry.save("points_transfer", enabled=True, config={"min_amount": 30})
        elif restriction == "receiver":
            env.members.set_status("u2", "suspended")
            env.services.registry.save(
                "points_transfer", enabled=True, config={"restrict_receivers": True}
            )
        await confirm(env, intent)
        assert env.services.points.balance("u2") == 0
        assert env.bot._group_points_service().get(intent["nonce"])["status"] == "pending"
        assert not env.db.query("SELECT * FROM points_ledger WHERE reason LIKE 'transfer.%'")

    asyncio.run(run())


def test_actual_dispatch_database_failure_and_lost_reply_do_not_recredit(env):
    async def run():
        intent = await dispatch(env, msg(actor=ADMIN))
        env.db.execute(
            "CREATE TRIGGER fail_mint BEFORE INSERT ON points_ledger WHEN NEW.reason='admin.mint' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        await confirm(env, intent)
        assert "数据库暂不可用" in responses(env)
        assert env.services.points.balance("u2") == 0
        env.db.execute("DROP TRIGGER fail_mint")
        original = env.bot._call

        async def fail_post_commit(method, payload=None, **kwargs):
            if method in ("answerCallbackQuery", "editMessageText"):
                return None
            return await original(method, payload, **kwargs)

        env.bot._call = fail_post_commit
        await confirm(env, intent)  # committed credit, both transport responses lost
        assert env.services.points.balance("u2") == 20
        env.bot._call = original
        await confirm(env, intent)
        assert env.services.points.balance("u2") == 20
        assert "发放成功" in env.tg.text(GROUP, intent["confirmation_message_id"])

    asyncio.run(run())


def test_same_command_cannot_be_rebound_to_new_amount_or_target_or_actor(env):
    async def run():
        original = await dispatch(env, msg(actor=ADMIN))
        await dispatch(env, msg("/transfer 21", actor=ADMIN))
        await dispatch(env, msg(actor=ADMIN, target=VIEWER))
        await dispatch(env, msg(actor=SECOND))
        assert len(env.db.query("SELECT * FROM group_points_intents")) == 1
        intent = env.bot._group_points_service().get(original["nonce"])
        assert intent["to_tg_id"] == str(TARGET) and intent["amount"] == 20
        assert env.services.points.balance("u2") == 0
        await confirm(env, original)
        assert env.services.points.balance("u2") == 20

    asyncio.run(run())


def test_mint_integer_storage_boundary_does_not_wrap_or_allow_negative_deduction(env):
    async def run():
        first = await dispatch(env, msg("/transfer 9223372036854775807", actor=ADMIN))
        await confirm(env, first)
        assert env.services.points.balance("u2") == 2**63 - 1
        next_intent = await dispatch(env, msg("/transfer 1", actor=ADMIN, mid=202))
        await confirm(env, next_intent)
        assert env.services.points.balance("u2") == 2**63 - 1
        assert "超出可存储范围" in responses(env)
        assert len(env.db.query("SELECT * FROM points_ledger WHERE reason='admin.mint'")) == 1

    asyncio.run(run())
