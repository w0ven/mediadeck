"""The bot is the front door: it registers people and holds a bearer credential.

What these tests pin down:

- Registration creates a real Emby account from the chat. There is no code to
  copy, because the chat itself proves who is asking.
- The two cases that *cannot* be self-proven — claiming a pre-existing account,
  or moving one to a new Telegram id — go through approval instead.
- Every gate is re-checked at creation time, not only when the conversation
  started: a slot can fill while someone is still typing a username.
- The credential never travels back to a browser or into the audit trail.
"""
from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from app.main import app
from app.modules.settings import mask_secret
from app.modules.telegram import (
    USERNAME_RE, TelegramBot, generate_password, looks_like_credential,
)

# Shaped like a real credential so format validation is exercised. Assembled
# from parts so nothing in this file can be mistaken for a live one.
FAKE_CRED = "1234567" + ":" + "placeholder-not-a-real-credential"

ADMIN = ("admin", "change-me")


# -- the credential must not leak -------------------------------------------

def test_saved_credential_never_comes_back_to_the_browser() -> None:
    with TestClient(app) as client:
        saved = client.post("/api/settings/telegram", auth=ADMIN,
                            json={"bot_token": FAKE_CRED, "enabled": True}).json()
        assert saved["bot_token_set"] is True
        assert FAKE_CRED not in str(saved)
        assert saved["bot_token_masked"] == mask_secret(FAKE_CRED)

        fetched = client.get("/api/settings/telegram", auth=ADMIN).json()
        assert FAKE_CRED not in str(fetched)
        assert "bot_token" not in fetched


def test_audit_records_that_it_changed_not_what_it_changed_to() -> None:
    with TestClient(app) as client:
        client.post("/api/settings/telegram", auth=ADMIN,
                    json={"bot_token": FAKE_CRED, "enabled": True})
        body = str(client.get("/api/audit?limit=20", auth=ADMIN).json())
        assert "settings.telegram" in body
        assert FAKE_CRED not in body


def test_unretyped_credential_keeps_the_stored_one() -> None:
    """An edit form that only flips a checkbox must not wipe the credential."""
    with TestClient(app) as client:
        client.post("/api/settings/telegram", auth=ADMIN,
                    json={"bot_token": FAKE_CRED, "enabled": True})
        after = client.post("/api/settings/telegram", auth=ADMIN,
                            json={"register_days": 5}).json()
        assert after["bot_token_set"] is True
        assert after["register_days"] == 5


def test_enabling_without_a_credential_is_refused() -> None:
    with TestClient(app) as client:
        client.post("/api/settings/telegram", auth=ADMIN,
                    json={"bot_token": "", "enabled": False})
        assert client.post("/api/settings/telegram", auth=ADMIN,
                           json={"enabled": True, "bot_token": ""}).status_code >= 400


def test_registration_cannot_be_opened_on_a_stopped_bot() -> None:
    """Advertising a door nobody can walk through would just confuse members."""
    with TestClient(app) as client:
        client.post("/api/settings/telegram", auth=ADMIN,
                    json={"bot_token": "", "enabled": False,
                          "allow_admin_grant": False, "allow_invite": False,
                          "allow_redeem": False})
        r = client.post("/api/settings/telegram", auth=ADMIN,
                        json={"enabled": False, "allow_invite": True})
        assert r.status_code >= 400


def test_the_old_single_registration_switch_is_gone() -> None:
    """One switch could only be open to everyone or closed to everyone.

    It is replaced by three channel switches; leaving the old key in the
    payload would let a stale UI reopen a door the operator shut.
    """
    with TestClient(app) as client:
        cfg = client.get("/api/settings/telegram", auth=ADMIN).json()
        assert "registration_enabled" not in cfg
        assert {"allow_admin_grant", "allow_invite", "allow_redeem"} <= set(cfg)


def test_settings_bounds_are_enforced() -> None:
    with TestClient(app) as client:
        client.post("/api/settings/telegram", auth=ADMIN,
                    json={"bot_token": FAKE_CRED, "enabled": True})
        for bad in ({"register_days": -1}, {"register_days": 99999},
                    {"max_users": -5}, {"register_days": "abc"}):
            assert client.post("/api/settings/telegram", auth=ADMIN,
                               json=bad).status_code >= 400, bad


def test_scheduling_settings_moved_to_the_plugin_cards() -> None:
    """The ranking post and expiry reminder are plugins now.

    Two switches for one post is how it ends up going out twice, so the old
    keys must be gone from the Telegram payload rather than merely ignored.
    """
    with TestClient(app) as client:
        cfg = client.get("/api/settings/telegram", auth=ADMIN).json()
        for gone in ("rankings_enabled", "rankings_hour", "rankings_chat",
                     "notify_expiring", "notify_expiring_days"):
            assert gone not in cfg
        ids = {c["id"] for c in client.get("/api/plugins", auth=ADMIN).json()}
        assert {"rankings_post", "expiry_reminder"} <= ids


def test_telegram_endpoints_require_auth() -> None:
    with TestClient(app) as client:
        assert client.get("/api/settings/telegram").status_code == 401
        assert client.get("/api/telegram/requests").status_code == 401
        assert client.post("/api/telegram/group-audit").status_code == 401


# -- fakes ------------------------------------------------------------------

class _FakeEmby:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.created: list[str] = []
        self.passwords: list[tuple[str, str]] = []

    async def create_user(self, name: str):
        if self.fail:
            return None
        self.created.append(name)
        return {"Id": f"emby-{name}", "Name": name}

    async def set_user_password(self, user_id: str, new_password: str) -> bool:
        self.passwords.append((user_id, new_password))
        return True


