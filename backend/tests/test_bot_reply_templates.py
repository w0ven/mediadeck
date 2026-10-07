"""Source-template previews from synthetic fixtures, never a Telegram connection."""
from __future__ import annotations

import asyncio
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.modules.bot_requests import ACCEPT_NOTICE
from app.modules.bot_views import bandwidth_lines, duration, quota_lines, short_label
from app.modules.plugins_builtin import ViewingReportPlugin
from app.modules.telegram import RULES_TEXT, TelegramBot

PREVIEW = Path(__file__).with_name('fixtures') / 'bot_reply_previews.md'


class TelegramHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        assert tag in {'b', 'i', 'a', 'pre', 'code'}
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag

    def handle_data(self, data):
        self.parts.append(data)


def plain(text):
    parser = TelegramHTML()
    parser.feed(text)
    parser.close()
    assert not parser.stack
    return ''.join(parser.parts)


def demo_bot():
    member = {
        'emby_user_id': 'demo-viewer', 'username': 'DemoViewer', 'tg_user_id': '1001',
        'group_name': '演示组', 'group_id': 'standard', 'status': 'active', 'roles': [],
        'expires_at': None, 'traffic_quota_bytes': 200 * 1024**3,
        'quota_source': 'measured', 'measured_used_bytes': 35 * 1024**3,
        'bandwidth_limit_kbps': 125829, 'max_streams': 2, 'device_count': 3,
    }
    cfg = {'bot_token': '', 'enabled': False, 'allow_invite': True, 'allow_redeem': True}
    members = SimpleNamespace(
        find_by_telegram=lambda uid: member if str(uid) == '1001' else None,
        find_by_username=lambda name: member if name == 'DemoViewer' else None,
        delete_preview=lambda uid, cascade=False: {'target': member, 'available_cascade': []},
    )
    points = SimpleNamespace(balance=lambda uid: 120,
                             ledger=lambda uid, limit: [{'delta': -30, 'reason_label': '兑换流量', 'created_at': 0}])
    stats = SimpleNamespace(watch_summary=lambda uid: {
        'seconds_24h': 5400, 'seconds_30d': 43200, 'recorded_seconds': 86400,
        'historical_unverified_seconds': 123456, 'verification_since': 1,
    } if uid == 'demo-viewer' else {})
    bot = TelegramBot(lambda: cfg, members, points=points, stats=stats)
    bot._requests = SimpleNamespace(remaining=lambda uid: 3)
    bot._shop = SimpleNamespace(get=lambda iid: {
        'id': 1, 'name': '10 GiB 流量', 'enabled': True, 'cost': 30,
        'amount': 10, 'unit': ' GiB', 'kind_label': '流量', 'kind': 'traffic', 'duration_days': 30,
    })
    bot._panel['1001'] = 11
    captured = []

    async def render(chat, *args, **kwargs):
        # _edit(chat, mid, body, keys), _show(chat, body, keys), or _rq_render(...)
        index = 2 if len(args) >= 4 else 1 if isinstance(args[0], int) else 0
        body = args[index]
        keys = args[index + 1] if len(args) > index + 1 else None
        captured.append((body, keys))
        return True

    async def forbidden(*args, **kwargs):
        raise AssertionError('Preview rendering must never use a transport')

    bot._edit = render
    bot._show = render
    bot._rq_render = render
    bot._call = forbidden
    bot._call_multipart = forbidden
    return bot, member, captured


def render_previews():
    bot, member, captured = demo_bot()
    result = {
        '用量卡': bot._usage_text(member),
        '账号卡': bot._account_card(member),
        '用量暂不可用': bot._usage_text({'emby_user_id': 'demo', 'quota_source': 'measured'}),
        '积分流水': bot._points_text(member),
        '群内帮助': bot._group_help_text(member),
        '行为准则': RULES_TEXT,
        '观影周报': ViewingReportPlugin._text('周报', 7, 12.345, 8, 35 * 1024**3, {}),
        '换绑审核': bot._rebind_card({'id': 7, 'status': 'pending', 'wanted_username': 'DemoViewer',
                                         'old_tg_user_id': '1001', 'tg_user_id': '1002'}),
    }

    async def cards():
        await bot._pw_confirm_card('1001', 11, {'purpose': 'reset', 'username': 'DemoViewer',
                                               'tg_id': '1001', 'password_mode': 'custom',
                                               'password': 'Demo<&123'})
        result['重置密码确认'] = captured[-1][0]
        await bot._rq_home('1001', 11, member)
        result['求片中心'] = captured[-1][0]
        await bot._shop_confirm('1001', 11, '1')
        result['兑换确认'] = captured[-1][0]
        await bot._cmd_rm('1001', 'demo-admin', ['DemoViewer'])
        result['删除确认'] = captured[-1][0]

    asyncio.run(cards())
    return result


def preview_document(cards):
    return '# Bot 回复预览\n\n合成 fixture，仅本地渲染；非真实账号，未发送。\n\n' + '\n\n'.join(
        f'## {title}\n\n```text\n{plain(body)}\n```' for title, body in cards.items()
    ) + '\n'


