"""Approved whole-Bot flow, visible shared results and retired capabilities. Local only."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import time
import unicodedata
from pathlib import Path

import pytest
from test_bot_reply_templates import plain
from test_request_bot import body, card_with, cards, message, run, submit, tap
from test_request_bot import bot as request_bot  # noqa: F401

from app.core.db import Database
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.requests import RequestError, RequestService
from app.modules.telegram import TelegramBot

PREVIEW = Path(__file__).with_name('fixtures') / 'bot_flow_previews.md'


@pytest.fixture
def bot(request):
    return request.getfixturevalue('request_bot')


def visible(bot, chat, mid):
    item = bot.transport.messages[(str(chat), mid)]
    return item.get('text') or item.get('caption') or ''


def action_set(bot, chat='900'):
    return set(json.loads(cards(bot, chat)[0]['actions']))


def callbacks(rows):
    return {b.get('callback_data') for row in rows for b in row}


@pytest.mark.parametrize('roles', [[], ['uploader'], ['admin'], ['uploader', 'admin']])
def test_six_home_entries_role_boundary_and_compact_secondary_menus(bot, roles):
    bot.members.set_roles('u1', roles)
    _, rows = bot._home('900', 'Demo')
    assert callbacks(rows[:3]) == {'me', 'me_nodes', 'request_center', 'bag', 'rank', 'help'}
    assert all(len(r) == 2 for r in rows[:3])
    assert len(rows) == (4 if 'admin' in roles else 3)
    assert 'request_uploader' not in callbacks(rows)
    assert callbacks(bot.info_menu()) == {'devices', 'resetpw', 'rebind', 'me', 'home'}
    assert len(bot.info_menu()) == 3
    assert len(bot.bag_menu()) <= 3
    assert callbacks(bot.help_menu()) == {'rules', 'home'}
    message(bot, '/requests')
    assert ('list:staff' in action_set(bot)) == bool(roles)
    assert len(bot.transport.messages[('900', cards(bot)[0]['message_id'])]['reply_markup']['inline_keyboard']) <= 3


@pytest.mark.parametrize('status,actor,label', [
    ('accepted', 'up1', '已接受'), ('rejected', 'up1', '已拒绝'), ('cancelled', 'u1', '已撤回'),
])
def test_other_staff_old_card_workbench_and_persisted_group_notice_show_actual_result(bot, status, actor, label):
    row = submit(bot)
    old_b = card_with(bot, 'accept', '802')
    message(bot, '/uploader', '802')
    workbench = cards(bot, '802')[0]['message_id']
    # A persisted old shared group receipt: never infer unrecorded message IDs.
    bot.service.record_notice(row['id'], '-10055', 700)
    bot.transport.messages[('-10055', 700)] = {'text': '旧待处理求片', 'reply_markup': {'inline_keyboard': [[{'text': '旧接受', 'callback_data': 'req_claim:1'}]]}}
    if status == 'accepted':
        tap(bot, 'accept', '801')
    elif status == 'rejected':
        tap(bot, 'reject', '801'); tap(bot, 'rejectreason:source', '801')
    else:
        tap(bot, 'cancel'); tap(bot, 'cancelok')
    for chat, mid in [('802', old_b['message_id']), ('-10055', 700)]:
        text = visible(bot, chat, mid)
        assert text.startswith(f'<b>{ {"accepted":"✅", "rejected":"⛔", "cancelled":"↩"}[status] } 已处理 · {label}</b>')
        assert f'处理人：{actor}' in text and '处理时间：' in text
        keys = bot.transport.messages[(chat, mid)]['reply_markup']['inline_keyboard']
        assert not any(b.get('callback_data', '').endswith((':accept', ':reject')) for r in keys for b in r)
    assert '#1' not in visible(bot, '802', workbench)
    assert '待处理' in visible(bot, '802', workbench)
    tap(bot, 'accept', '802', card=old_b)
    answers = [p.get('text', '') for m, p in bot.transport.calls if m == 'answerCallbackQuery']
    assert any('未重复执行' in t and actor in t for t in answers)
    assert bot.service.get(1)['status'] == status
    assert len(bot.db.query("SELECT * FROM request_events WHERE kind IN ('accepted','rejected','cancelled')")) == 1
    if status == 'accepted':
        assert '等待下载并入库' in visible(bot, '-10055', 700)
        assert '已入库，可观看' not in visible(bot, '-10055', 700)
    message(bot, '/uploader', '802')
    assert 'view:1' not in action_set(bot, '802')


def test_failed_shared_refresh_keeps_locator_and_retryable_outbox_without_repeating_business(bot):
    submit(bot)
    original = card_with(bot, 'accept', '802')
    bot.service.record_notice(1, '-10055', 700)
    bot.transport.fail.update({'editMessageText', 'editMessageMedia', 'editMessageCaption'})
    bot.service.finish(1, 'up1', 'accepted', revision=1)
    run(bot.flush_request_notifications())
    job = bot.db.one("SELECT * FROM request_outbox WHERE kind='refresh'")
    assert job['state'] == 'pending'
    assert bot.service.notices(1)[0]['message_id'] == 700
    assert bot.db.one('SELECT * FROM request_cards WHERE token=?', (original['token'],))
    assert not bot.transport.messages[('802', original['message_id'])]['reply_markup']['inline_keyboard']
    tap(bot, 'accept', '802', card=original)
    assert bot.service.get(1)['claimed_by'] == 'up1'
    bot.transport.fail.clear()
    bot.service.retry_notifications(1)
    run(bot.flush_request_notifications())
    assert bot.db.one("SELECT * FROM request_outbox WHERE kind='refresh'")['state'] == 'sent'
    assert '处理人：up1' in visible(bot, '-10055', 700)
    assert len(bot.db.query("SELECT * FROM request_events WHERE kind='accepted'")) == 1
    assert len(bot.db.query("SELECT * FROM request_outbox WHERE kind='result'")) == 1


@pytest.mark.parametrize('action', ['ask', 'reply', 'internal', 'thread:0', 'correct', 'refund', 'retry', 'modify'])
def test_persisted_removed_callbacks_cannot_mutate_or_send_communication(bot, action):
    submit(bot)
    member = bot.members.get('up1' if action != 'reply' else 'u1')
    chat = '801' if action != 'reply' else '900'
    mid = cards(bot, chat)[0]['message_id']
    run(bot._rq_render(chat, mid, member, '旧卡', [[('旧按钮', action)]],
                       {'rid': 1, 'view': 'staff' if chat == '801' else 'user'}, rid=1, revision=1))
    old = card_with(bot, action, chat)
    before = bot.db.query('SELECT * FROM request_events')
    notices = bot.db.query('SELECT * FROM request_outbox')
    tap(bot, action, chat, card=old)
    assert '已停用' in body(bot, chat)
    assert bot.db.query('SELECT * FROM request_events') == before
    assert bot.db.query('SELECT * FROM request_outbox') == notices
    assert bot.service.used('u1') == 1


@pytest.mark.parametrize('kind', ['ask', 'reply', 'internal', 'correct', 'refund'])
def test_rebooted_communication_or_admin_input_is_retired_and_history_not_deleted(bot, kind):
    submit(bot)
    bot.service.message(1, 'up1', '历史可审计', internal=True)
    run(bot._rq_render('801', None, bot.members.get('up1'), '旧输入', [[('返回', 'home')]],
                       {'rid': 1, 'view': 'staff'}, rid=1, revision=1, input_kind=kind))
    before = bot.db.query('SELECT * FROM request_events')
    notices = bot.db.query('SELECT * FROM request_outbox')
    bot = bot.reboot()
    message(bot, '不得发送或修改', '801')
    assert bot.db.query('SELECT * FROM request_events') == before
    assert bot.db.query('SELECT * FROM request_outbox') == notices
    assert not bot.db.one("SELECT * FROM request_inputs WHERE chat_id='801'")
    assert any('已停用' in p.get('text', '') for m, p in bot.transport.calls if m in ('sendMessage','editMessageText'))


def test_lists_pagination_hidden_filters_search_and_input_cancel(bot):
    bot._groups.update('standard', {'request_quota': 0})
    for i in range(7):
        run(bot.service.create('u1', 'movie' if i % 2 else 'tv', 550+i))
    message(bot, '/uploader', '801')
    actions = action_set(bot, '801')
    assert 'page:1' in actions and 'page:-1' not in actions
    assert not any(a.startswith(('status:', 'filter:')) for a in actions)
    tap(bot, 'page:1', '801')
    assert 'page:-1' in action_set(bot, '801') and 'page:1' not in action_set(bot, '801')
    tap(bot, 'filters', '801')
    assert 'listclear' not in action_set(bot, '801')
    tap(bot, 'filtertypes', '801'); tap(bot, 'filter:movie', '801')
    assert len([a for a in action_set(bot, '801') if a.startswith('view:')]) == 3
    tap(bot, 'filters', '801'); assert 'listclear' in action_set(bot, '801')
    tap(bot, 'find', '801'); message(bot, '551', '801')
    assert [a for a in action_set(bot, '801') if a.startswith('view:')] == ['view:2']
    tap(bot, 'filters', '801'); tap(bot, 'find', '801'); message(bot, '/cancel', '801')
    assert not bot.db.one("SELECT * FROM request_inputs WHERE chat_id='801'")
    message(bot, '/requests')
    tap(bot, 'list:mine')
    assert '我的求片' in body(bot)
    message(bot, '/requests', '901'); tap(bot, 'list:mine', '901')
    assert not any(a.startswith('view:') for a in action_set(bot, '901'))


def test_existing_request_shown_directly_follow_free_and_readonly_when_terminal(bot):
    submit(bot)
    message(bot, '/requests', '901'); tap(bot, 'new', '901')
    message(bot, 'https://www.themoviedb.org/movie/550', '901')
    assert '#1' in body(bot, '901') and '待处理' in body(bot, '901')
    assert action_set(bot, '901') == {'follow', 'home'}
    tap(bot, 'follow', '901')
    assert bot.service.used('u2') == 0 and len(bot.service.list()) == 1
    assert action_set(bot, '901') == {'list:follow'}
    tap(bot, 'accept', '801')
    message(bot, '/requests', '901'); tap(bot, 'new', '901')
    message(bot, 'https://www.themoviedb.org/movie/550', '901')
    assert '已处理 · 已接受' in body(bot, '901')
    assert action_set(bot, '901') == {'home'}
    assert len(bot.service.list()) == 1 and bot.service.used('u1') == 1


def test_two_staff_simultaneous_callbacks_one_result_each_recipient_once(bot):
    submit(bot); bot.service.follow(1, 'u2')
    a, b = card_with(bot, 'accept', '801'), card_with(bot, 'accept', '802')
    async def race():
        await asyncio.gather(*(bot._rq_callback(f"rq:{c['token']}:accept", chat, c['message_id'], chat,
                                                bot.members.get(uid))
                               for c, chat, uid in [(a, '801', 'up1'), (b, '802', 'up2'), (a, '801', 'up1')]))
    run(race())
    assert len(bot.db.query("SELECT * FROM request_events WHERE kind='accepted'")) == 1
    assert len(bot.db.query("SELECT * FROM request_outbox WHERE kind='result'")) == 2
    assert bot.service.used('u1') == 1 and bot.service.used('u2') == 0
    winner = bot.service.get(1)['claimed_by']
    assert all(f'处理人：{winner}' in visible(bot, chat, c['message_id'])
               for chat, c in [('801', a), ('802', b)])


def test_independent_database_connections_competing_finish_compare_revision_in_transaction(bot):
    submit(bot)
    second_db = Database(bot.db.path)
    groups = GroupService(second_db)
    second = RequestService(second_db, MemberService(second_db, groups), groups)
    try:
        def finish(args):
            service, actor, status = args
            try:
                service.finish(1, actor, status, revision=1)
                return True
            except RequestError:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            assert sum(pool.map(finish, [(bot.service, 'up1', 'accepted'), (second, 'up2', 'rejected')])) == 1
        assert len(bot.db.query("SELECT * FROM request_events WHERE kind IN ('accepted','rejected')")) == 1
        assert len(bot.db.query("SELECT * FROM request_outbox WHERE kind='result'")) == 1
    finally:
        second_db.close()


@pytest.mark.parametrize('local,remote,expected', [
    ('active', False, 'admin_disable'), ('suspended', True, 'admin_enable'),
    ('suspended', False, 'admin_enable'), ('active', True, 'admin_enable'),
    ('pending', True, 'admin_enable'), ('unknown', True, 'admin_enable'),
    ('active', None, ''), ('suspended', None, ''), ('unknown', False, ''),
])
def test_management_main_releases_either_known_disable_source_and_never_guesses_unknown(local, remote, expected):
    assert TelegramBot._admin_access_action({'status': local, 'emby_disabled': remote}) == expected


def test_result_actor_comes_from_latest_status_event_not_stale_claim(bot):
    row = submit(bot)
    bot.service.finish(row['id'], 'up1', 'accepted', revision=1)
    bot.service.correct(row['id'], 'admin1', 'rejected', 'Web纠错', 2)
    text = bot._rq_body(bot.service.get(1))
    assert '处理人：admin1' in text and '处理人：up1' not in text
    assert '原因：Web纠错' not in text  # not a fabricated rejection reason
    bot.db.execute("DELETE FROM request_events WHERE request_id=1 AND kind IN ('accepted','correction')")
    assert '处理人 / 时间：暂不可用' in bot._rq_body(bot.service.get(1))


def test_title_link_escapes_text_and_validates_fixed_origin_and_numeric_identifier(bot):
    good = {'title': '<b>片名</b> & 引号"', 'tmdb_id': 550, 'media_type': 'movie', 'year': 2026}
    assert '&lt;b&gt;片名&lt;/b&gt; &amp;' in bot._rq_title(good)
    for kind, ident in [('javascript:', 550), ('movie', '550" onclick="bad'), ('movie', -1)]:
        assert '<a ' not in bot._rq_title(dict(good, media_type=kind, tmdb_id=ident))


def render_flow_previews(bot):
    """Run real handlers with the same local DB/transport fixture as lifecycle tests."""
    from test_bot_reply_templates import demo_bot

    from app.modules.telegram import RULES_TEXT

    result = {}
    def snapshot(title, chat='900', mid=None, *, listing=False):
        mid = mid or cards(bot, chat)[0]['message_id']
        item = bot.transport.messages[(chat, mid)]
        result[title] = {'body': visible(bot, chat, mid),
                         'rows': [[b['text'] for b in row] for row in item['reply_markup']['inline_keyboard']],
                         'photo': bool(item.get('caption')), 'listing': listing}
    def page(title, text, rows):
        result[title] = {'body': text, 'rows': [[b['text'] for b in row] for row in rows],
                         'photo': False, 'listing': False}
    for title, chat in [('普通首页', '900'), ('上片员首页', '801'), ('管理员首页', '800')]:
        text, rows = bot._home(chat, '合成用户')
        page(title, text, rows)
    demo, member, _ = demo_bot()
    member.update(allow_download=True, allow_transcode=False, note='请遵守套餐播放限制')
    page('我的账号 · 状态与用量合并', demo._account_card(member), demo.info_menu())
    page('帮助', bot._help_text(bot.members.get('u1')), bot.help_menu())
    page('使用准则', RULES_TEXT, [[{'text':'◀ 帮助'}]])
    bot.cfg.update(playback_lines=[{'label':'演示线路','url':'https://play.invalid'}], playback_lines_show_load=False)
    page('播放线路', run(bot._nodes_text()), bot.nodes_menu())
    message(bot, '/start')
    mid = bot._panel['900']
    run(bot._handle_callback({'id':'preview-bag', 'data':'bag', 'from':{'id':900},
                             'message':{'chat':{'id':900,'type':'private'},'message_id':mid}}))
    item = bot.transport.messages[('900', mid)]
    page('积分背包', item['text'], item['reply_markup']['inline_keyboard'])
    message(bot, '/requests'); snapshot('求片中心 · 普通用户')
    message(bot, '/requests', '801'); snapshot('求片中心 · 上片员', '801')
    message(bot, '/requests'); tap(bot, 'new'); message(bot, '庆州纪行')
    snapshot('选片 · 简短简介')
    tap(bot, 'searchfilters'); snapshot('选片筛选 · 无清除')
    tap(bot, 'candidates'); tap(bot, 'pick'); snapshot('电影确认 · 明确扣次')
    original_user = card_with(bot, 'submit')['message_id']
    tap(bot, 'submit')
    snapshot('本人待处理 · 操作后原卡', mid=original_user)
    bot.db.execute("UPDATE media_requests SET title='庆州纪行',year=2026 WHERE id=1")
    run(bot._rq_refresh(1)); snapshot('上片员待处理 · 仅接受拒绝', '801')
    other_staff = card_with(bot, 'accept', '802')
    message(bot, '/uploader', '802'); snapshot('上片员工作台 · 默认待处理', '802', listing=True)
    tap(bot, 'filters', '802'); snapshot('工作台筛选 · 低频收起', '802')
    old = card_with(bot, 'accept', '801')
    tap(bot, 'reject', '801', card=old); snapshot('拒绝 · 常用原因即完成', '801')
    bot.service.record_notice(1, '-10055', 700)
    tap(bot, 'rejectreason:source', '801'); snapshot('已拒绝 · 操作后原卡仅返回工作台', '801', old['message_id'])
    result_notice = bot.db.one("SELECT message_id FROM request_outbox WHERE request_id=1 AND user_id='u1' AND kind='result'")
    snapshot('异步已拒绝通知 · 独立投递记录', mid=result_notice['message_id'])
    snapshot('其他上片员原卡 · 已拒绝', '802', other_staff['message_id'])
    snapshot('已登记历史群原卡 · 已拒绝', '-10055', 700)
    message(bot, '/requests'); tap(bot, 'new'); message(bot, 'https://www.themoviedb.org/tv/1396')
    snapshot('剧集确认 · 全部季')
    tap(bot, 'selectseasons'); message(bot, '1,3,5-7'); snapshot('剧集确认 · 指定季')
    tap(bot, 'submit')
    accepted_card = card_with(bot, 'accept', '801')
    tap(bot, 'accept', '801'); snapshot('已接受 · 操作后原卡不等于已入库', '801', accepted_card['message_id'])
    accepted_notice = bot.db.one("SELECT message_id FROM request_outbox WHERE request_id=2 AND user_id='u1' AND kind='result'")
    snapshot('异步已接受通知 · 独立投递记录', mid=accepted_notice['message_id'])
    bot.service.correct(2, 'admin1', 'rejected', '合成后台纠正', 2)
    run(bot.flush_request_notifications())
    corrected_notice = bot.db.one("SELECT message_id FROM request_outbox WHERE request_id=2 AND user_id='u1' AND kind='result' ORDER BY id DESC LIMIT 1")
    snapshot('异步后台纠正通知 · 该次结果', mid=corrected_notice['message_id'])
    snapshot('异步原接受通知 · 后续纠正不改写旧事件', mid=accepted_notice['message_id'])
    message(bot, '/requests'); tap(bot, 'new'); message(bot, 'https://www.themoviedb.org/movie/560'); tap(bot, 'submit')
    message(bot, '/requests', '901'); tap(bot, 'new', '901'); message(bot, 'https://www.themoviedb.org/movie/560', '901')
    snapshot('已有原单 · 免费关注而非再确认扣次', '901')
    tap(bot, 'follow', '901'); snapshot('关注结果 · 原卡更新', '901')
    message(bot, '/requests'); tap(bot, 'list:mine'); tap(bot, 'view:3')
    cancelled_card = card_with(bot, 'cancel')
    tap(bot, 'cancel'); tap(bot, 'cancelok')
    snapshot('本人已撤回 · 操作后原卡只返回', mid=cancelled_card['message_id'])
    cancelled_notice = bot.db.one("SELECT message_id FROM request_outbox WHERE request_id=3 AND user_id='u1' AND kind='result'")
    snapshot('异步已撤回通知 · 独立投递记录', mid=cancelled_notice['message_id'])
    page('/kk · 正常账号', demo._user_card(member) + demo._admin_access_text({'status':'active','emby_disabled':False}),
         demo._user_admin_keyboard(member['emby_user_id'], {'status':'active','emby_disabled':False}))
    page('/kk · 已禁用账号', demo._user_card(dict(member, status='suspended')) + demo._admin_access_text({'status':'suspended','emby_disabled':True}),
         demo._user_admin_keyboard(member['emby_user_id'], {'status':'suspended','emby_disabled':True}))
    for title, local, remote in [('/kk · 仅Emby禁用可明确解除', 'active', True),
                                  ('/kk · 仅本地封禁可明确解除', 'suspended', False)]:
        page(title, demo._user_card(dict(member, status=local))
             + demo._admin_access_text({'status':local,'emby_disabled':remote}),
             demo._user_admin_keyboard(member['emby_user_id'], {'status':local,'emby_disabled':remote}))
    page('/kk · 远端未知只刷新', demo._user_card(member) + demo._admin_access_text({'status':'active','emby_disabled':None}),
         demo._user_admin_keyboard(member['emby_user_id'], {'status':'active','emby_disabled':None}))
    page('更多管理 · 私聊', '🛠 <b>更多管理</b>\n目标：DemoViewer', demo._user_admin_more(member['emby_user_id']))
    demo._bot_username = 'synthetic_preview_bot'
    with demo._bind_session('-10055', '800', group=True):
        page('更多管理 · 群内目标绑定深链', '🛠 <b>更多管理</b>\n目标：DemoViewer', demo._user_admin_more(member['emby_user_id']))
    return result


def flow_document(previews):
    lines = ['# Bot 全流程合成预览', '',
             '真实源模板与本地模拟回调生成；纯合成账号、媒体和消息 ID。无外发。',
             '日期固定；已接受仍表示等待下载入库。群卡仅演示已有持久回执的安全刷新。', '']
    for title, page in previews.items():
        lines += [f'## {title}', '', '```html', page['body'], '```', '', '按钮：']
        lines += ['- ' + ' ｜ '.join(row) for row in page['rows']] or ['- 无']
        lines.append('')
    return '\n'.join(lines)


def test_real_template_synthetic_preview_html_lengths_mobile_lines_and_button_budget(request, monkeypatch):
    monkeypatch.setattr(time, 'time', lambda: 1791309600)
    bot = request.getfixturevalue('bot')  # freeze before accounts are created, not after
    previews = render_flow_previews(bot)
    for title, page in previews.items():
        text = plain(page['body'])
        assert len(text.encode('utf-16-le')) // 2 <= (1024 if page['photo'] else 4096), title
        if not page['listing'] and title != '管理员首页':
            assert len(page['rows']) <= 3, title
        if any(x in title for x in ('已拒绝', '已接受', '已撤回', '本人待处理', '账号 · 状态')):
            for line in text.splitlines():
                assert sum(2 if unicodedata.east_asian_width(c) in ('W','F') else 1 for c in line) <= 32, (title, line)
        for row in page['rows']:
            assert all(len(b.encode('utf-16-le')) // 2 <= 55 for b in row), title
    assert PREVIEW.read_text() == flow_document(previews)


def test_long_rejection_refreshes_original_photo_with_visible_result_and_full_record_retained(bot):
    submit(bot)
    original = card_with(bot, 'accept', '802')
    note = '😀<&>' * 100
    bot.service.finish(1, 'up1', 'rejected', note=note, revision=1)
    run(bot.flush_request_notifications())
    text = visible(bot, '802', original['message_id'])
    assert '已处理 · 已拒绝' in text and '处理人：up1' in text
    assert '完整信息请从工单列表重新打开' in text
    assert len(plain(text).encode('utf-16-le')) // 2 <= 1024
    assert bot.service.get(1)['result_note'] == note
    assert not any(m in ('sendMessage','sendPhoto') and str(p.get('chat_id')) == '802'
                   for m, p in bot.transport.calls[4:] if '已处理' in p.get('text',''))
    message(bot, '/uploader', '802'); tap(bot, 'filters', '802'); tap(bot, 'status:rejected', '802'); tap(bot, 'view:1', '802')
    assert note in plain(body(bot, '802'))


def test_account_actually_merges_usage_fields_permissions_note_and_unknowns(bot):
    from test_bot_reply_templates import demo_bot
    local, member, _ = demo_bot()
    member.update(allow_download=True, allow_transcode=False, note='<合成备注>')
    card = plain(local._account_card(member))
    usage = plain(local._usage_text(member))
    for label in ('已用：', '剩余：', '带宽：', '同时播放：', '设备分组：', '近24小时：', '近30天：', '累计观看：'):
        value = next(line for line in usage.splitlines() if line.startswith(label))
        assert value in card and card.count(label) == 1
    assert '下载权限：允许' in card and '转码权限：不允许' in card and '备注：<合成备注>' in card
    assert '备注' not in plain(local._account_card(member, public=True))
    assert '统计口径' not in card and '历史未核验' not in card
    local._stats = None
    unknown = plain(local._account_card({'emby_user_id':'demo'}))
    for label in ('同时播放：', '设备分组：', '累计观看：'):
        assert label + '暂不可用' in unknown
    assert '累计观看：0秒' not in unknown and '同时播放：不限' not in unknown
    member['max_streams'] = 0
    assert '同时播放：不限' in plain(local._account_card(member))


@pytest.mark.parametrize('status,actor,label', [
    ('accepted','up1','已接受'), ('rejected','up1','已拒绝'), ('cancelled','u1','已撤回'),
])
def test_original_action_card_and_separate_async_result_receipt_are_both_visible(bot, status, actor, label):
    submit(bot)
    chat = '900' if status == 'cancelled' else '801'
    original = card_with(bot, 'cancel' if status == 'cancelled' else 'accept', chat)
    if status == 'accepted':
        tap(bot, 'accept', chat)
    elif status == 'rejected':
        tap(bot, 'reject', chat); tap(bot, 'rejectreason:source', chat)
    else:
        tap(bot, 'cancel', chat); tap(bot, 'cancelok', chat)
    direct = visible(bot, chat, original['message_id'])
    assert direct.startswith('<b>') and '已处理 · ' + label in direct.splitlines()[0]
    assert '<a href="https://www.themoviedb.org/movie/550">' in direct
    card = bot.db.one('SELECT * FROM request_cards WHERE chat_id=? AND message_id=?', (chat, original['message_id']))
    assert not json.loads(card['payload']).get('notice')
    assert json.loads(card['actions']) == ['list:mine' if status == 'cancelled' else 'list:staff']
    job = bot.db.one("SELECT * FROM request_outbox WHERE request_id=1 AND user_id='u1' AND kind='result'")
    assert job['state'] == 'sent' and job['message_id'] != original['message_id']
    receipt = visible(bot, '900', job['message_id'])
    assert receipt.startswith('<b>') and '已处理 · ' + label in receipt.splitlines()[0]
    assert '<a href="https://www.themoviedb.org/movie/550">' in receipt
    assert '处理人：' + actor in receipt and '处理时间：' in receipt
    assert '以上为本次处理结果' not in receipt and '状态已有更新' not in receipt
    assert bot.service.used('u1') == 1
    assert len(bot.db.query("SELECT * FROM request_outbox WHERE kind='result'")) == 1


def test_delayed_result_notifications_keep_exact_event_state_actor_time_and_original_reason(bot, monkeypatch):
    base = 1791309600
    monkeypatch.setattr(time, 'time', lambda: base)
    submit(bot)
    bot.service.finish(1, 'up1', 'rejected', note='原拒绝理由', revision=1)
    first = bot.db.one("SELECT * FROM request_outbox WHERE kind='result'")
    monkeypatch.setattr(time, 'time', lambda: base + 60)
    bot.service.correct(1, 'admin1', 'open', 'Web重新打开', 2)
    monkeypatch.setattr(time, 'time', lambda: base + 120)
    bot.service.finish(1, 'up2', 'accepted', revision=3)
    run(bot.flush_request_notifications())
    jobs = bot.db.query("SELECT * FROM request_outbox WHERE kind='result' ORDER BY id")
    expected = [('rejected','up1',base), ('open','admin1',base+60), ('accepted','up2',base+120)]
    for job, (status, actor, stamp) in zip(jobs, expected, strict=True):
        payload = json.loads(job['payload'])
        assert payload['status'] == status
        event = bot.db.one('SELECT * FROM request_events WHERE id=?', (payload['event_id'],))
        assert event['actor'] == actor and event['created_at'] == stamp
        text = visible(bot, '900', job['message_id'])
        assert ('状态已有更新' in text) == (status != 'accepted')
        assert '处理人：' + actor in text
        assert time.strftime('%Y-%m-%d %H:%M', time.localtime(stamp)) in text
        if status == 'rejected':
            assert text.startswith('<b>⛔ 已处理 · 已拒绝</b>') and '原因：原拒绝理由' in text
            assert '已接受' not in text
        elif status == 'open':
            assert text.startswith('<b>🟠 状态已纠正 · 待处理</b>') and '管理员已纠正' in text
        else:
            assert text.startswith('<b>✅ 已处理 · 已接受</b>') and '等待下载并入库' in text
    assert json.loads(first['payload']) == json.loads(jobs[0]['payload'])
    old_receipt = visible(bot, '900', jobs[0]['message_id'])
    run(bot._rq_refresh(1))
    assert visible(bot, '900', jobs[0]['message_id']) == old_receipt
    assert bot.service.get(1)['status'] == 'accepted' and bot.service.used('u1') == 1


def test_legacy_result_payload_without_event_reference_never_borrows_later_processor(bot):
    submit(bot)
    bot.service.finish(1, 'up1', 'accepted', revision=1)
    bot.service.correct(1, 'admin1', 'rejected', '后续纠正', 2)
    payload = {'status':'accepted','note':'','correction':False}
    text = bot._rq_result_notice(bot.service.get(1), payload)
    assert text.startswith('<b>✅ 已处理 · 已接受</b>')
    assert '处理人 / 时间：暂不可用' in text
    assert 'admin1' not in text and 'up1' not in text
    bot.db.execute("UPDATE request_outbox SET payload=? WHERE kind='result' AND id=(SELECT MIN(id) FROM request_outbox WHERE kind='result')",
                   (json.dumps(payload),))
    run(bot.flush_request_notifications())
    job = bot.db.one("SELECT * FROM request_outbox WHERE kind='result' ORDER BY id LIMIT 1")
    assert '已处理 · 已接受' in visible(bot, '900', job['message_id'])
    assert '处理人 / 时间：暂不可用' in visible(bot, '900', job['message_id'])


def test_queued_new_notice_at_terminal_delivery_does_not_claim_new_pending_request(bot):
    row = run(bot.service.create('u1', 'movie', 550))
    bot.service.finish(row['id'], 'up1', 'accepted', revision=1)
    run(bot.flush_request_notifications())
    for chat in ('801', '802'):
        text = body(bot, chat)
        assert text.startswith('<b>✅ 已处理 · 已接受</b>')
        assert '新求片' not in text and '处理人：up1' in text
        assert action_set(bot, chat) == {'list:staff'}