class _FakeMembers:
    def __init__(self, linked: dict | None = None) -> None:
        self._linked = linked or {}
        self.upserted: list[tuple] = []
        self.bound: list[tuple] = []
        self._by_name: dict[str, dict] = {}

    def find_by_telegram(self, tg_user_id: str):
        return self._linked.get(str(tg_user_id))

    def find_by_username(self, username: str):
        return self._by_name.get(str(username).lower())

    def get(self, user_id: str):
        for row in self._linked.values():
            if str(row.get("emby_user_id")) == str(user_id):
                return row
        return None

    def upsert(self, user_id, username, payload, actor="system"):
        self.upserted.append((user_id, username, payload))
        row = {"emby_user_id": user_id, "username": username,
               "status": "active", "expires_at": payload.get("expires_at")}
        self._by_name[username.lower()] = row
        return row

    def bind_telegram(self, user_id, tg_user_id, tg_username="", actor="operator"):
        self.bound.append((user_id, tg_user_id, tg_username))
        self._linked[str(tg_user_id)] = {
            "emby_user_id": user_id, "username": "someone",
            "status": "active", "expires_at": int(time.time()) + 86400,
        }
        return self._linked[str(tg_user_id)]

    def devices(self, user_id):
        return []

    def list(self, **kw):
        return list(self._linked.values())


def _bot(members=None, emby=None, cfg=None, db=None) -> TelegramBot:
    base = {"enabled": True, "bot_token": FAKE_CRED,
            "allow_admin_grant": True, "allow_invite": True,
            "allow_redeem": True, "register_days": 30,
            "max_users": 0, "require_group": "", "default_group_id": "",
            "emby_public_url": "https://emby.example"}
    base.update(cfg or {})
    bot = TelegramBot(lambda: base, members or _FakeMembers(),
                      emby=emby, db=db)
    bot.sent = []  # type: ignore[attr-defined]

    async def fake_send(chat, text, keyboard=None):
        bot.sent.append(text)  # type: ignore[attr-defined]
        return True

    bot.send = fake_send  # type: ignore[assignment]
    return bot


# -- menus differ by audience -----------------------------------------------

def test_guest_and_member_see_different_menus() -> None:
    member = {"emby_user_id": "u1", "username": "someone", "status": "active",
              "expires_at": int(time.time()) + 86400}
    bot = _bot(_FakeMembers({"999": member}))

    guest_body, guest_keys = bot._home("111", "Stranger")
    member_body, member_keys = bot._home("999", "Friend")

    guest_actions = {b.get("callback_data") for row in guest_keys for b in row}
    guest_actions.discard(None)
    member_actions = {b.get("callback_data") for row in member_keys for b in row}
    member_actions.discard(None)

    # A guest can only register or claim; member-only views are absent.
    assert "register" in guest_actions
    assert "claim" in guest_actions
    assert "devices" not in guest_actions

    # A member is past that step and must not be offered it again.
    assert "register" not in member_actions
    # The member menu is two levels: the top offers identity and backpack, and
    # the per-account views hang off 「我的信息」 rather than crowding the root.
    assert {"me", "bag", "top"} <= member_actions
    assert "home" not in member_actions
    info_actions = {b["callback_data"] for row in bot.info_menu() for b in row}
    assert {"devices", "usage", "me_points", "me_nodes", "resetpw"} <= info_actions

    assert "没有账号" in guest_body
    assert "someone" in member_body


def test_menu_follows_state_after_registering() -> None:
    members = _FakeMembers()
    bot = _bot(members)
    assert "register" in {b["callback_data"] for r in bot._home("42", "X")[1] for b in r}
    members.bind_telegram("u9", "42", "x")
    assert "register" not in {b["callback_data"] for r in bot._home("42", "X")[1] for b in r}


# -- registration -----------------------------------------------------------

def test_registration_creates_the_account_and_links_the_chat() -> None:
    members, emby = _FakeMembers(), _FakeEmby()
    bot = _bot(members, emby)
    asyncio.run(bot._finish_registration(1, "42", "tguser", "newmember"))

    assert emby.created == ["newmember"]
    assert members.upserted and members.upserted[0][1] == "newmember"
    # The Telegram id is the identity, recorded as owner at creation time.
    assert members.bound == [("emby-newmember", "42", "tguser")]


def test_the_generated_password_is_shown_once_and_not_chosen() -> None:
    emby = _FakeEmby()
    bot = _bot(_FakeMembers(), emby)
    asyncio.run(bot._finish_registration(1, "42", "u", "newmember"))

    assert emby.passwords, "a password should have been set"
    issued = emby.passwords[0][1]
    assert len(issued) >= 12
    assert any(issued in msg for msg in bot.sent)  # type: ignore[attr-defined]
    assert any("不会再发第二次" in msg for msg in bot.sent)  # type: ignore[attr-defined]


def test_bad_usernames_are_refused_before_touching_emby() -> None:
    for bad in ("ab", "1startsdigit", "has space", "sym!bol", "x" * 21, ""):
        assert not USERNAME_RE.match(bad), bad
        emby = _FakeEmby()
        bot = _bot(_FakeMembers(), emby)
        asyncio.run(bot._finish_registration(1, "42", "u", bad))
        assert emby.created == [], f"{bad!r} reached Emby"


def test_a_taken_username_fails_without_creating_a_member() -> None:
    members, emby = _FakeMembers(), _FakeEmby(fail=True)
    bot = _bot(members, emby)
    asyncio.run(bot._finish_registration(1, "42", "u", "duplicate"))
    assert members.upserted == []
    assert members.bound == []
    assert any("创建失败" in m for m in bot.sent)  # type: ignore[attr-defined]


def test_registration_closed_is_refused() -> None:
    """Every channel shut is a shut door, and it has to say so."""
    emby = _FakeEmby()
    bot = _bot(_FakeMembers(), emby,
               cfg={"allow_admin_grant": False, "allow_invite": False,
                    "allow_redeem": False})
    asyncio.run(bot._start_registration(1, "42"))
    assert emby.created == []
    assert any("暂停注册" in m for m in bot.sent)  # type: ignore[attr-defined]


def test_one_open_channel_keeps_registration_reachable() -> None:
    """The inverse: shutting two channels must not close the third."""
    emby = _FakeEmby()
    bot = _bot(_FakeMembers(), emby,
               cfg={"allow_admin_grant": False, "allow_invite": False,
                    "allow_redeem": True})
    asyncio.run(bot._start_registration(1, "42"))
    assert not any("暂停注册" in m for m in bot.sent)  # type: ignore[attr-defined]


def test_a_full_slot_cap_blocks_registration() -> None:
    class _Db:
        def one(self, sql, params=()):
            return {"n": 10}

    emby = _FakeEmby()
    bot = _bot(_FakeMembers(), emby, cfg={"max_users": 10}, db=_Db())
    asyncio.run(bot._start_registration(1, "42"))
    assert emby.created == []
    assert any("名额已满" in m for m in bot.sent)  # type: ignore[attr-defined]


