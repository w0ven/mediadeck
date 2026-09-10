"""Line-page online count uses Emby playback sessions, never node probe watermarks."""
import asyncio
import copy
from types import SimpleNamespace

import pytest
from test_request_bot import bot as request_bot  # noqa: F401
from test_request_bot import run

from app.modules.telegram import PLAYBACK_COUNT_TTL


class Sessions:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0
        self.fail = False

    async def active_sessions(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError('local upstream failure')
        return self.rows


@pytest.fixture
def bot(request):
    b = request.getfixturevalue('request_bot')
    b.cfg.update(playback_lines=[{'label': '主线路', 'url': 'https://play.invalid'}])
    return b


def refresh(bot, mid=700):
    run(bot._handle_callback({'id': 'refresh', 'data': 'me_nodes', 'from': {'id': '900'},
        'message': {'chat': {'id': '900', 'type': 'private'}, 'message_id': mid}}))
    return bot.transport.messages[('900', mid)]['text']


def test_probe_zero_does_not_hide_playback_sessions_or_count_unique_users(bot):
    bot._emby = Sessions([{'Id': str(n), 'UserId': 'same-user', 'Item': 'Movie',
                           'Paused': n % 2 == 0} for n in range(13)])
    bot._scheduler = SimpleNamespace(snapshot=lambda: [{'name': 'de1', 'enabled': True,
                                       'active_streams': 0, 'utilisation': 0.0}])
    text = run(bot._nodes_text())
    assert '当前在线：<b>13</b> 路播放' in text
    assert '0 个连接' in text
    assert '节点连接数不等于 Emby 播放会话数' in text
    assert bot._emby.calls == 1


def test_disabled_node_existing_sessions_still_count_without_changing_scheduler(bot):
    nodes = [{'name': 'ca1', 'enabled': False, 'manually_disabled': True, 'active_streams': 1,
              'utilisation': 0.4}, {'name': 'de1', 'enabled': True, 'active_streams': 0},
             {'name': 'nc1', 'enabled': True, 'active_streams': 1}]
    original = copy.deepcopy(nodes)
    bot._scheduler = SimpleNamespace(snapshot=lambda: nodes)
    bot._emby = Sessions([{'Id': str(n), 'Item': 'Movie', 'Node': 'ca1', 'Paused': False}
                          for n in range(13)])
    text = refresh(bot)
    assert '当前在线：<b>13</b> 路播放' in text and '维护中' in text
    assert 'ca1' in text and '1 个连接' in text
    assert nodes == original


def test_real_empty_sessions_are_zero_and_unavailable_is_never_zero_or_probe_fallback(bot):
    bot._scheduler = SimpleNamespace(snapshot=lambda: [{'name': 'node', 'active_streams': 9}])
    emby = bot._emby = Sessions([])
    assert '当前在线：<b>0</b> 路播放' in refresh(bot)
    emby.fail = True
    bot._online_plays_cache.clear()
    text = refresh(bot)
    assert '当前在线：暂不可用' in text
    assert '当前在线：<b>0</b>' not in text and '当前在线：<b>9</b>' not in text
    assert '9 个连接' in text
    refresh(bot)
    assert emby.calls == 2, 'unavailable reads should also have a short cache TTL'


def test_line_refresh_edits_same_card_and_updates_after_web_equivalent_cache_ttl(bot, monkeypatch):
    now = [1_800_000_000.0]
    monkeypatch.setattr('app.core.cache.time.time', lambda: now[0])
    emby = bot._emby = Sessions([{'Id': 'a', 'Item': 'One'}])
    assert '当前在线：<b>1</b>' in refresh(bot, mid=777)
    emby.rows = [{'Id': 'a', 'Item': 'One'}, {'Id': 'b', 'Item': 'Two', 'Paused': True}]
    assert '当前在线：<b>1</b>' in refresh(bot, mid=777)
    assert emby.calls == 1 and PLAYBACK_COUNT_TTL == 5
    now[0] += PLAYBACK_COUNT_TTL + .1
    assert '当前在线：<b>2</b>' in refresh(bot, mid=777)
    assert emby.calls == 2
    edits = [p for method, p in bot.transport.calls if method == 'editMessageText']
    assert len(edits) == 3 and all(p['message_id'] == 777 for p in edits)
    assert not any(method == 'sendMessage' for method, _ in bot.transport.calls)


def test_timeout_and_invalid_sessions_report_unavailable_without_breaking_addresses(bot, monkeypatch):
    cancelled = []
    async def slow():
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)
    monkeypatch.setattr('app.modules.telegram.PLAYBACK_COUNT_TIMEOUT', .01)
    bot._emby = SimpleNamespace(active_sessions=slow)
    assert '当前在线：暂不可用' in run(bot._nodes_text())
    assert cancelled
    for source in (Sessions(None), Sessions({'logged_in_users': 12}), None):
        bot._emby = source
        bot._online_plays_cache.clear()
        text = run(bot._nodes_text())
        assert '当前在线：暂不可用' in text and 'https://play.invalid' in text
        assert '当前在线：<b>0</b>' not in text


def test_hiding_node_watermarks_does_not_hide_emby_session_count(bot):
    bot.cfg['playback_lines_show_load'] = False
    bot._emby = Sessions([{'Id': 'a', 'Item': 'Movie'}])
    text = refresh(bot)
    assert '当前在线：<b>1</b> 路播放' in text
    assert '节点水位' not in text


def test_live_adapter_matches_web_playback_rows_excluding_idle_login(bot):
    import httpx

    from app.adapters.live import LiveEmby

    raw = [
        {'Id': 'idle-login', 'UserId': 'u0', 'UserName': 'Signed in'},
        {'Id': 'playing', 'UserId': 'u1', 'NowPlayingItem': {'Id': 'm1', 'Name': 'One'}},
        {'Id': 'paused', 'UserId': 'u1', 'NowPlayingItem': {'Id': 'm2', 'Name': 'Two'},
         'PlayState': {'IsPaused': True}},
    ]
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == 'GET' and request.url.path == '/emby/Sessions'
        return httpx.Response(200, json=raw)
    emby = LiveEmby(lambda: {'enabled': True, 'url': 'https://emby.invalid', 'api_key': 'test-only'})
    emby._client = lambda timeout, verify: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bot._emby = emby
    assert '当前在线：<b>2</b> 路播放' in refresh(bot)
    assert len(calls) == 1
