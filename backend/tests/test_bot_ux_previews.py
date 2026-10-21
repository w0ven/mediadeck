"""Current user UX via real local handlers; no network or production accounts."""
import asyncio
import time
from pathlib import Path

from test_activity_bot import (  # noqa: F401
    bot_env,
    economy_env,
    env,
    find_action,
    interaction_env,
    message,
    private_command,
    tap,
)
from test_bot_reply_templates import plain
from test_economy import proof
from test_tg_interaction_context import VIEWER

from app.modules.economy_rules import DEFAULT_CARDS, economy_write
from app.modules.inventory import InventoryService

PREVIEW = Path(__file__).with_name('fixtures') / 'bot_ux_previews.md'


def render_ux_previews(e):
    pages = {}
    def page(title, text, keyboard=()):
        assert len(plain(text).encode('utf-16-le')) // 2 <= 4096
        for row in keyboard:
            assert len(row) <= 3
            for button in row:
                assert len(button.get('callback_data', '').encode()) <= 64
        pages[title] = (text, [[b['text'] for b in row] for row in keyboard])

    def snapshot(title, mid):
        item = e.tg.message(VIEWER, mid)
        page(title, e.tg.text(VIEWER, mid), item.get('reply_markup', {}).get('inline_keyboard', []))

    e.db.execute("INSERT INTO growth_ledger(emby_user_id,delta,reason,ref,created_at) VALUES('u1',120,'test','preview',?)", (int(time.time()),))
    item = e.services.shop.create(dict(DEFAULT_CARDS[0], enabled=True))
    with economy_write(e.db) as conn:
        InventoryService.add(conn, 'u1', DEFAULT_CARDS[0], 'preview', int(time.time()))
    before = e.services.points.balance('u1')

    async def run():
        mid = await private_command(e, '/start')
        snapshot('首页 · 无签到按钮', mid)
        await tap(e, 'me', mid)
        snapshot('我的账号 · 完整等级与直接保号入口', mid)
        await tap(e, 'activity:status', mid)
        snapshot('等级权益 · 无说明堆叠', mid)
        await tap(e, 'activity:renew', mid)
        snapshot('续期确认 · 金额与动作保留', mid)
        await tap(e, find_action(e, mid, 'activity:cancel:'), mid)
        await tap(e, 'activity:auto:on', mid)
        snapshot('自动保号 · 主动确认前不启用', mid)
        assert not e.members.get('u1')['auto_renew']
        await tap(e, 'bag', mid)
        snapshot('背包 · 物品与消费入口', mid)
        for action, title in [('inventory', '我的道具'), ('titles', '我的称号'),
                              ('shop', '兑换商城'), (f"buy:{item['id']}", '兑换确认'),
                              ('orders', '兑换记录 · 空状态'), ('points_rank', '积分榜')]:
            await tap(e, action, mid)
            snapshot(title, mid)
        assert e.services.points.balance('u1') == before
        await tap(e, 'home', mid)
        await e.bot._dispatch_update({'message':message('/checkin')})
        receipt = [p for method, p in e.tg.calls if method == 'sendMessage'][-1]
        page('签到回执 · 运气覆盖风险保留', receipt['text'], receipt.get('reply_markup', {}).get('inline_keyboard', []))
    asyncio.run(run())

    board = {'stamp': '10-07 截至当前', 'users': [{'tg_user_id':str(VIEWER), 'tg_display_name':'ViewerA', 'seconds':5400}],
             'movies': [{'title':'合成影片 <A&B>', 'seconds':5400, 'plays':3}],
             'shows': [{'title':'合成剧集', 'seconds':7200, 'plays':4}]}
    page('影片榜', e.bot._rankings_text(today=True, board=board))
    page('观影榜', e.bot._watch_rank_pages(today=True, board=board)[0])
    now = int(time.time())
    e.levels.activity.activate(now-300)
    proof(e.db, 'u1', now=now, seconds=300, key='preview-paid')
    e.levels.activity.settle(now)
    async def settled_account():
        mid = await private_command(e, '/me')
        snapshot('我的账号 · 今日观影奖励', mid)
    asyncio.run(settled_account())
    e.members.upsert('u1', '', {'group_id':'whitelist'})
    member = e.members.get('u1')
    member.update(quota_source='measured', measured_used_bytes=int(43.4*1024**3),
                  traffic_quota_bytes=0, bandwidth_limit_kbps=280000, max_streams=10,
                  allow_download=True, allow_transcode=True)
    page('白名单完整账号卡 · 无续期缺口', e.bot._account_card(member, public=True))
    page('/kk · 简卡保留管理状态', e.bot._user_card(member) + e.bot._admin_access_text({'status':'active','emby_disabled':False}))
    return pages


def preview_document(pages):
    lines = ['# 用户 Bot 统一展示预览', '',
             '真实源码模板/本地回调生成；合成账号及本地临时数据库，不是线上截图，未发送到 Telegram。',
             '等级名称采用源码默认配置（与本轮只读核对的现网名称一致）；仅模拟账号，不修改生产配置。', '']
    for title, (body, rows) in pages.items():
        lines += [f'## {title}', '', '```text', plain(body), '```', '', '按钮：']
        lines += ['- ' + ' ｜ '.join(row) for row in rows] or ['- 无']
        lines.append('')
    return '\n'.join(lines)


def test_current_user_ux_previews(request, monkeypatch):
    monkeypatch.setattr(time, 'time', lambda: 1791309600)
    e = request.getfixturevalue('env')
    pages = render_ux_previews(e)
    assert PREVIEW.read_text() == preview_document(pages)
    account, rows = pages['我的账号 · 完整等级与直接保号入口']
    assert '🗡 Lv2 游侠「一身侠气走江湖」' in account
    assert '观影奖励' not in account
    assert '今日观影奖励 +8 积分 · +5 成长' in pages['我的账号 · 今日观影奖励'][0]
    assert rows[0] == ['⏳ 续期 30 天', '🤖 开启自动保号']
    assert all('签到' not in label for body, rows in pages.values() for row in rows for label in row)
    assert all('删除候选' not in body and '保留至：' not in body for body, rows in pages.values())
    assert all(not any(text in body for text in ('80分钟', '有效日', '合格日', '资格已达标'))
               for body, rows in pages.values())
    assert not any(text in pages['白名单完整账号卡 · 无续期缺口'][0]
                   for text in ('续期', '缺口', '近30天核验', '有效日', '删除候选'))