def test_the_cap_is_rechecked_at_creation_not_only_at_the_start() -> None:
    """A slot can fill while someone is still typing their username."""
    class _Db:
        def one(self, sql, params=()):
            return {"n": 5}

    emby = _FakeEmby()
    bot = _bot(_FakeMembers(), emby, cfg={"max_users": 5}, db=_Db())
    asyncio.run(bot._finish_registration(1, "42", "u", "toolate"))
    assert emby.created == [], "the account was created after the cap was reached"


def test_an_existing_member_cannot_register_twice() -> None:
    members = _FakeMembers({"42": {"emby_user_id": "u1", "username": "a",
                                   "status": "active", "expires_at": None}})
    emby = _FakeEmby()
    bot = _bot(members, emby)
    asyncio.run(bot._start_registration(1, "42"))
    assert emby.created == []


def test_register_days_zero_means_no_expiry() -> None:
    members = _FakeMembers()
    bot = _bot(members, _FakeEmby(), cfg={"register_days": 0})
    asyncio.run(bot._finish_registration(1, "42", "u", "forever"))
    assert "expires_at" not in members.upserted[0][2]


# -- group requirement ------------------------------------------------------

def test_no_group_configured_lets_everyone_in() -> None:
    bot = _bot(cfg={"require_group": ""})
    allowed, _ = asyncio.run(bot.in_required_group("42"))
    assert allowed is True


def test_a_failed_group_lookup_does_not_close_registration() -> None:
    """Telegram being unreachable must not lock out every new member."""
    bot = _bot(cfg={"require_group": "@somegroup"})

    async def fail(*a, **k):
        return None

    bot._call = fail  # type: ignore[assignment]
    allowed, reason = asyncio.run(bot.in_required_group("42"))
    assert allowed is True
    assert reason == "group-check-unavailable"


def test_a_user_who_left_the_group_is_refused() -> None:
    bot = _bot(cfg={"require_group": "@somegroup"})

    async def left(*a, **k):
        return {"status": "left"}

    bot._call = left  # type: ignore[assignment]
    allowed, _ = asyncio.run(bot.in_required_group("42"))
    assert allowed is False


def test_group_members_of_every_rank_are_accepted() -> None:
    for status in ("creator", "administrator", "member", "restricted"):
        bot = _bot(cfg={"require_group": "@g"})

        async def ok(*a, _s=status, **k):
            return {"status": _s}

        bot._call = ok  # type: ignore[assignment]
        assert asyncio.run(bot.in_required_group("42"))[0] is True, status


# -- claim / rebind approval ------------------------------------------------

class _ReqDb:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._next = 1

    def one(self, sql, params=()):
        if "tg_requests WHERE id" in sql:
            return next((r for r in self.rows if r["id"] == params[0]), None)
        if "tg_requests WHERE tg_user_id" in sql:
            return next(({"x": 1} for r in self.rows
                         if r["tg_user_id"] == params[0] and r["status"] == "pending"),
                        None)
        return {"n": 0}

    def query(self, sql, params=()):
        return [r for r in self.rows if r["status"] == "pending"]

    def execute(self, sql, params=()):
        if sql.startswith("INSERT INTO tg_requests"):
            self.rows.append({
                "id": self._next, "kind": params[0], "tg_user_id": params[1],
                "tg_username": params[2], "wanted_username": params[3],
                "status": "pending", "created_at": params[4],
            })
            self._next += 1
        elif sql.startswith("UPDATE tg_requests"):
            rid = params[-1]
            for r in self.rows:
                if r["id"] == rid:
                    r["status"] = params[0]


def test_a_claim_becomes_a_pending_request_not_an_instant_link() -> None:
    members, db = _FakeMembers(), _ReqDb()
    bot = _bot(members, db=db)
    assert bot._create_request("bind", "42", "tguser", "oldaccount") is True
    assert members.bound == [], "claiming must not link before review"
    assert len(bot.pending_requests()) == 1


def test_a_second_request_from_one_chat_is_refused() -> None:
    bot = _bot(db=_ReqDb())
    assert bot._create_request("bind", "42", "u", "acct") is True
    assert bot._create_request("bind", "42", "u", "other") is False


def test_approving_links_the_account() -> None:
    members, db = _FakeMembers(), _ReqDb()
    members.upsert("emby-old", "oldaccount", {})
    bot = _bot(members, db=db)
    bot._create_request("bind", "42", "tguser", "oldaccount")

    result = bot.review_request(1, approve=True, reviewer="admin")
    assert result["approved"] is True
    assert members.bound == [("emby-old", "42", "tguser")]


def test_rejecting_links_nothing() -> None:
    members, db = _FakeMembers(), _ReqDb()
    members.upsert("emby-old", "oldaccount", {})
    bot = _bot(members, db=db)
    bot._create_request("bind", "42", "tguser", "oldaccount")

    bot.review_request(1, approve=False, reviewer="admin")
    assert members.bound == []


def test_approving_a_claim_for_an_unknown_account_fails_loudly() -> None:
    bot = _bot(_FakeMembers(), db=_ReqDb())
    bot._create_request("bind", "42", "u", "does-not-exist")
    try:
        bot.review_request(1, approve=True)
    except ValueError:
        return
    raise AssertionError("approving a claim on a missing account should fail")


def test_a_request_cannot_be_reviewed_twice() -> None:
    members, db = _FakeMembers(), _ReqDb()
    members.upsert("emby-old", "acct", {})
    bot = _bot(members, db=db)
    bot._create_request("bind", "42", "u", "acct")
    bot.review_request(1, approve=True)
    try:
        bot.review_request(1, approve=True)
    except KeyError:
        return
    raise AssertionError("a settled request should not be reviewable again")


# -- rankings ---------------------------------------------------------------

class _FakeStats:
    def top_users(self, days=30, limit=20):
        return [{"username": "alice", "hours": 12.5, "plays": 30, "bytes": 1},
                {"username": "bob", "hours": 8.0, "plays": 12, "bytes": 1}]

    def top_titles(self, days=30, limit=20):
        return [{"title": "Some Show", "plays": 40, "hours": 20.0,
                 "viewers": 5, "type": "Series"}]


