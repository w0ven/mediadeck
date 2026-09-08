"""The request conversation and the uploader fan-out.

The fan-out is the part worth testing hardest. Every uploader gets their own
message so that a claim can *take the button back*, and that only works if
each message id was recorded. If it is not, the losers keep a live 接单 button
for a job that is gone and two people download the same 40GB.

The rest is the member's side of the loop: paste a link, confirm the title
that came back, and be told what happened to it. The confirmation step exists
because a wrong id is otherwise invisible until an uploader has spent an
evening on the wrong film.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.db import Database
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.requests import RequestService
from app.modules.telegram import TelegramBot

FAKE_CRED = "1234567" + ":" + "placeholder-not-a-real-credential"

MEMBER_CHAT = "900"
UP1_CHAT = "801"
UP2_CHAT = "802"


class _FakeTmdb:
    def __init__(self, answers: dict | None = None) -> None:
        self.answers = answers or {}

    async def resolve(self, media_type, tmdb_id):
        found = self.answers.get((media_type, int(tmdb_id)))
        if found is not None:
            return media_type, dict(found)
        other = "tv" if media_type == "movie" else "movie"
        found = self.answers.get((other, int(tmdb_id)))
        if found is not None:
            return other, dict(found)
        return media_type, None


@pytest.fixture()
def bot(tmp_path):
    db = Database(tmp_path / "reqbot.db")
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    tmdb = _FakeTmdb({("movie", 550): {"title": "搏击俱乐部", "year": 1999,
                                       "poster_path": "/p.jpg"}})
    requests = RequestService(db, members, groups, tmdb)

    members.upsert("u1", "alice", {"group_id": "standard"}, actor="test")
    members.bind_telegram("u1", MEMBER_CHAT, "alice_tg", actor="test")
    for uid, name, chat in (("up1", "bob", UP1_CHAT), ("up2", "dave", UP2_CHAT)):
        members.upsert(uid, name, {"group_id": "standard"}, actor="test")
        members.set_roles(uid, ["uploader"], actor="test")
        members.bind_telegram(uid, chat, name + "_tg", actor="test")

    cfg = {"enabled": True, "bot_token": FAKE_CRED, "register_days": 30,
           "max_users": 0, "require_group": "", "default_group_id": "",
           "allow_admin_grant": True, "allow_invite": True,
           "allow_redeem": True}
    instance = TelegramBot(lambda: cfg, members, db=db, requests=requests,
                           tmdb=tmdb, groups=groups)
    instance.sent = []
    instance.edits = []
    instance.photos = []
    instance.answers = []
    instance._next_message_id = 1000

    async def fake_send(chat, text, keyboard=None):
        instance.sent.append((str(chat), text, keyboard))
        return True

    async def fake_send_message(chat, text, keyboard=None):
        instance._next_message_id += 1
        instance.sent.append((str(chat), text, keyboard))
        return instance._next_message_id

    async def fake_send_photo(chat, photo, caption, keyboard=None):
        instance.photos.append((str(chat), photo, caption))
        instance.sent.append((str(chat), caption, keyboard))
        return True

    async def fake_edit(chat, mid, text, keyboard=None):
        instance.edits.append((str(chat), int(mid), text, keyboard))
        return True

    async def fake_answer(callback_id, text=""):
        instance.answers.append(text)

    instance.send = fake_send
    instance.send_message = fake_send_message
    instance.send_photo = fake_send_photo
    instance._edit = fake_edit
    instance._answer_callback = fake_answer
    instance.members = members
    instance.requests = requests
    instance.groups = groups
    return instance


def _message(bot, text: str, chat: str = MEMBER_CHAT):
    asyncio.run(bot._handle_message({
        "chat": {"id": chat},
        "from": {"id": chat, "username": "alice_tg", "first_name": "A"},
        "text": text,
    }))


def _tap(bot, data: str, chat: str = MEMBER_CHAT, message_id: int = 7):
    asyncio.run(bot._handle_callback({
        "id": "cb", "data": data,
        "message": {"chat": {"id": chat}, "message_id": message_id},
        "from": {"id": chat, "username": "x", "first_name": "X"},
    }))


def _texts(bot, chat: str | None = None) -> list[str]:
    return [t for c, t, _ in bot.sent if chat is None or c == str(chat)]


def _edits_for(bot, chat: str) -> list[str]:
    return [t for c, _, t, _ in bot.edits if c == str(chat)]


def _keyboard_actions(keyboard) -> set:
    if not keyboard:
        return set()
    return {b.get("callback_data") for row in keyboard for b in row}


# -- the member's side -------------------------------------------------------

def test_the_menu_offers_requests_and_shows_the_allowance_first(bot) -> None:
    """Being told the quota is spent only after finding a link is the
    annoying version of this feature."""
    _tap(bot, "req_new")
    body = _edits_for(bot, MEMBER_CHAT)[-1]
    assert "求片" in body and "3 次" in body
    assert bot._pending[MEMBER_CHAT][0] == "request_link"


def test_a_member_with_no_allowance_left_is_told_before_being_asked(bot) -> None:
    for tmdb_id in (1, 2, 3):
        asyncio.run(bot.requests.create("u1", "movie", tmdb_id))

    _tap(bot, "req_new")

    assert "已经用完" in _edits_for(bot, MEMBER_CHAT)[-1]
    assert MEMBER_CHAT not in bot._pending


def test_a_link_resolves_to_a_title_with_a_poster_and_a_confirmation(bot) -> None:
    _tap(bot, "req_new")
    _message(bot, "https://www.themoviedb.org/movie/550")

    body = _edits_for(bot, MEMBER_CHAT)[-1]
    assert "搏击俱乐部" in body and "1999" in body
    assert "w342/p.jpg" in body
    assert not bot.photos, "the confirmation stays on the same message"
    # Nothing is written until the member confirms.
    assert bot.requests.list() == []
    assert bot._pending[MEMBER_CHAT][0] == "request_confirm"


def test_confirming_creates_the_request_and_reports_the_number(bot) -> None:
    _tap(bot, "req_new")
    _message(bot, "550")
    _tap(bot, "req_ok")

    rows = bot.requests.list()
    assert len(rows) == 1 and rows[0]["tmdb_id"] == 550
    reply = _edits_for(bot, MEMBER_CHAT)[-1]
    assert "已提交" in reply and "#1" in reply
    assert "2 次" in reply, "the remaining allowance should be updated"


def test_an_unrecognisable_link_explains_the_format_and_keeps_the_step(bot) -> None:
    _tap(bot, "req_new")
    _message(bot, "随便来部好看的")

    reply = _edits_for(bot, MEMBER_CHAT)[-1]
    assert "没能识别" in reply and "themoviedb.org" in reply
    assert bot._pending[MEMBER_CHAT][0] == "request_link"
    assert bot.requests.list() == []


def test_with_no_metadata_the_confirmation_still_offers_the_id(bot) -> None:
    """No TMDB key, or an id TMDB does not know: the request still works."""
    _tap(bot, "req_new")
    _message(bot, "99999")

    reply = _edits_for(bot, MEMBER_CHAT)[-1]
    assert "99999" in reply and "查不到片名" in reply
    assert not bot.photos

    _tap(bot, "req_ok")
    assert bot.requests.list()[0]["display_title"] == "#99999"


def test_cancelling_the_confirmation_writes_nothing(bot) -> None:
    _tap(bot, "req_new")
    _message(bot, "550")
    _tap(bot, "home")

    assert bot.requests.list() == []
    assert MEMBER_CHAT not in bot._pending


def test_confirming_an_expired_conversation_says_so(bot) -> None:
    _tap(bot, "req_ok")
    assert "过期" in _edits_for(bot, MEMBER_CHAT)[-1]
    assert bot.requests.list() == []


def test_a_duplicate_request_is_refused_with_a_readable_reason(bot) -> None:
    asyncio.run(bot.requests.create("u1", "movie", 550))
    _tap(bot, "req_new")
    _message(bot, "550")
    _tap(bot, "req_ok")

    assert "已经有人求过" in _edits_for(bot, MEMBER_CHAT)[-1]
    assert len(bot.requests.list()) == 1


def test_my_requests_lists_recent_ones_with_their_status(bot) -> None:
    first = asyncio.run(bot.requests.create("u1", "movie", 550))
    bot.requests.claim(first["id"], "up1")
    asyncio.run(bot.requests.create("u1", "movie", 551))

    _tap(bot, "my_requests")

    body = _edits_for(bot, MEMBER_CHAT)[-1]
    assert "#1" in body and "处理中" in body
    assert "#2" in body and "待接单" in body
    assert "1 次" in body


def test_my_requests_shows_the_rejection_reason(bot) -> None:
    req = asyncio.run(bot.requests.create("u1", "movie", 550))
    bot.requests.claim(req["id"], "up1")
    bot.requests.resolve(req["id"], "up1", done=False, note="全网无片源")

    _tap(bot, "my_requests")
    assert "全网无片源" in _edits_for(bot, MEMBER_CHAT)[-1]


def test_my_requests_is_empty_for_someone_who_never_asked(bot) -> None:
    _tap(bot, "my_requests")
    assert "还没有求过片" in _edits_for(bot, MEMBER_CHAT)[-1]


# -- the fan-out -------------------------------------------------------------

def test_every_uploader_gets_their_own_message_and_it_is_recorded(bot) -> None:
    """One message each, with its id stored: without that a claim cannot take
    the button away from the uploaders who did not win."""
    request = asyncio.run(bot.requests.create("u1", "movie", 550))

    sent = asyncio.run(bot.announce_request(request))

    assert sent == 2
    assert set(_texts(bot, UP1_CHAT)) and set(_texts(bot, UP2_CHAT))
    body = _texts(bot, UP1_CHAT)[-1]
    assert "新求片 #1" in body and "搏击俱乐部" in body and "alice" in body

    notices = bot.requests.notices(request["id"])
    assert {n["tg_user_id"] for n in notices} == {UP1_CHAT, UP2_CHAT}
    assert all(n["message_id"] for n in notices)


def test_the_announcement_carries_a_claim_button(bot) -> None:
    request = asyncio.run(bot.requests.create("u1", "movie", 550))
    asyncio.run(bot.announce_request(request))
    keyboard = [k for c, _, k in bot.sent if c == UP1_CHAT][-1]
    assert _keyboard_actions(keyboard) == {"req_claim:1"}


def test_an_uploader_without_a_linked_chat_is_simply_skipped(bot) -> None:
    bot.members.upsert("up3", "erin", {"group_id": "standard"}, actor="test")
    bot.members.set_roles("up3", ["uploader"], actor="test")

    request = asyncio.run(bot.requests.create("u1", "movie", 550))
    assert asyncio.run(bot.announce_request(request)) == 2


def test_submitting_from_the_bot_announces_to_uploaders(bot) -> None:
    _tap(bot, "req_new")
    _message(bot, "550")
    _tap(bot, "req_ok")

    assert any("新求片" in t for t in _texts(bot, UP1_CHAT))
    assert any("新求片" in t for t in _texts(bot, UP2_CHAT))


# -- claiming ----------------------------------------------------------------

def test_claiming_gives_the_winner_the_resolve_buttons(bot) -> None:
    request = asyncio.run(bot.requests.create("u1", "movie", 550))
    asyncio.run(bot.announce_request(request))

    _tap(bot, "req_claim:1", chat=UP1_CHAT)

    assert bot.requests.get(1)["claimed_by"] == "up1"
    body = _edits_for(bot, UP1_CHAT)[-1]
    assert "已接单" in body
    keyboard = [k for c, _, _, k in bot.edits if c == UP1_CHAT][-1]
    assert _keyboard_actions(keyboard) == {"req_done:1", "req_fail:1"}


def test_the_other_uploaders_lose_their_button_when_somebody_claims(bot) -> None:
    """A live button on a job that is gone is how two people end up
    downloading the same title."""
    request = asyncio.run(bot.requests.create("u1", "movie", 550))
    asyncio.run(bot.announce_request(request))

    _tap(bot, "req_claim:1", chat=UP1_CHAT)

    losers = [(t, k) for c, _, t, k in bot.edits if c == UP2_CHAT]
    assert losers, "the other uploader's message must be rewritten"
    text, keyboard = losers[-1]
    assert "已由 bob 接单" in text
    assert not keyboard, "the claim button must be gone"


def test_the_second_claimer_is_told_who_won_and_nothing_changes(bot) -> None:
    request = asyncio.run(bot.requests.create("u1", "movie", 550))
    asyncio.run(bot.announce_request(request))
    _tap(bot, "req_claim:1", chat=UP1_CHAT)

    _tap(bot, "req_claim:1", chat=UP2_CHAT)

    assert any("已被 bob 接单" in a for a in bot.answers)
    assert bot.requests.get(1)["claimed_by"] == "up1"


def test_a_member_who_is_not_an_uploader_cannot_claim(bot) -> None:
    request = asyncio.run(bot.requests.create("u1", "movie", 550))
    asyncio.run(bot.announce_request(request))

    _tap(bot, "req_claim:1", chat=MEMBER_CHAT)

    assert any("不是上片员" in a for a in bot.answers)
    assert bot.requests.get(1)["status"] == "open"


def test_the_claim_alert_is_not_swallowed_by_the_generic_ack(bot) -> None:
    """answerCallbackQuery may be sent once. Acking every callback up front
    would leave the loser with no explanation at all."""
    request = asyncio.run(bot.requests.create("u1", "movie", 550))
    asyncio.run(bot.announce_request(request))
    _tap(bot, "req_claim:1", chat=UP1_CHAT)
    bot.answers.clear()

    _tap(bot, "req_claim:1", chat=UP2_CHAT)

    assert bot.answers == ["已被 bob 接单"]


# -- resolving ---------------------------------------------------------------

def test_marking_it_done_notifies_the_requester(bot) -> None:
    request = asyncio.run(bot.requests.create("u1", "movie", 550))
    asyncio.run(bot.announce_request(request))
    _tap(bot, "req_claim:1", chat=UP1_CHAT)

    _tap(bot, "req_done:1", chat=UP1_CHAT)

    assert bot.requests.get(1)["status"] == "done"
    told = _texts(bot, MEMBER_CHAT)[-1]
    assert "已处理" in told and "搏击俱乐部" in told and "等待入库" in told


def test_refusing_asks_for_a_reason_before_closing_anything(bot) -> None:
    request = asyncio.run(bot.requests.create("u1", "movie", 550))
    asyncio.run(bot.announce_request(request))
    _tap(bot, "req_claim:1", chat=UP1_CHAT)

    _tap(bot, "req_fail:1", chat=UP1_CHAT)

    assert bot._pending[UP1_CHAT][0] == "request_reason"
    assert bot.requests.get(1)["status"] == "claimed", "not closed yet"


def test_the_reason_reaches_the_member_who_asked(bot) -> None:
    """A refusal with no reason cannot tell the member whether to ask again
    differently."""
    request = asyncio.run(bot.requests.create("u1", "movie", 550))
    asyncio.run(bot.announce_request(request))
    _tap(bot, "req_claim:1", chat=UP1_CHAT)
    _tap(bot, "req_fail:1", chat=UP1_CHAT)

    asyncio.run(bot._handle_message({
        "chat": {"id": UP1_CHAT},
        "from": {"id": UP1_CHAT, "username": "bob_tg", "first_name": "B"},
        "text": "全网都没有片源",
    }))

    row = bot.requests.get(1)
    assert row["status"] == "rejected"
    assert row["result_note"] == "全网都没有片源"
    told = _texts(bot, MEMBER_CHAT)[-1]
    assert "无法处理" in told and "全网都没有片源" in told


def test_an_uploader_who_did_not_claim_cannot_close_it(bot) -> None:
    request = asyncio.run(bot.requests.create("u1", "movie", 550))
    asyncio.run(bot.announce_request(request))
    _tap(bot, "req_claim:1", chat=UP1_CHAT)
    bot.answers.clear()

    _tap(bot, "req_done:1", chat=UP2_CHAT)

    assert bot.requests.get(1)["status"] == "claimed"
    assert any("别人接的单" in a for a in bot.answers)


def test_a_request_from_a_member_with_no_chat_notifies_nobody(bot) -> None:
    """Must not raise: an operator can create a request for an unlinked
    member from the panel."""
    bot.members.upsert("u9", "frank", {"group_id": "standard"}, actor="test")
    request = asyncio.run(bot.requests.create("u9", "movie", 550))
    bot.requests.claim(request["id"], "up1")
    resolved = bot.requests.resolve(request["id"], "up1", done=True)

    assert asyncio.run(bot.notify_request_resolved(resolved["request"])) is False


# -- panel-side claim --------------------------------------------------------

def test_claiming_from_the_panel_also_retracts_the_buttons(bot) -> None:
    request = asyncio.run(bot.requests.create("u1", "movie", 550))
    asyncio.run(bot.announce_request(request))
    bot.requests.claim(request["id"], "up1")

    edited = asyncio.run(bot.announce_request_claimed(request["id"], "up1"))

    assert edited == 2
    for chat in (UP1_CHAT, UP2_CHAT):
        assert "已由 bob 接单" in _edits_for(bot, chat)[-1]
