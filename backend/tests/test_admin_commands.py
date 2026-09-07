"""Admin commands in the bot.

Two things are being pinned down for every command.

**Authority is re-read, never remembered.** Admin is the 'admin' role on a
linked member, checked at the moment the command runs and again when a
confirmation is tapped. A confirmation dialog can sit on somebody's screen for
a long time, and the person who taps it may no longer be who they were when
they typed it.

**The irreversible ones ask first.** /rm deletes an account (and optionally
its inviter) plus the Emby user; /renewall and /scoreall touch everybody.
Each shows what it is about to do and does nothing until a button is pressed
-- cascade is a separate button, because that is the account nobody named.

Every command is also tested for the two boring refusals that would otherwise
be discovered in production: a non-admin sending it, and a username that does
not exist.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.core.db import Database
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.points import PointsService
from app.modules.registration import RegistrationService
from app.modules.requests import RequestService
from app.modules.shop import GB, KBPS_PER_MBPS, ShopService
from app.modules.telegram import TelegramBot

FAKE_CRED = "1234567" + ":" + "placeholder-not-a-real-credential"

ADMIN_CHAT = "900"
PLAIN_CHAT = "901"


class _FakeEmby:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_user(self, user_id: str) -> bool:
        self.deleted.append(str(user_id))
        return True


@pytest.fixture()
def bot(tmp_path):
    db = Database(tmp_path / "admin.db")
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    points = PointsService(db)
    shop = ShopService(db, members, points)
    registration = RegistrationService(db, groups)
    requests = RequestService(db, members, groups, tmdb=None)
    emby = _FakeEmby()

    # The admin: a member with the role and a linked chat. There is no
    # separate list of privileged ids by design.
    members.upsert("admin1", "root", {"group_id": "standard"}, actor="test")
    members.set_roles("admin1", ["admin"], actor="test")
    members.bind_telegram("admin1", ADMIN_CHAT, "rootadmin", actor="test")

    members.upsert("u1", "alice", {"group_id": "standard"}, actor="test")
    members.bind_telegram("u1", PLAIN_CHAT, "alice_tg", actor="test")

    cfg = {"enabled": True, "bot_token": FAKE_CRED, "register_days": 30,
           "max_users": 0, "require_group": "", "default_group_id": "",
           "allow_admin_grant": True, "allow_invite": True,
           "allow_redeem": True}
    instance = TelegramBot(lambda: cfg, members, emby=emby, db=db,
                           registration=registration, points=points,
                           shop=shop, requests=requests, groups=groups)
    instance.sent = []
    instance.edits = []

    async def fake_send(chat, text, keyboard=None):
        instance.sent.append((str(chat), text, keyboard))
        return True

    async def fake_edit(chat, mid, text, keyboard=None):
        instance.edits.append((str(chat), text))
        return True

    instance.send = fake_send
    instance._edit = fake_edit
    instance.db = db
    instance.groups = groups
    instance.members = members
    instance.points = points
    instance.shop = shop
    instance.requests = requests
    instance.emby = emby
    return instance


def _run(bot, text: str, chat: str = ADMIN_CHAT, username: str = "rootadmin"):
    """Send a command as if it arrived from Telegram."""
    asyncio.run(bot._handle_message({
        "chat": {"id": chat},
        "from": {"id": chat, "username": username, "first_name": "T"},
        "text": text,
    }))
    return bot.sent[-1][1] if bot.sent else ""


def _tap(bot, data: str, chat: str = ADMIN_CHAT):
    asyncio.run(bot._handle_callback({
        "id": "cb1", "data": data,
        "message": {"chat": {"id": chat}, "message_id": 7},
        "from": {"id": chat, "username": "rootadmin", "first_name": "T"},
    }))
    return bot.edits[-1][1] if bot.edits else ""


ALL_COMMANDS = [
    "/kk alice", "/prouser alice", "/revuser alice", "/renew alice 30",
    "/renewall 7", "/rm alice", "/score alice 10", "/scoreall 5",
    "/gift alice traffic 50", "/code standard 30 2", "/invite alice 3",
    "/auth 123456", "/req", "/help",
]


# -- authority ---------------------------------------------------------------

@pytest.mark.parametrize("command", ALL_COMMANDS)
def test_a_non_admin_is_refused_every_command(bot, command) -> None:
    """A linked member without the role has no more power than a stranger."""
    reply = _run(bot, command, chat=PLAIN_CHAT, username="alice_tg")
    if command == "/help":
        # /help is the one that stays useful: a member gets their own menu
        # rather than a refusal for a command they may have typed by accident.
        assert "管理员命令" not in reply
    else:
        assert "无权限" in reply


@pytest.mark.parametrize("command", ALL_COMMANDS)
def test_an_unlinked_stranger_is_refused_every_command(bot, command) -> None:
    reply = _run(bot, command, chat="404404", username="nobody")
    assert "管理员命令" not in reply


def test_admin_is_the_role_not_a_configured_id(bot) -> None:
    """Dropping the role takes the power away immediately."""
    assert "root" in _run(bot, "/kk root")
    bot.members.set_roles("admin1", [], actor="test")
    assert "无权限" in _run(bot, "/kk root")


@pytest.mark.parametrize("command", [
    "/kk", "/prouser", "/revuser", "/renew", "/rm", "/score", "/gift",
    "/invite",
])
def test_a_command_needing_a_user_says_so_when_given_none(bot, command) -> None:
    assert "请指定用户" in _run(bot, command)


@pytest.mark.parametrize("command", [
    "/kk ghost", "/prouser ghost", "/revuser ghost", "/renew ghost 30",
    "/rm ghost", "/score ghost 10", "/gift ghost traffic 5", "/invite ghost 2",
])
def test_an_unknown_username_is_reported_not_guessed(bot, command) -> None:
    assert "找不到该用户" in _run(bot, command)


def test_an_unknown_command_points_at_the_help(bot) -> None:
    assert "未知命令" in _run(bot, "/frobnicate")


def test_a_command_addressed_to_the_bot_by_name_still_works(bot) -> None:
    """Telegram appends @botname in groups."""
    assert "管理员命令" in _run(bot, "/help@keleembybot")


# -- /help -------------------------------------------------------------------

def test_help_lists_every_command(bot) -> None:
    reply = _run(bot, "/help")
    for command in ("/kk", "/prouser", "/revuser", "/renew", "/renewall",
                    "/rm", "/score", "/scoreall", "/gift", "/code",
                    "/invite", "/auth", "/req"):
        assert command in reply


# -- /kk ---------------------------------------------------------------------

def test_kk_reports_the_account_at_a_glance(bot) -> None:
    bot.points.add("u1", 120, "test", actor="test")
    reply = _run(bot, "/kk alice")
    for fragment in ("alice", "状态", "用户组", "有效期", "积分", "注册渠道",
                     "邀请人", "下级", "Telegram", "设备数", "最近活跃",
                     "求片剩余"):
        assert fragment in reply
    assert "120" in reply


def test_kk_accepts_an_at_handle(bot) -> None:
    """Operators type the Telegram handle they can see, not the Emby login."""
    assert "alice" in _run(bot, "/kk @alice_tg")


def test_kk_is_case_insensitive_on_the_emby_name(bot) -> None:
    assert "alice" in _run(bot, "/kk ALICE")


# -- /prouser and /revuser ---------------------------------------------------

def test_prouser_moves_the_account_into_the_whitelist(bot) -> None:
    reply = _run(bot, "/prouser alice")
    assert "白名单" in reply
    assert bot.members.get("u1")["group_id"] == "whitelist"


def test_prouser_recreates_a_missing_whitelist_group(bot) -> None:
    """The command names a specific group, so it must not depend on the
    operator never having tidied their group list."""
    bot.groups.delete("whitelist")
    _run(bot, "/prouser alice")
    assert bot.members.get("u1")["group_id"] == "whitelist"


def test_revuser_moves_the_account_back_to_the_default_group(bot) -> None:
    _run(bot, "/prouser alice")
    reply = _run(bot, "/revuser alice")
    assert "默认组" in reply
    assert bot.members.get("u1")["group_id"] == "standard"


# -- /renew ------------------------------------------------------------------

def test_renew_extends_the_term(bot) -> None:
    before = bot.members.get("u1")["expires_at"] or int(time.time())
    reply = _run(bot, "/renew alice 30")
    after = bot.members.get("u1")["expires_at"]
    assert "已续期 30 天" in reply
    assert after >= before + 29 * 86400


def test_renew_without_a_day_count_explains_the_syntax(bot) -> None:
    assert "用法" in _run(bot, "/renew alice")
    assert "用法" in _run(bot, "/renew alice soon")


def test_renewall_does_nothing_until_it_is_confirmed(bot) -> None:
    """Everybody at once is not undoable."""
    before = bot.members.get("u1")["expires_at"]
    prompt = _run(bot, "/renewall 7")
    assert "确认" in prompt
    assert bot.members.get("u1")["expires_at"] == before

    result = _tap(bot, "admin_ok")
    assert "已为 2 个账号各续期 7 天" in result
    assert bot.members.get("u1")["expires_at"] > (before or 0)


def test_renewall_can_be_cancelled(bot) -> None:
    before = bot.members.get("u1")["expires_at"]
    _run(bot, "/renewall 7")
    assert "已取消" in _tap(bot, "admin_cancel")
    assert bot.members.get("u1")["expires_at"] == before


def test_a_confirmation_is_refused_if_the_role_was_dropped_meanwhile(bot) -> None:
    """The dialog may sit on screen while the operator is demoted."""
    before = bot.members.get("u1")["expires_at"]
    _run(bot, "/renewall 7")
    bot.members.set_roles("admin1", [], actor="test")

    assert "无权限" in _tap(bot, "admin_ok")
    assert bot.members.get("u1")["expires_at"] == before


def test_confirming_twice_does_not_run_it_twice(bot) -> None:
    """The pending state is consumed, so a double tap is inert."""
    _run(bot, "/renewall 7")
    _tap(bot, "admin_ok")
    after_first = bot.members.get("u1")["expires_at"]

    assert "过期" in _tap(bot, "admin_ok")
    assert bot.members.get("u1")["expires_at"] == after_first


# -- /rm ---------------------------------------------------------------------

def test_rm_previews_the_cascade_before_deleting_anything(bot) -> None:
    """Cascade removes an account nobody named. That has to be on screen
    before the button, not in the confirmation afterwards."""
    bot.members.upsert("u2", "carol", {"group_id": "standard",
                                       "inviter_id": "u1"}, actor="test")

    prompt = _run(bot, "/rm carol")

    assert "确认删除" in prompt
    assert "连带邀请人" in prompt and "alice" in prompt
    assert "只删本人" in prompt
    assert bot.members.get("u2") is not None
    assert bot.members.get("u1") is not None


def test_rm_deletes_the_member_the_inviter_and_the_emby_accounts(bot) -> None:
    bot.members.upsert("u2", "carol", {"group_id": "standard",
                                       "inviter_id": "u1"}, actor="test")
    _run(bot, "/rm carol")

    result = _tap(bot, "rm_cascade")

    assert "已删除" in result
    assert bot.members.get("u2") is None
    assert bot.members.get("u1") is None
    assert set(bot.emby.deleted) == {"u2", "u1"}


def test_rm_can_be_cancelled_and_removes_nothing(bot) -> None:
    _run(bot, "/rm alice")
    assert "已取消" in _tap(bot, "admin_cancel")
    assert bot.members.get("u1") is not None
    assert bot.emby.deleted == []


def test_rm_without_an_inviter_reports_no_cascade(bot) -> None:
    prompt = _run(bot, "/rm alice")
    assert "连带删除" not in prompt
    assert "连带邀请人" not in prompt


def test_rm_self_leaves_the_inviter(bot) -> None:
    bot.members.upsert("u2", "carol", {"group_id": "standard",
                                       "inviter_id": "u1"}, actor="test")
    _run(bot, "/rm carol")
    result = _tap(bot, "rm_self")
    assert "仅本人" in result
    assert bot.members.get("u2") is None
    assert bot.members.get("u1") is not None
    assert bot.emby.deleted == ["u2"]


# -- /score and /scoreall ----------------------------------------------------

def test_score_credits_and_debits(bot) -> None:
    assert "+50" in _run(bot, "/score alice +50")
    assert bot.points.balance("u1") == 50
    assert "-20" in _run(bot, "/score alice -20")
    assert bot.points.balance("u1") == 30


def test_score_cannot_push_a_balance_negative(bot) -> None:
    reply = _run(bot, "/score alice -10")
    assert "❌" in reply
    assert bot.points.balance("u1") == 0


def test_score_rejects_a_non_numeric_amount(bot) -> None:
    assert "整数" in _run(bot, "/score alice lots")


def test_score_is_written_to_the_ledger_with_the_admin_as_actor(bot) -> None:
    _run(bot, "/score alice 25")
    entry = bot.points.ledger("u1", 5)[0]
    assert entry["delta"] == 25
    assert entry["reason"] == "admin.adjust"
    assert "rootadmin" in str(entry.get("actor") or "")


def test_scoreall_needs_confirmation_then_credits_everyone(bot) -> None:
    prompt = _run(bot, "/scoreall 10")
    assert "确认" in prompt
    assert bot.points.balance("u1") == 0

    result = _tap(bot, "admin_ok")
    assert "2 个账号" in result
    assert bot.points.balance("u1") == 10
    assert bot.points.balance("admin1") == 10


def test_scoreall_can_be_cancelled(bot) -> None:
    _run(bot, "/scoreall 10")
    _tap(bot, "admin_cancel")
    assert bot.points.balance("u1") == 0


# -- /gift -------------------------------------------------------------------

def test_gift_traffic_adds_the_same_bytes_the_shop_would(bot) -> None:
    """A gift and a purchase must be the same write, or the two paths drift."""
    reply = _run(bot, "/gift alice traffic 50")
    assert "50GB" in reply
    overrides = bot.members.get("u1")["overrides"]
    assert overrides["extra_traffic_bytes"] == 50 * GB


def test_gift_days_extends_the_term(bot) -> None:
    before = bot.members.get("u1")["expires_at"] or int(time.time())
    _run(bot, "/gift alice days 7")
    assert bot.members.get("u1")["expires_at"] >= before + 6 * 86400


def test_gift_bandwidth_raises_the_cap(bot) -> None:
    bot.members.set_overrides("u1", {"bandwidth_limit_kbps": 1000},
                              actor="test")
    _run(bot, "/gift alice bandwidth 10")
    overrides = bot.members.get("u1")["overrides"]
    assert overrides["bandwidth_limit_kbps"] == 1000 + 10 * KBPS_PER_MBPS


def test_gift_invite_adds_quota(bot) -> None:
    _run(bot, "/gift alice invite 3")
    assert bot.members.get("u1")["invite_quota"] == 3


def test_gift_never_charges_points(bot) -> None:
    bot.points.add("u1", 100, "test", actor="test")
    _run(bot, "/gift alice traffic 10")
    assert bot.points.balance("u1") == 100


def test_gift_rejects_an_unknown_kind(bot) -> None:
    assert "类型必须是" in _run(bot, "/gift alice unicorns 5")


def test_gift_rejects_a_missing_amount(bot) -> None:
    assert "用法" in _run(bot, "/gift alice traffic")


# -- /code -------------------------------------------------------------------

def test_code_mints_the_requested_number_of_cards(bot) -> None:
    reply = _run(bot, "/code standard 30 3")
    assert "已生成 3 张卡密" in reply
    assert reply.count("<code>") == 3
    rows = bot.db.query("SELECT * FROM redeem_codes")
    assert len(rows) == 3
    assert all(r["days"] == 30 and r["group_id"] == "standard" for r in rows)


def test_code_rejects_an_unknown_group(bot) -> None:
    assert "用户组不存在" in _run(bot, "/code nosuchgroup 30 1")


def test_code_explains_the_syntax_when_arguments_are_missing(bot) -> None:
    assert "用法" in _run(bot, "/code standard")
    assert "用法" in _run(bot, "/code standard thirty 1")


# -- /invite -----------------------------------------------------------------

def test_invite_adds_slots(bot) -> None:
    reply = _run(bot, "/invite alice 3")
    assert "当前 3 个" in reply
    assert bot.members.get("u1")["invite_quota"] == 3


def test_invite_can_take_slots_away_but_not_below_zero(bot) -> None:
    _run(bot, "/invite alice 2")
    _run(bot, "/invite alice -5")
    assert bot.members.get("u1")["invite_quota"] == 0


def test_invite_needs_a_number(bot) -> None:
    assert "用法" in _run(bot, "/invite alice")


# -- /auth -------------------------------------------------------------------

def test_auth_pre_authorises_a_telegram_id(bot) -> None:
    reply = _run(bot, "/auth 123456")
    assert "已授权" in reply
    rows = bot.db.query("SELECT * FROM admin_grants WHERE tg_user_id='123456'")
    assert len(rows) == 1
    assert not rows[0]["used_at"]


def test_auth_rejects_a_non_numeric_id(bot) -> None:
    assert "❌" in _run(bot, "/auth notanid")


def test_auth_without_an_id_explains_the_syntax(bot) -> None:
    assert "用法" in _run(bot, "/auth")


# -- /req --------------------------------------------------------------------

def test_req_lists_outstanding_requests(bot) -> None:
    asyncio.run(bot.requests.create("u1", "movie", 550))
    reply = _run(bot, "/req")
    assert "#1" in reply and "待接单" in reply
    assert "alice" in reply


def test_req_filters_by_status(bot) -> None:
    first = asyncio.run(bot.requests.create("u1", "movie", 550))
    asyncio.run(bot.requests.create("u1", "movie", 551))
    bot.members.set_roles("admin1", ["admin", "uploader"], actor="test")
    bot.requests.claim(first["id"], "admin1")

    assert "#1" not in _run(bot, "/req open")
    claimed = _run(bot, "/req claimed")
    assert "#1" in claimed and "#2" not in claimed


def test_req_says_so_when_there_is_nothing(bot) -> None:
    assert "没有符合条件" in _run(bot, "/req")


def test_req_reports_the_totals(bot) -> None:
    asyncio.run(bot.requests.create("u1", "movie", 550))
    reply = _run(bot, "/req")
    assert "待接单 1" in reply and "本月 1" in reply


# -- audit -------------------------------------------------------------------

@pytest.mark.parametrize(("command", "action"), [
    ("/renew alice 30", "member.renew"),
    ("/score alice 10", "points.adjust"),
    ("/invite alice 2", "member.invite_quota"),
    ("/auth 123456", "registration.grant"),
    ("/code standard 30 1", "redeem.generate"),
    ("/gift alice traffic 5", "shop.grant"),
])
def test_commands_are_written_to_the_audit_trail_naming_the_admin(
        bot, command, action) -> None:
    _run(bot, command)
    rows = bot.members.audit_log(limit=50)
    hit = [r for r in rows if r["action"] == action]
    assert hit, f"{command} wrote no {action} audit row"
    assert "tg:rootadmin" in str(hit[0]["actor"])


def test_deleting_through_the_bot_is_audited(bot) -> None:
    _run(bot, "/rm alice")
    _tap(bot, "admin_ok")
    actions = {r["action"] for r in bot.members.audit_log(limit=50)}
    assert "member.delete" in actions
    assert "member.delete_emby" in actions