def test_rankings_list_both_viewers_and_titles() -> None:
    bot = TelegramBot(lambda: {"enabled": False, "bot_token": ""},
                      _FakeMembers(), stats=_FakeStats())
    text = bot._rankings_text(1)
    assert "alice" in text and "12.5" in text
    assert "Some Show" in text and "40" in text


def test_rankings_say_so_when_there_is_nothing_yet() -> None:
    class _Empty:
        def top_users(self, **k):
            return []

        def top_titles(self, **k):
            return []

    bot = TelegramBot(lambda: {"enabled": False, "bot_token": ""},
                      _FakeMembers(), stats=_Empty())
    assert "还没有排行数据" in bot._rankings_text(1)


def test_a_broken_stats_source_does_not_crash_the_bot() -> None:
    class _Broken:
        def top_users(self, **k):
            raise RuntimeError("db down")

        def top_titles(self, **k):
            raise RuntimeError("db down")

    bot = TelegramBot(lambda: {"enabled": False, "bot_token": ""},
                      _FakeMembers(), stats=_Broken())
    assert "还没有排行数据" in bot._rankings_text(1)


# -- disabled bot behaves ---------------------------------------------------

def test_a_bot_without_a_credential_reports_disabled_and_does_not_call_out() -> None:
    bot = TelegramBot(lambda: {"enabled": False, "bot_token": ""}, _FakeMembers())
    assert bot.enabled is False
    assert bot.status()["running"] is False
    assert asyncio.run(bot._call("getMe")) is None


def test_generated_passwords_differ() -> None:
    assert generate_password() != generate_password()


# -- registration channels in the conversation ------------------------------
# The ordering contract: resolve() decides, the account is created, and only
# then is the credential spent. Reversed, a member whose chosen username turns
# out to be taken loses the card they paid for and gets no account.

class _FakeRegistration:
    """Records what was resolved and what was consumed, in order."""

    def __init__(self, verdict=None) -> None:
        self.verdict = verdict
        self.resolved: list[tuple] = []
        self.consumed: list[tuple] = []
        self.quota = 0
        self.minted: list[str] = []

    def resolve(self, tg_user_id, credential=None):
        self.resolved.append((str(tg_user_id), credential))
        if self.verdict is None:
            return _Verdict(allowed=False, reason="没有可用的凭证")
        return self.verdict

    def consume(self, admission, new_user_id):
        self.consumed.append((getattr(admission, "via", ""), new_user_id))
        return True

    def invite_quota(self, user_id):
        return self.quota

    def list_invites(self, user_id, limit=10):
        return [{"code": c, "uses_left": 1, "expires_at": None,
                 "revoked": 0, "usable": True} for c in self.minted]

    def spend_quota_for_invite(self, user_id, ttl_days=0):
        if self.quota <= 0:
            raise ValueError("你没有可用的邀请名额。")
        self.quota -= 1
        made = "INVITE" + str(len(self.minted) + 1)
        self.minted.append(made)
        return {"code": made}


class _Verdict:
    def __init__(self, allowed=True, via="invite", reason="", group_id="",
                 days=30, inviter_id="", credential="") -> None:
        self.allowed = allowed
        self.via = via
        self.reason = reason or ("邀请码有效" if allowed else "无效")
        self.group_id = group_id
        self.days = days
        self.inviter_id = inviter_id
        self.credential = credential
        self.tg_user_id = ""


def test_registration_asks_for_a_credential_before_a_username() -> None:
    members, emby = _FakeMembers(), _FakeEmby()
    reg = _FakeRegistration()
    bot = _bot(members, emby)
    bot._registration = reg

    asyncio.run(bot._start_registration(1, "42"))

    assert emby.created == []
    assert any("邀请码" in m for m in bot.sent)  # type: ignore[attr-defined]
    assert bot._pending["1"][0] == "credential"


def test_a_pre_authorised_chat_skips_straight_to_the_username() -> None:
    """Asking someone the operator already named for a code is a dead end."""
    members, emby = _FakeMembers(), _FakeEmby()
    reg = _FakeRegistration(_Verdict(via="admin"))
    bot = _bot(members, emby)
    bot._registration = reg

    asyncio.run(bot._start_registration(1, "42"))

    assert bot._pending["1"][0] == "username"
    assert any("用户名" in m for m in bot.sent)  # type: ignore[attr-defined]


def test_a_bad_credential_keeps_the_conversation_open() -> None:
    """A mistyped code should cost one message, not the whole flow."""
    members, emby = _FakeMembers(), _FakeEmby()
    reg = _FakeRegistration(_Verdict(allowed=False, reason="这个邀请码已被作废。"))
    bot = _bot(members, emby)
    bot._registration = reg
    bot._pending["1"] = ("credential", time.time() + 600, {})

    asyncio.run(bot._submit_credential(1, "42", "BADCODE1"))

    assert emby.created == []
    assert reg.consumed == []
    assert any("已被作废" in m for m in bot.sent)  # type: ignore[attr-defined]
    assert bot._pending["1"][0] == "credential"


def test_a_good_credential_advances_to_the_username_without_spending_it() -> None:
    members, emby = _FakeMembers(), _FakeEmby()
    reg = _FakeRegistration(_Verdict(credential="GOODCODE"))
    bot = _bot(members, emby)
    bot._registration = reg

    asyncio.run(bot._submit_credential(1, "42", "GOODCODE"))

    assert bot._pending["1"][0] == "username"
    # Nothing is spent yet: the account does not exist.
    assert reg.consumed == []


def test_the_credential_is_spent_only_after_the_account_exists() -> None:
    members, emby = _FakeMembers(), _FakeEmby()
    reg = _FakeRegistration()
    admission = _Verdict(via="redeem", credential="CARD", days=90,
                         group_id="vip")
    bot = _bot(members, emby)
    bot._registration = reg

    asyncio.run(bot._finish_registration(1, "42", "tguser", "newmember",
                                        admission=admission))

    assert emby.created == ["newmember"]
    assert reg.consumed == [("redeem", "emby-newmember")]
    payload = members.upserted[0][2]
    assert payload["register_via"] == "redeem"
    assert payload["group_id"] == "vip"
    assert payload["register_at"]