def test_checked_in_previews_are_rendered_by_current_templates():
    cards = render_previews()
    assert PREVIEW.read_text() == preview_document(cards)
    for title, text in cards.items():
        assert len(plain(text).encode('utf-16-le')) // 2 < 4096, title
    for name in ('用量卡', '账号卡', '用量暂不可用'):
        rows = plain(cards[name]).splitlines()
        # Budget 16 CJK cells in a narrow bubble; each value has its own row.
        assert len(rows) <= (18 if name == '账号卡' else 16)
        assert all(sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
                       for c in row) <= 32 for row in rows)
        for label in ('已用：', '剩余：', '近24小时：', '近30天：'):
            matching = [row for row in rows if row.startswith(label)]
            assert len(matching) == 1
            assert ' · ' not in matching[0]
    for text in cards.values():
        for forbidden in ('历史未核验', '核验起点', '统计口径', '进度正常前进', '采样'):
            assert forbidden not in text
    assert '旧密码立即失效' in cards['重置密码确认']
    assert '不可恢复' in cards['删除确认']
    assert '提交扣 1 次 · 关注免费' in cards['求片中心']
    assert '接受后等待下载入库' not in cards['求片中心']
    assert '账号权益不变' not in cards['重置密码确认']
    assert '需在客户端更新密码' in cards['重置密码确认']
    assert '遵守套餐的流量、带宽、同时播放限制' in cards['行为准则']
    assert '设备超出套餐' not in cards['行为准则']
    assert '限速或暂停播放' not in cards['行为准则']
    assert '等待下载' in ACCEPT_NOTICE


def test_unknown_values_never_become_zero_or_unlimited():
    assert '暂不可用' in '\n'.join(bandwidth_lines({}))
    quota = '\n'.join(quota_lines({'quota_source': 'measured'}))
    assert '计量暂不可用' in quota and '暂无法确认' in quota and '0 B' not in quota
    assert '不限' not in quota
    bot, _, _ = demo_bot()
    bot._stats = SimpleNamespace(watch_summary=lambda uid: {})
    body = bot._usage_text({'emby_user_id': 'demo', 'quota_source': 'measured'})
    assert '设备：暂不可用' in body and '同时播放：暂不可用' in body
    assert '近24小时：暂不可用' in body and '累计观看：暂不可用' in body
    assert '有效期：暂不可用' in body
    account = bot._account_card({'emby_user_id': 'demo'})
    assert '有效期：暂不可用' in account
    assert '同时播放：暂不可用' in account and '已登记设备：暂不可用' in account
    assert '累计观看：<b>暂不可用</b>' in account
    bot._points = SimpleNamespace(balance=lambda uid: (_ for _ in ()).throw(OSError()))
    assert '余额：<b>暂不可用</b>' in bot._points_text({'emby_user_id': 'demo'})
    assert '流水暂不可用' in bot._points_text({'emby_user_id': 'demo'})


@pytest.mark.parametrize('kbps,expected', [(125829, '125.8 Mbps'), (20000, '20.0 Mbps'),
                                           (999, '999 kbps'), (0, '不限'), (None, '暂不可用')])
def test_bandwidth_rounding_is_display_only(kbps, expected):
    member = {'bandwidth_limit_kbps': kbps}
    assert expected in '\n'.join(bandwidth_lines(member))
    assert member['bandwidth_limit_kbps'] == kbps


def test_card_and_password_html_escape_real_inputs():
    bot, member, captured = demo_bot()
    member.update(username='<Demo&😀>', group_name='<Group&>')
    card = bot._account_card(member)
    assert '&lt;Demo&amp;😀&gt;' in card and '&lt;Group&amp;&gt;' in card
    assert '<Demo&' not in card
    assert '<Demo&😀>' in plain(card)
    asyncio.run(bot._pw_confirm_card('1001', 11, {
        'purpose': 'reset', 'username': '<Demo&😀>', 'tg_id': '1001',
        'password_mode': 'custom', 'password': ' <&Demo> ',
    }))
    body = captured[-1][0]
    assert '<pre> &lt;&amp;Demo&gt; </pre>' in body
    assert ' <&Demo> ' in plain(body)


def test_rank_titles_split_without_breaking_html_entities_or_names():
    bot, _, _ = demo_bot()
    rows = [{'title': ('<&😀' * 1000), 'seconds': 3600, 'plays': i + 1} for i in range(10)]
    bot._stats = SimpleNamespace(top_titles_split=lambda **kw: (rows, rows))
    text = bot._rankings_text()
    parts = bot._split_bulletin(text)
    assert len(parts) > 1
    assert sum(plain(part).count('1小时') for part in parts) == 20
    assert '1小时0分' not in text
    assert all(len(part) <= 1000 and len(plain(part).encode('utf-16-le')) // 2 <= 1024 for part in parts)
    assert plain(text).count('…') == 20
    assert '…' in short_label('😀' * 80, 40)
    assert len(short_label('😀' * 80, 40).encode('utf-16-le')) // 2 <= 40


@pytest.mark.parametrize('seconds,expected', [
    (None, '暂不可用'), (0, '0秒'), (59, '59秒'), (60, '1分'), (3599, '59分'),
    (3600, '1小时'), (3660, '1小时1分'), (43200, '12小时'), (43260, '12小时1分'),
])
def test_duration_omits_zero_minutes_without_changing_input(seconds, expected):
    original = seconds
    assert duration(seconds) == expected
    assert seconds == original


def test_access_no_other_limits_is_short_but_real_limits_remain_visible():
    clear = TelegramBot._admin_access_text({'status': 'active', 'emby_disabled': False})
    assert '其他限制：无\n' in clear and '无额外到期' not in clear
    restricted = TelegramBot._admin_access_text({
        'status': 'suspended', 'emby_disabled': None,
        'remaining_restrictions': ['已过期', '<额度不足>'],
    })
    assert '已封禁' in restricted and '未知，请刷新核实' in restricted
    assert '其他限制：已过期、&lt;额度不足&gt;' in restricted
