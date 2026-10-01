"""Separate, private routing help with copyable operator-supplied examples."""
import time
from html import escape

import pytest
from fastapi.testclient import TestClient
from test_request_bot import bot as request_bot  # noqa: F401
from test_request_bot import run

from app.core.errors import ConfigError
from app.core.store import SettingsStore
from app.main import app
from app.modules.settings import (
    PLAYBACK_ROUTING_RULES_MAX,
    SettingsService,
    normalize_playback_routing_rules,
)

RULES = "rules:\n  - DOMAIN,play.example.com,代理组\n  - GEOIP,CN,DIRECT"


@pytest.fixture
def bot(request):
    return request.getfixturevalue("request_bot")


def tap(bot, action, actor="900", group=False):
    chat = "-10055" if group else actor
    run(bot._handle_callback({
        "id": "routing-" + action, "data": action, "from": {"id": actor},
        "message": {"chat": {"id": chat, "type": "supergroup" if group else "private"},
                    "message_id": 777},
    }))
    return bot.transport.messages.get((chat, 777), {})


def actions(message):
    return {b["callback_data"] for row in message["reply_markup"]["inline_keyboard"] for b in row}


def test_lines_rules_and_back_edit_one_card_without_expanding_the_line_page(bot):
    bot.cfg.update(playback_lines=[{"label": "主地址", "url": "https://play.example.com"}],
                   playback_lines_note="主线路建议代理；国内盘资源请直连。",
                   playback_routing_rules=RULES)
    lines = tap(bot, "me_nodes")
    assert actions(lines) == {"me_nodes", "me_routing", "home"}
    assert "国内盘资源请直连" in lines["text"] and "rules:" not in lines["text"]
    rules = tap(bot, "me_routing")
    assert actions(rules) == {"me_nodes", "home"}
    assert f'<pre><code class="language-yaml">{escape(RULES)}</code></pre>' in rules["text"]
    assert "自己的代理软件、内核及策略组名称调整配置" in rules["text"]
    assert "勿直接覆盖原配置" in rules["text"]
    assert "GeoSite" in rules["text"]
    again = tap(bot, "me_nodes")
    assert "rules:" not in again["text"] and "当前在线" in again["text"]
    assert len(bot.transport.messages) == 1
    assert not any(m in ("sendMessage", "sendPhoto") for m, _ in bot.transport.calls)


def test_empty_rules_hide_button_and_stale_button_has_a_way_back(bot):
    bot.cfg["playback_routing_rules"] = " \n "
    assert "me_routing" not in actions(tap(bot, "me_nodes"))
    stale = tap(bot, "me_routing")
    assert "暂未配置" in stale["text"] and "<pre>" not in stale["text"]
    assert actions(stale) == {"me_nodes", "home"}


def test_rule_content_is_escaped_not_executable_telegram_html(bot):
    bot.cfg["playback_routing_rules"] = "</code></pre><b>not markup</b> & example"
    text = tap(bot, "me_routing")["text"]
    assert escape(bot.cfg["playback_routing_rules"]) in text
    assert "<b>not markup</b>" not in text


def test_rules_are_not_disclosed_to_unbound_users_or_group_chats(bot):
    bot.cfg["playback_routing_rules"] = RULES
    guest = tap(bot, "me_routing", actor="99999")
    assert "还没有账号" in guest["text"] and RULES not in guest["text"]
    assert tap(bot, "me_routing", group=True) == {}


def test_entering_rules_abandons_previous_input_draft(bot):
    bot.cfg["playback_routing_rules"] = RULES
    key = bot._pkey("900")
    bot._pending[key] = ("local_draft", time.time() + 60, {})
    tap(bot, "me_routing")
    assert key not in bot._pending


def test_rules_settings_round_trip_preserves_unrelated_fields_and_partial_saves():
    with TestClient(app) as client:
        auth = ("admin", "change-me")
        saved = client.post("/api/settings/telegram", auth=auth, json={
            "playback_routing_rules": RULES.replace("\n", "\r\n"),
            "playback_lines_note": "简短提示", "playback_lines_show_load": False,
        })
        assert saved.status_code == 200
        assert saved.json()["playback_routing_rules"] == RULES
        assert client.post("/api/settings/telegram", auth=auth,
                           json={"playback_lines_note": "新的简短提示"}).status_code == 200
        cfg = client.get("/api/settings/telegram", auth=auth).json()
        assert cfg["playback_routing_rules"] == RULES
        assert cfg["playback_lines_show_load"] is False
        assert client.post("/api/settings/telegram", auth=auth,
                           json={"playback_routing_rules": ""}).json()["playback_routing_rules"] == ""


@pytest.mark.parametrize("bad", ["x" * (PLAYBACK_ROUTING_RULES_MAX + 1), "bad\x00", "bad\tvalue"])
def test_invalid_rules_are_rejected_without_mutating_settings(tmp_path, bad):
    store = SettingsStore(tmp_path / "settings.json")
    service = SettingsService(store)
    service.save_telegram({"playback_routing_rules": RULES})
    before = store.document()
    with pytest.raises(ConfigError):
        service.save_telegram({"playback_routing_rules": bad})
    assert store.document() == before


def test_bad_stored_rules_do_not_hide_other_line_settings(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.set_section("telegram", {
        "playback_lines": [{"label": "主线路", "url": "https://play.example.com"}],
        "playback_lines_note": "提示", "playback_routing_rules": "bad\x00",
    })
    cfg = SettingsService(store).telegram_config()
    assert cfg["playback_lines"] and cfg["playback_lines_note"] == "提示"
    assert cfg["playback_routing_rules"] == ""


def test_maximum_rule_size_fits_telegram_message(bot):
    bot.cfg["playback_routing_rules"] = "x" * PLAYBACK_ROUTING_RULES_MAX
    assert normalize_playback_routing_rules(bot.cfg["playback_routing_rules"])
    assert len(tap(bot, "me_routing")["text"]) < 4096