def test_a_failed_creation_does_not_spend_the_credential() -> None:
    """The whole point of the ordering: a taken username must cost nothing."""
    members, emby = _FakeMembers(), _FakeEmby(fail=True)
    reg = _FakeRegistration()
    bot = _bot(members, emby)
    bot._registration = reg

    asyncio.run(bot._finish_registration(
        1, "42", "tguser", "duplicate",
        admission=_Verdict(via="redeem", credential="CARD")))

    assert members.upserted == []
    assert reg.consumed == []
    assert any("创建失败" in m for m in bot.sent)  # type: ignore[attr-defined]


def test_a_rejected_username_does_not_spend_the_credential() -> None:
    members, emby = _FakeMembers(), _FakeEmby()
    reg = _FakeRegistration()
    bot = _bot(members, emby)
    bot._registration = reg

    asyncio.run(bot._finish_registration(
        1, "42", "tguser", "no",
        admission=_Verdict(via="invite", credential="INV")))

    assert emby.created == []
    assert reg.consumed == []


def test_an_invite_records_who_vouched_for_the_new_member() -> None:
    members, emby = _FakeMembers(), _FakeEmby()
    reg = _FakeRegistration()
    bot = _bot(members, emby)
    bot._registration = reg

    asyncio.run(bot._finish_registration(
        1, "42", "tguser", "newmember",
        admission=_Verdict(via="invite", credential="INV",
                           inviter_id="emby-owner")))

    payload = members.upserted[0][2]
    assert payload["inviter_id"] == "emby-owner"
    assert payload["register_via"] == "invite"


def test_members_see_their_invite_codes_and_remaining_slots() -> None:
    member = {"emby_user_id": "u1", "username": "someone", "status": "active",
              "expires_at": int(time.time()) + 86400}
    reg = _FakeRegistration()
    reg.quota = 2
    bot = _bot(_FakeMembers({"999": member}))
    bot._registration = reg
    edits: list[str] = []

    async def fake_edit(chat, mid, text, keyboard=None):
        edits.append(text)

    bot._edit = fake_edit  # type: ignore[assignment]
    asyncio.run(bot._invites_view(1, 2, member))

    assert any("剩余名额" in t and "2" in t for t in edits)


def test_minting_an_invite_debits_a_slot_and_shows_the_code() -> None:
    member = {"emby_user_id": "u1", "username": "someone", "status": "active",
              "expires_at": int(time.time()) + 86400}
    reg = _FakeRegistration()
    reg.quota = 1
    bot = _bot(_FakeMembers({"999": member}))
    bot._registration = reg
    edits: list[str] = []

    async def fake_edit(chat, mid, text, keyboard=None):
        edits.append(text)

    bot._edit = fake_edit  # type: ignore[assignment]
    asyncio.run(bot._invites_view(1, 2, member, mint=True))

    assert reg.quota == 0
    assert reg.minted == ["INVITE1"]
    assert any("INVITE1" in t for t in edits)


def test_a_member_with_no_slots_is_told_rather_than_ignored() -> None:
    member = {"emby_user_id": "u1", "username": "someone", "status": "active",
              "expires_at": int(time.time()) + 86400}
    reg = _FakeRegistration()
    reg.quota = 0
    bot = _bot(_FakeMembers({"999": member}))
    bot._registration = reg
    edits: list[str] = []

    async def fake_edit(chat, mid, text, keyboard=None):
        edits.append(text)

    bot._edit = fake_edit  # type: ignore[assignment]
    asyncio.run(bot._invites_view(1, 2, member, mint=True))

    assert reg.minted == []
    assert any("名额" in t for t in edits)


def test_a_broken_registration_service_does_not_crash_the_flow() -> None:
    class _Broken:
        def resolve(self, *a, **k):
            raise RuntimeError("db down")

    members, emby = _FakeMembers(), _FakeEmby()
    bot = _bot(members, emby)
    bot._registration = _Broken()

    asyncio.run(bot._submit_credential(1, "42", "ANY"))

    assert emby.created == []
    assert any("暂时不可用" in m for m in bot.sent)  # type: ignore[attr-defined]


# -- points in the bot -------------------------------------------------------
# The menu is the product here: a member who cannot find the button does not
# have the feature. These pin the structure and the two multi-step flows.

class _FakeRegistry:
    """Just the two questions the bot asks a registry."""

    def __init__(self, enabled: set[str] | None = None,
                 plugins: dict | None = None) -> None:
        self._enabled = enabled or set()
        self._plugins = plugins or {}

    def enabled(self, plugin_id: str) -> bool:
        return plugin_id in self._enabled

    def get(self, plugin_id: str):
        return self._plugins.get(plugin_id)


class _FakePoints:
    def __init__(self, balances: dict | None = None) -> None:
        self.balances = balances or {}
        self.transfers: list[tuple] = []

    def balance(self, user_id: str) -> int:
        return int(self.balances.get(str(user_id), 0))

    def ledger(self, user_id: str, limit: int = 20):
        return [{"delta": 10, "reason": "checkin", "reason_label": "每日签到",
                 "created_at": int(time.time())}]

    def top(self, limit: int = 5):
        return [{"username": "alice", "balance": 300}]


class _FakeTransferPlugin:
    def __init__(self, ok: bool = True, reason: str = "", fee: int = 0) -> None:
        self.ok, self.reason, self._fee = ok, reason, fee
        self.calls: list[tuple] = []

    def can_transfer(self, from_id: str, amount):
        return (self.ok, self.reason)

    def fee_for(self, amount: int) -> int:
        return self._fee

    def transfer(self, from_id: str, to_id: str, amount: int):
        self.calls.append((from_id, to_id, amount))
        return {"amount": amount, "fee": self._fee,
                "received": amount - self._fee,
                "from_balance": 100, "to_balance": 200}


class _FakeCheckinPlugin:
    def __init__(self, result: dict | None = None) -> None:
        self.result = result or {"ok": True, "points": 15, "bonus": 5,
                                 "streak": 2, "balance": 115}
        self.calls: list[str] = []

    def checkin(self, user_id: str):
        self.calls.append(user_id)
        return self.result


