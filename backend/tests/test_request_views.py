"""Role changes must never change the provenance of an existing request card."""

import json

import pytest
from test_request_bot import bot as request_bot  # noqa: F401
from test_request_bot import card_with, cards, message, run, tap


@pytest.fixture
def bot(request):
    return request.getfixturevalue("request_bot")


def card_at(bot, mid, chat="900"):
    return bot.db.one("SELECT * FROM request_cards WHERE chat_id=? AND message_id=?", (chat, mid))


def actions(bot, mid, chat="900"):
    return set(json.loads(card_at(bot, mid, chat)["actions"]))


def open_original(bot):
    message(bot, "/requests")
    tap(bot, "new")
    message(bot, "https://www.themoviedb.org/movie/550")
    original = card_with(bot, "submit")
    tap(bot, "submit", card=original)
    return original["message_id"]


USER_ACTIONS = {"full", "thread:0", "reply", "cancel", "list:mine"}


@pytest.mark.parametrize("roles", [[], ["uploader"], ["admin"], ["admin", "uploader"]])
def test_own_card_stays_user_independent_notice_and_workbench_stay_staff(bot, roles):
    bot.members.set_roles("u1", roles, actor="test")
    mid = open_original(bot)
    assert actions(bot, mid) == USER_ACTIONS
    assert bot._panel["900"] == mid, "own uploader notification stole the user panel"
    if roles:
        staff = [c for c in cards(bot) if json.loads(c["payload"]).get("view") == "staff"]
        assert len(staff) == 1 and staff[0]["message_id"] != mid
        assert {"accept", "reject", "ask", "internal"} <= actions(bot, staff[0]["message_id"])
        assert not {"reply", "cancel"} & actions(bot, staff[0]["message_id"])
        bot.service.message(1, "up1", "内部备注仅管理可见", internal=True)
    bot = bot.reboot()
    bot._panel["900"] = mid  # restored/opened user card must survive background refresh
    run(bot._rq_refresh(1))
    assert bot._panel["900"] == mid
    assert actions(bot, mid) == USER_ACTIONS
    tap(bot, "full", card=card_at(bot, mid))
    assert "correct" not in actions(bot, mid)
    tap(bot, "thread:0", card=card_at(bot, mid))
    visible = bot.transport.messages[("900", mid)]
    assert "内部备注仅管理可见" not in (visible.get("text") or visible.get("caption", ""))
    tap(bot, "view:1", card=card_at(bot, mid))
    assert actions(bot, mid) == USER_ACTIONS
    if roles:
        message(bot, "/uploader")
        tap(bot, "view:1")
        staff = card_with(bot, "accept")
        assert json.loads(staff["payload"])["view"] == "staff"
        tap(bot, "accept", card=dict(staff, message_id=mid))
        assert bot.service.get(1)["status"] == "open"
        tap(bot, "accept", card=staff)
        assert bot.service.get(1)["status"] == "accepted"
        assert not {"accept", "correct", "refund"} & actions(bot, mid)


def test_role_promotion_and_legacy_mixed_card_do_not_grant_management_view(bot):
    mid = open_original(bot)
    bot.members.set_roles("u1", ["admin", "uploader"], actor="test")
    run(bot._rq_refresh(1))
    assert actions(bot, mid) == USER_ACTIONS
    bot.db.execute(
        "UPDATE request_cards SET payload=?,actions=? WHERE chat_id=? AND message_id=?",
        (
            json.dumps({"rid": 1}),
            json.dumps(list(USER_ACTIONS | {"accept", "correct", "refund"})),
            "900",
            mid,
        ),
    )
    old = card_at(bot, mid)
    bot = bot.reboot()
    tap(bot, "accept", card=old)
    assert bot.service.get(1)["status"] == "open"
    message(bot, "/requests")
    tap(bot, "list:mine")
    tap(bot, "view:1")
    assert json.loads(cards(bot)[0]["payload"])["view"] == "user"


def test_staff_role_rechecked_after_restart_result_notice_is_user_view(bot):
    bot.members.set_roles("u1", ["uploader"], actor="test")
    mid = open_original(bot)
    notice = card_with(bot, "accept")
    bot.members.set_roles("u1", [], actor="test")
    bot = bot.reboot()
    tap(bot, "accept", card=notice)
    assert bot.service.get(1)["status"] == "open"
    bot.service.finish(1, "up1", "accepted")
    run(bot.flush_request_notifications())
    result = cards(bot)[0]
    assert json.loads(result["payload"])["view"] == "user"
    tap(bot, "view:1", card=result)
    assert not {"accept", "internal", "correct", "refund"} & actions(bot, result["message_id"])
    assert not {"accept", "internal", "correct", "refund"} & actions(bot, mid)


def test_workbench_buttons_name_each_correct_title_with_bounded_label(bot):
    for ident, kind, title in [
        (550, "movie", "奥德赛"),
        (551, "tv", "我的剧集"),
        (552, "movie", "长片名" * 30),
        (553, "tv", ""),
    ]:
        bot._groups.update("standard", {"request_quota": 0})
        row = run(bot.service.create("u1", kind, ident))
        bot.db.execute("UPDATE media_requests SET title=? WHERE id=?", (title, row["id"]))
    message(bot, "/uploader", "801")
    card = cards(bot, "801")[0]
    buttons = bot.transport.messages[("801", card["message_id"])]["reply_markup"]["inline_keyboard"]
    entries = [b for line in buttons for b in line if ":view:" in b.get("callback_data", "")]
    assert len(entries) == 4
    for b in entries:
        rid = int(b["callback_data"].rsplit(":", 1)[1])
        row = bot.service.get(rid)
        assert f"#{rid}" in b["text"] and len(b["text"]) <= 60
        assert (row["title"][:12] if row["title"] else "暂未获取片名") in b["text"]
    long = next(b for b in entries if b["callback_data"].endswith(":3"))
    tap(bot, "view:3", "801", card=card)
    rendered = bot.transport.messages[("801", cards(bot, "801")[0]["message_id"])]
    assert "长片名" * 30 in (rendered.get("text") or rendered.get("caption", ""))
    assert "…" in long["text"]