def _points_bot(members=None, *, enabled=None, plugins=None, points=None,
                shop=None, scheduler=None):
    bot = _bot(members or _FakeMembers())
    bot._plugins = _FakeRegistry(enabled or set(), plugins or {})
    bot._points = points or _FakePoints()
    bot._shop = shop
    bot._scheduler = scheduler
    bot.edits = []  # type: ignore[attr-defined]

    async def fake_edit(chat, mid, text, keyboard=None):
        bot.edits.append(text)  # type: ignore[attr-defined]
        return True

    bot._edit = fake_edit  # type: ignore[assignment]
    return bot


def _actions(keyboard) -> set:
    return {b["callback_data"] for row in keyboard for b in row if "callback_data" in b}


def test_the_main_menu_is_two_levels_not_one_long_list() -> None:
    """Identity and backpack are the entry points; details hang off them."""
    bot = _points_bot(enabled={"checkin", "points_transfer"})
    rows = bot.member_menu()

    assert _actions(rows) == {"me", "bag", "checkin", "transfer", "req_new",
                              "top"}
    # Two buttons per row keeps the keyboard readable on a phone.
    assert all(len(row) <= 2 for row in rows)
    assert _actions(bot.info_menu()) == {
        "me_status", "me_points", "me_nodes", "devices", "usage", "resetpw",
        "my_requests", "home"}
    assert _actions(bot.bag_menu()) == {"invites", "shop", "orders", "home"}


def test_the_backpack_holds_invites_shop_and_history() -> None:
    bot = _points_bot()
    labels = [b["text"] for row in bot.bag_menu() for b in row]
    assert any("邀请码" in t for t in labels)
    assert any("兑换商城" in t for t in labels)
    assert any("兑换记录" in t for t in labels)


def test_checking_in_from_the_bot_reports_points_and_streak() -> None:
    plugin = _FakeCheckinPlugin()
    bot = _points_bot(enabled={"checkin"}, plugins={"checkin": plugin})
    member = {"emby_user_id": "u1", "username": "alice"}

    asyncio.run(bot._checkin(1, 2, member))

    assert plugin.calls == ["u1"]
    text = bot.edits[0]  # type: ignore[attr-defined]
    assert "签到成功" in text and "+15" in text
    assert "2" in text and "115" in text


def test_a_second_checkin_says_so_instead_of_paying_again() -> None:
    plugin = _FakeCheckinPlugin({"ok": False, "reason": "今天已签到",
                                 "balance": 115})
    bot = _points_bot(enabled={"checkin"}, plugins={"checkin": plugin})

    asyncio.run(bot._checkin(1, 2, {"emby_user_id": "u1"}))
    assert "今天已签到" in bot.edits[0]  # type: ignore[attr-defined]


def test_checkin_is_refused_when_the_plugin_is_off() -> None:
    plugin = _FakeCheckinPlugin()
    bot = _points_bot(plugins={"checkin": plugin})  # registered but disabled
    asyncio.run(bot._checkin(1, 2, {"emby_user_id": "u1"}))
    assert plugin.calls == []
    assert "未开启" in bot.edits[0]  # type: ignore[attr-defined]


def test_transfer_asks_for_a_name_then_an_amount_then_confirmation() -> None:
    members = _FakeMembers({"999": {"emby_user_id": "u1", "username": "alice"}})
    members._by_name["bob"] = {"emby_user_id": "u2", "username": "bob"}
    plugin = _FakeTransferPlugin()
    bot = _points_bot(members, enabled={"points_transfer"},
                      plugins={"points_transfer": plugin},
                      points=_FakePoints({"u1": 500}))
    member = members._linked["999"]

    asyncio.run(bot._transfer_start(1, 2))
    assert bot._pending["1"][0] == "transfer_to"

    asyncio.run(bot._transfer_pick_target(1, member, "bob"))
    kind, _, extra = bot._pending["1"]
    assert kind == "transfer_amount" and extra["to_id"] == "u2"

    asyncio.run(bot._transfer_pick_amount(1, member, extra, "50"))
    kind, _, extra = bot._pending["1"]
    assert kind == "transfer_confirm" and extra["amount"] == 50
    # Nothing has moved yet: the last step is a deliberate confirmation.
    assert plugin.calls == []

    asyncio.run(bot._transfer_execute(1, 2, member))
    assert plugin.calls == [("u1", "u2", 50)]
    assert "转账成功" in bot.edits[-1]  # type: ignore[attr-defined]
    assert "1" not in bot._pending


def test_an_unknown_recipient_ends_the_conversation_with_a_reason() -> None:
    members = _FakeMembers({"999": {"emby_user_id": "u1", "username": "alice"}})
    bot = _points_bot(members, enabled={"points_transfer"},
                      plugins={"points_transfer": _FakeTransferPlugin()})

    asyncio.run(bot._transfer_pick_target(1, members._linked["999"], "nobody"))
    assert "找不到用户" in bot.sent[-1]  # type: ignore[attr-defined]
    assert "1" not in bot._pending


def test_transferring_to_yourself_is_stopped_before_any_amount_is_asked() -> None:
    members = _FakeMembers({"999": {"emby_user_id": "u1", "username": "alice"}})
    members._by_name["alice"] = {"emby_user_id": "u1", "username": "alice"}
    bot = _points_bot(members, enabled={"points_transfer"},
                      plugins={"points_transfer": _FakeTransferPlugin()})

    asyncio.run(bot._transfer_pick_target(1, members._linked["999"], "alice"))
    assert "不能转给自己" in bot.sent[-1]  # type: ignore[attr-defined]
    assert "1" not in bot._pending


def test_a_refused_amount_does_not_reach_confirmation() -> None:
    members = _FakeMembers({"999": {"emby_user_id": "u1", "username": "alice"}})
    plugin = _FakeTransferPlugin(ok=False, reason="积分不足，当前余额 10")
    bot = _points_bot(members, enabled={"points_transfer"},
                      plugins={"points_transfer": plugin})

    asyncio.run(bot._transfer_pick_amount(
        1, members._linked["999"], {"to_id": "u2", "to_name": "bob"}, "500"))

    assert "积分不足" in bot.sent[-1]  # type: ignore[attr-defined]
    assert "1" not in bot._pending
    assert plugin.calls == []


def test_a_non_numeric_amount_re_asks_rather_than_giving_up() -> None:
    members = _FakeMembers({"999": {"emby_user_id": "u1", "username": "alice"}})
    bot = _points_bot(members, enabled={"points_transfer"},
                      plugins={"points_transfer": _FakeTransferPlugin()})
    bot._pending["1"] = ("transfer_amount", time.time() + 600,
                         {"to_id": "u2", "to_name": "bob"})

    asyncio.run(bot._transfer_pick_amount(
        1, members._linked["999"], {"to_id": "u2", "to_name": "bob"}, "五十"))

    assert "正整数" in bot.sent[-1]  # type: ignore[attr-defined]
    assert bot._pending["1"][0] == "transfer_amount"


def test_a_confirmation_that_expired_does_not_transfer() -> None:
    members = _FakeMembers({"999": {"emby_user_id": "u1", "username": "alice"}})
    plugin = _FakeTransferPlugin()
    bot = _points_bot(members, enabled={"points_transfer"},
                      plugins={"points_transfer": plugin})

    asyncio.run(bot._transfer_execute(1, 2, members._linked["999"]))
    assert plugin.calls == []
    assert "取消或超时" in bot.edits[-1]  # type: ignore[attr-defined]


def test_the_recipient_is_told_they_received_points() -> None:
    """A balance that silently changes is indistinguishable from a bug."""
    members = _FakeMembers({"999": {"emby_user_id": "u1", "username": "alice"}})
    members._linked["888"] = {"emby_user_id": "u2", "username": "bob",
                              "tg_user_id": "888"}
    plugin = _FakeTransferPlugin()
    bot = _points_bot(members, enabled={"points_transfer"},
                      plugins={"points_transfer": plugin})
    bot._pending["1"] = ("transfer_confirm", time.time() + 600,
                         {"to_id": "u2", "to_name": "bob", "amount": 50})

    asyncio.run(bot._transfer_execute(1, 2, members._linked["999"]))

    assert plugin.calls == [("u1", "u2", 50)]
    assert any("收到" in m and "alice" in m
               for m in bot.sent)  # type: ignore[attr-defined]


class _FakeShop:
    def __init__(self, items=None, fail: str = "") -> None:
        self._items = items or []
        self.fail = fail
        self.redeemed: list[tuple] = []

    def items(self, enabled_only: bool = False):
        return [i for i in self._items if i["enabled"]] if enabled_only \
            else list(self._items)

    def get(self, item_id: int):
        return next((i for i in self._items if i["id"] == int(item_id)), None)

    def redeem(self, user_id: str, item_id: int, actor: str = "member"):
        if self.fail:
            raise ValueError(self.fail)
        self.redeemed.append((user_id, int(item_id)))
        item = self.get(item_id)
        return {"ok": True, "item": item, "cost": item["cost"],
                "balance": 400, "granted": "+50GB 流量"}

    def orders(self, user_id=None, limit: int = 10):
        return [{"item_name": "流量包", "cost": 100,
                 "created_at": int(time.time())}]


def _shop_item(**kw):
    base = {"id": 1, "name": "流量包 50GB", "cost": 100, "amount": 50,
            "enabled": True, "unit": "GB", "kind_label": "流量包",
            "description": "增加 50GB"}
    base.update(kw)
    return base


def test_the_shop_lists_only_items_that_are_on_sale() -> None:
    shop = _FakeShop([_shop_item(), _shop_item(id=2, name="隐藏商品",
                                               enabled=False)])
    bot = _points_bot(shop=shop, points=_FakePoints({"u1": 500}))

    asyncio.run(bot._shop_view(1, 2, {"emby_user_id": "u1"}))
    text = bot.edits[0]  # type: ignore[attr-defined]
    assert "流量包 50GB" in text and "隐藏商品" not in text
    assert "500" in text  # the balance, so the price means something


def test_buying_asks_before_it_spends() -> None:
    shop = _FakeShop([_shop_item()])
    bot = _points_bot(shop=shop)

    asyncio.run(bot._shop_confirm(1, 2, "1"))
    assert "确定用" in bot.edits[0]  # type: ignore[attr-defined]
    assert "100" in bot.edits[0]  # type: ignore[attr-defined]
    # Confirmation only: nothing has been redeemed.
    assert shop.redeemed == []

    asyncio.run(bot._shop_redeem(1, 2, {"emby_user_id": "u1"}, "1"))
    assert shop.redeemed == [("u1", 1)]
    assert "兑换成功" in bot.edits[-1]  # type: ignore[attr-defined]


def test_confirming_a_withdrawn_item_is_refused() -> None:
    shop = _FakeShop([_shop_item(enabled=False)])
    bot = _points_bot(shop=shop)
    asyncio.run(bot._shop_confirm(1, 2, "1"))
    assert "已下架" in bot.edits[0]  # type: ignore[attr-defined]
    assert shop.redeemed == []


def test_a_failed_redemption_explains_why() -> None:
    shop = _FakeShop([_shop_item()], fail="积分不足")
    bot = _points_bot(shop=shop)
    asyncio.run(bot._shop_redeem(1, 2, {"emby_user_id": "u1"}, "1"))
    assert "兑换失败" in bot.edits[-1]  # type: ignore[attr-defined]
    assert "积分不足" in bot.edits[-1]  # type: ignore[attr-defined]


def test_the_points_view_shows_a_balance_and_where_it_came_from() -> None:
    bot = _points_bot(points=_FakePoints({"u1": 115}))
    text = bot._points_text({"emby_user_id": "u1"})
    assert "115" in text and "每日签到" in text


def test_the_line_view_reports_each_node_and_its_load() -> None:
    class _FakeScheduler:
        def snapshot(self):
            return [{"name": "hk1", "utilisation": 0.25, "ok": True,
                     "enabled": True},
                    {"name": "ca1", "utilisation": 0.95, "ok": True,
                     "enabled": True},
                    {"name": "old", "utilisation": 0.0, "ok": True,
                     "enabled": False}]

    bot = _points_bot(scheduler=_FakeScheduler())
    text = asyncio.run(bot._nodes_text())
    assert "hk1" in text and "25%" in text
    assert "ca1" in text and "95%" in text
    assert "维护中" in text


def test_the_ranking_gains_a_points_section() -> None:
    bot = _points_bot(points=_FakePoints())
    assert "积分排行" in bot._rankings_text(1)


# -- onboarding: deep links, pasted codes, quieter menus ---------------------

def _tg_message(text, *, tg_id=42, chat_id=1, username="ada", first="Ada",
                chat_type="private"):
    return {
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": tg_id, "username": username, "first_name": first},
        "text": text,
    }


class _CodeReg:
    """Allows one specific code; a missing credential is just 'not yet'."""

    def __init__(self, code="GOODCODE") -> None:
        self.code = code
        self.resolved: list[tuple] = []
        self.consumed: list = []

    def resolve(self, tg_user_id, credential=None):
        self.resolved.append((str(tg_user_id), credential))
        if str(credential or "").upper() == self.code:
            return _Verdict(credential=self.code)
        return _Verdict(allowed=False, reason="没有可用的凭证")

    def consume(self, admission, new_user_id):
        self.consumed.append(new_user_id)


def test_credential_shape_is_eight_or_twelve_letters() -> None:
    assert looks_like_credential("GOODCODE")
    assert looks_like_credential("abcd-efgh-ijkl")
    assert not looks_like_credential("alice")
    assert not looks_like_credential("/start")
    assert not looks_like_credential("")


def test_guest_menu_hides_register_when_every_channel_is_closed() -> None:
    bot = _bot(cfg={"allow_admin_grant": False, "allow_invite": False,
                    "allow_redeem": False})
    body, keys = bot._home("1", "Ada")
    assert "register" not in _actions(keys)
    assert "claim" in _actions(keys)
    assert "暂停注册" in body
    assert "没有账号" not in body


def test_guest_menu_offers_a_join_button_for_a_public_group() -> None:
    bot = _bot(cfg={"require_group": "@cineclub"})
    _, keys = bot._home("1", "Ada")
    urls = [b.get("url") for row in keys for b in row]
    assert "https://t.me/cineclub" in urls
    assert "register" in _actions(keys)


def test_a_start_payload_checks_the_code_instead_of_asking_for_one() -> None:
    bot = _bot(_FakeMembers(), _FakeEmby())
    bot._registration = _CodeReg()
    asyncio.run(bot._handle_command(1, "42", "ada", "/start GOODCODE"))
    assert bot._pending["1"][0] == "username"
    assert any("用户名" in m for m in bot.sent)  # type: ignore[attr-defined]
    assert bot._registration.resolved[-1] == ("42", "GOODCODE")


def test_start_with_botname_suffix_still_carries_the_payload() -> None:
    bot = _bot(_FakeMembers(), _FakeEmby())
    bot._registration = _CodeReg()
    asyncio.run(bot._handle_command(1, "42", "ada", "/start@deckbot GOODCODE"))
    assert bot._pending["1"][0] == "username"


def test_a_linked_member_does_not_reenter_registration_via_start_payload() -> None:
    member = {"emby_user_id": "u1", "username": "someone", "status": "active",
              "expires_at": int(time.time()) + 86400}
    bot = _bot(_FakeMembers({"42": member}), _FakeEmby())
    bot._registration = _CodeReg()
    asyncio.run(bot._handle_command(1, "42", "someone", "/start GOODCODE"))
    assert "1" not in bot._pending
    assert any("someone" in m for m in bot.sent)  # type: ignore[attr-defined]
    assert bot._registration.resolved == []


def test_pasting_a_code_is_enough_without_tapping_register() -> None:
    bot = _bot(_FakeMembers(), _FakeEmby())
    bot._registration = _CodeReg()
    asyncio.run(bot._handle_message(_tg_message("GOODCODE")))
    assert bot._pending["1"][0] == "username"
    assert any("用户名" in m for m in bot.sent)  # type: ignore[attr-defined]


def test_an_ordinary_hello_still_opens_the_guest_home() -> None:
    bot = _bot()
    asyncio.run(bot._handle_message(_tg_message("你好")))
    assert any("账号服务" in m for m in bot.sent)  # type: ignore[attr-defined]
    assert "1" not in bot._pending


def test_start_cancels_an_in_flight_registration() -> None:
    bot = _bot()
    bot._pending["1"] = ("credential", time.time() + 600, {})
    asyncio.run(bot._handle_command(1, "42", "ada", "/start"))
    assert "1" not in bot._pending


def test_group_messages_are_ignored() -> None:
    bot = _bot()
    asyncio.run(bot._handle_message(_tg_message("hi", chat_type="supergroup")))
    assert bot.sent == []  # type: ignore[attr-defined]


def test_invite_view_shows_a_deeplink_when_the_bot_username_is_known() -> None:
    member = {"emby_user_id": "u1", "username": "someone", "status": "active",
              "expires_at": int(time.time()) + 86400}
    reg = _FakeRegistration()
    reg.quota = 1
    reg.minted = ["ABCD2345"]
    bot = _bot(_FakeMembers({"999": member}))
    bot._registration = reg
    bot._bot_username = "deckbot"
    edits: list[str] = []

    async def fake_edit(chat, mid, text, keyboard=None):
        edits.append(text)

    bot._edit = fake_edit  # type: ignore[assignment]
    asyncio.run(bot._invites_view(1, 2, member))
    assert any("https://t.me/deckbot?start=ABCD2345" in t for t in edits)


def test_member_home_shows_the_server_address() -> None:
    member = {"emby_user_id": "u1", "username": "someone", "status": "active",
              "expires_at": int(time.time()) + 86400, "group_name": "标准"}
    bot = _bot(_FakeMembers({"999": member}))
    body, _ = bot._home("999", "Friend")
    assert "https://emby.example" in body
    assert "标准" in body


def test_member_help_is_not_a_permission_error() -> None:
    member = {"emby_user_id": "u1", "username": "someone", "status": "active",
              "expires_at": int(time.time()) + 86400}
    bot = _bot(_FakeMembers({"999": member}))
    asyncio.run(bot._handle_command(1, "999", "someone", "/help"))
    assert "无权限" not in bot.sent[-1]  # type: ignore[attr-defined]
    assert "使用说明" in bot.sent[-1]  # type: ignore[attr-defined]
