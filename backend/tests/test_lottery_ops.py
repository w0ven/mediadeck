"""Lottery operations: rule visibility, winner DMs, live count, copy/delete/cancel,
member home, participation thresholds, reminders and silent pins.

Real SQLite/services/APIs; Telegram is always mocked, never contacted.
"""
import asyncio
import copy
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.core.db import Database
from app.main import app
from app.modules import lottery_identity
from app.modules.lottery import FAIRNESS, RULES, live_status
from app.modules.report_delivery import CALL_DELIVERY, failure
from app.modules.telegram import _CALL_ERROR, TelegramBot
from tests.test_lottery import (
    ADMIN,
    BASE,
    CHAT,
    GUEST,
    MEMBER,
    browser_id,
    config,
    create,
    draw,
    join,
    req,
)
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_miniapp import headers

FORBIDDEN = ('防小号', 'IP', '浏览器', '设备', '关联')
OTHER_CHAT = '-1009876543210'
BOT_ID = 123456


@pytest.fixture
def e(request, monkeypatch):
    env = request.getfixturevalue('lottery_env')
    base = TelegramBot._call
    env.script = {}

    async def fake(self, method, payload=None, **kw):
        payload = payload or {}
        queue = env.script.get((method, str(payload.get('chat_id')))) or env.script.get(method)
        if queue:
            env.calls.append((method, copy.deepcopy(payload)))
            result = queue.pop(0)
            CALL_DELIVERY.set(None)
            _CALL_ERROR.set('')
            if result == 'timeout':
                CALL_DELIVERY.set(failure(exc=httpx.ReadTimeout('timeout')))
                _CALL_ERROR.set('timeout')
                return None
            if isinstance(result, dict) and 'error_code' in result:
                CALL_DELIVERY.set(failure(result))
                _CALL_ERROR.set(result.get('description', ''))
                return None
            return result
        if method in ('pinChatMessage', 'unpinChatMessage', 'deleteMessage'):
            env.calls.append((method, copy.deepcopy(payload)))
            CALL_DELIVERY.set(None)
            _CALL_ERROR.set('')
            return True
        return await base(self, method, payload, **kw)

    monkeypatch.setattr(TelegramBot, '_call', fake)
    return env


def sent(e, method, chat=None):
    return [p for m, p in e.calls if m == method and (chat is None or str(p.get('chat_id')) == str(chat))]


def notice(e, lid, kind):
    return e.service.db.one('SELECT * FROM lottery_notices WHERE lottery_id=? AND kind=?', (lid, kind))


def op(e, lid, kind):
    return e.service.db.one('SELECT * FROM lottery_ops WHERE lottery_id=? AND kind=?', (lid, kind))


def fund(e, uid, amount):
    e.service.points.add(uid, amount, 'admin.mint', f'mock-ops-test-{uid}-{amount}-{time.time_ns()}')


def ops(e, lid=None):
    asyncio.run(e.service.deliver_ops(lid))


def store(e, lid, **changes):
    c = e.service.row(lid)['config']
    c.update(changes)
    e.service.db.execute('UPDATE lotteries SET config_json=? WHERE id=?', (json.dumps(c, ensure_ascii=False), lid))


def strip_new_fields(e, lid):
    """Turn an activity into one published before this upgrade (fields absent)."""
    raw = json.loads(e.service.db.one('SELECT config_json FROM lotteries WHERE id=?', (lid,))['config_json'])
    for key in ('min_level', 'min_points', 'min_watch_hours', 'reminder', 'pin', 'winner_dm', 'live_count'):
        raw.pop(key, None)
    e.service.db.execute('UPDATE lotteries SET config_json=? WHERE id=?', (json.dumps(raw, ensure_ascii=False), lid))
    e.service.db.execute('DELETE FROM lottery_ops WHERE lottery_id=?', (lid,))
    return raw


def no_forbidden(text):
    return not any(word in text for word in FORBIDDEN)


def enable_levels(e, uid='u2', growth=400):
    levels = app.state.levels
    cfg = levels.config()
    cfg['config']['enabled'] = True
    levels.save_config(cfg['config'], cfg['revision'])
    e.service.db.execute('INSERT OR IGNORE INTO activity_members(emby_user_id,enrolled_at) VALUES(?,?)',
                         (uid, int(time.time())))
    e.service.db.execute("INSERT INTO growth_ledger(emby_user_id,delta,reason,ref,created_at) VALUES(?,?,'test',?,?)",
                         (uid, growth, f'seed-{uid}', int(time.time())))


# --- 1. rule visibility ----------------------------------------------------

@pytest.mark.parametrize('anti_multi', [True, False])
def test_public_rules_identical_for_admin_and_member_and_admin_rules_admin_only(e, anti_multi):
    lid = create(e, config(anti_multi=anti_multi, reminder=True))
    member, admin = req(e, 'GET', '/' + lid, MEMBER).json(), req(e, 'GET', '/' + lid, ADMIN).json()
    guest = req(e, 'GET', '/' + lid, GUEST).json()
    assert member['rules'] == admin['rules'] == guest['rules']
    for view in (member, guest):
        assert 'admin_rules' not in view
        for key in ('qualification', 'excluded_count', 'candidate_count', 'notices', 'operations', 'awards'):
            assert key not in view
        for key in ('chat_id', 'thread_id', 'anti_multi', 'reminder', 'pin', 'winner_dm', 'live_count'):
            assert key not in view['config']
    expected = lottery_identity.RULE if anti_multi else lottery_identity.RULE_OFF
    assert admin['admin_rules'] == expected
    if anti_multi:
        assert '关联账号全部不中奖' in expected and '记录页' in expected and len(expected) < 60
    joined = join(e, lid, MEMBER)
    assert 'admin_rules' not in joined and joined['rules'] == member['rules']
    preview = req(e, 'POST', f'/{lid}/preview').json()
    texts = [member['rules'], admin['rules'], preview['rules'], preview['text'], notice(e, lid, 'launch')['payload_json']]
    for text in texts:
        assert no_forbidden(text), text
    draw(e, lid)
    assert no_forbidden(notice(e, lid, 'result')['payload_json'])


def test_fairness_statement_public_and_only_on_paid_launch(e):
    assert RULES.endswith(FAIRNESS) and FAIRNESS == '发现多账号等违规参与，将取消中奖资格，报名费不退。'
    free = create(e)
    fund(e, 'u2', 100)
    paid = create(e, config(entry_fee=40))
    for lid in (free, paid):
        for user in (MEMBER, GUEST, ADMIN):
            rules = req(e, 'GET', '/' + lid, user).json()['rules']
            assert rules.endswith(FAIRNESS) and rules.count(FAIRNESS) == 1
        assert req(e, 'POST', f'/{lid}/preview').json()['rules'].endswith(FAIRNESS)
    free_text = json.loads(notice(e, free, 'launch')['payload_json'])['text']
    paid_text = json.loads(notice(e, paid, 'launch')['payload_json'])['text']
    assert FAIRNESS not in free_text
    assert '报名 40 积分 · 成功报名不退，取消活动全额退还' in paid_text and FAIRNESS + '</i>' in paid_text
    for text in (free_text, paid_text):
        assert no_forbidden(text)


# --- a. winner DMs -----------------------------------------------------------

def test_winner_dm_short_native_deep_link_without_private_instructions(e):
    secret = 'PRIVATE-CLAIM-NOTE-xyz'
    prizes = [{'kind': 'manual', 'name': '周边礼包', 'quantity': 2, 'instructions': secret}]
    lid = create(e, config(prizes=prizes))
    join(e, lid, MEMBER)
    join(e, lid, GUEST)
    result = draw(e, lid)
    dms = [p for p in sent(e, 'sendMessage') if str(p['chat_id']) in (str(MEMBER), str(GUEST))]
    assert len(dms) == 2
    for dm in dms:
        assert '星光福利' in dm['text'] and '周边礼包' in dm['text'] and secret not in dm['text']
        assert '<tg-emoji emoji-id=' in dm['text'] and 'message_thread_id' not in dm
        button = dm['reply_markup']['inline_keyboard'][0][0]
        assert button['url'] == f'https://t.me/example_lottery_bot?startapp=lottery_{lid}'
        assert button['icon_custom_emoji_id'] and button['text'] == '查看并领取'
    assert {a['dm']['state'] for a in result['awards']} == {'sent'}
    member = req(e, 'GET', '/' + lid, MEMBER).json()
    assert 'dm' not in member['my_award'] and 'awards' not in member
    assert result['operations']['winner_dm'] == {'enabled': True, 'summary': {'sent': 2}}
    # Idempotent: no second DM for the same award.
    e.service.draw(lid)
    asyncio.run(e.service.deliver_dms(lid))
    assert len([p for p in sent(e, 'sendMessage') if str(p['chat_id']) in (str(MEMBER), str(GUEST))]) == 2


def test_winner_dm_unreachable_unknown_and_rate_limit_semantics(e):
    lid = create(e, config(prizes=[{'kind': 'points', 'name': '500 积分', 'quantity': 3, 'amount': 500}]))
    for user in (MEMBER, GUEST, ADMIN):
        join(e, lid, user)
    e.script[('sendMessage', str(GUEST))] = [{'ok': False, 'error_code': 403, 'description': 'Forbidden: bot was blocked by the user'}]
    e.script[('sendMessage', str(MEMBER))] = ['timeout']
    e.script[('sendMessage', str(ADMIN))] = [{'ok': False, 'error_code': 429, 'description': 'Too Many Requests',
                                             'parameters': {'retry_after': 5}}]
    result = draw(e, lid)
    owners = {r['id']: r['tg_id'] for r in e.service.db.query('SELECT id,tg_id FROM lottery_awards')}
    states = {owners[a['id']]: a['dm'] for a in result['awards']}
    assert states[str(GUEST)]['state'] == 'unreachable' and '无法私信' in states[str(GUEST)]['error']
    assert states[str(MEMBER)]['state'] == 'unknown' and '未自动重发' in states[str(MEMBER)]['error']
    assert states[str(ADMIN)]['state'] == 'pending' and states[str(ADMIN)]['error'] == 'Telegram 限流'
    # DM failures never change winners or award delivery.
    assert {a['state'] for a in result['awards']} <= {'delivered', 'binding'}
    assert len(e.service.db.query('SELECT * FROM lottery_awards WHERE lottery_id=?', (lid,))) == 3
    for _ in range(2):
        asyncio.run(e.service.deliver_dms(lid))
    assert len(sent(e, 'sendMessage', GUEST)) == 1 and len(sent(e, 'sendMessage', MEMBER)) == 1
    assert len(sent(e, 'sendMessage', ADMIN)) == 1  # retry waits for next_at
    e.service.db.execute("UPDATE lottery_dms SET next_at=0 WHERE tg_id=?", (str(ADMIN),))
    asyncio.run(e.service.deliver_dms(lid))
    assert e.service.db.one('SELECT state FROM lottery_dms WHERE tg_id=?', (str(ADMIN),))['state'] == 'sent'


def test_winner_dm_paused_while_bot_disabled_and_expired_lease_not_resent(e, monkeypatch):
    lid = create(e)
    join(e, lid, MEMBER)
    original = TelegramBot._cfg
    monkeypatch.setattr(TelegramBot, '_cfg', lambda self: {**original(self), 'enabled': False})
    e.service.draw(lid)
    asyncio.run(e.service.deliver_dms(lid))
    dm = e.service.db.one('SELECT * FROM lottery_dms WHERE lottery_id=?', (lid,))
    assert dm['state'] == 'pending' and 'Bot 暂未启用' in dm['error'] and not sent(e, 'sendMessage', MEMBER)
    monkeypatch.setattr(TelegramBot, '_cfg', original)
    e.service.db.execute("UPDATE lottery_dms SET state='sending',updated_at=? WHERE id=?", (int(time.time()) - 600, dm['id']))
    asyncio.run(e.service.deliver_dms(lid))
    assert e.service.db.one('SELECT state FROM lottery_dms WHERE id=?', (dm['id'],))['state'] == 'unknown'
    assert not sent(e, 'sendMessage', MEMBER)


# --- b. live entry count -----------------------------------------------------

def test_live_count_edits_on_change_with_interval_and_final_edit(e):
    lid = create(e)
    launch = notice(e, lid, 'launch')
    join(e, lid, MEMBER)
    ops(e, lid)
    edits = sent(e, 'editMessageText')
    assert len(edits) == 1 and edits[0]['message_id'] == launch['message_id']
    # The count joins the title line (what a pinned-message bar shows); the body is unchanged.
    title, body = json.loads(launch['payload_json'])['text'].split('\n', 1)
    assert edits[0]['text'] == f'{title}  {live_status("open:1", {})}\n{body}' and edits[0]['reply_markup']['inline_keyboard']
    ops(e, lid)  # unchanged count: no edit
    join(e, lid, GUEST)
    ops(e, lid)  # changed, but within 30 seconds
    assert len(sent(e, 'editMessageText')) == 1
    e.service.db.execute("UPDATE lottery_ops SET updated_at=updated_at-31 WHERE lottery_id=? AND kind='live'", (lid,))
    ops(e, lid)
    assert sent(e, 'editMessageText')[-1]['text'].split('\n')[0] == f'{title}  {live_status("open:2", {})}'
    join(e, lid, ADMIN)
    result = draw(e, lid)  # final edit ignores the interval
    assert sent(e, 'editMessageText')[-1]['text'].split('\n')[0] == f'{title}  {live_status("settled:3", {})}'
    assert result['operations']['live']['state'] == 'sent'
    assert result['operations']['live']['shown'] == '已开奖 · 3 人参与'
    count = len(sent(e, 'editMessageText'))
    ops(e, lid)
    assert len(sent(e, 'editMessageText')) == count
    # Public total, same number as the ordinary view.
    assert req(e, 'GET', '/' + lid, MEMBER).json()['count'] == 3


def test_live_count_not_modified_is_success_and_unknown_is_not_repeated(e):
    lid = create(e)
    join(e, lid, MEMBER)
    e.script['editMessageText'] = [{'ok': False, 'error_code': 400, 'description': 'Bad Request: message is not modified'}]
    ops(e, lid)
    assert op(e, lid, 'live')['state'] == 'sent' and op(e, lid, 'live')['shown'] == 'open:1'
    join(e, lid, GUEST)
    e.service.db.execute("UPDATE lottery_ops SET updated_at=0 WHERE lottery_id=? AND kind='live'", (lid,))
    e.script['editMessageText'] = ['timeout']
    ops(e, lid)
    assert op(e, lid, 'live')['state'] == 'unknown' and op(e, lid, 'live')['target'] == 'open:2'
    count = len(sent(e, 'editMessageText'))
    e.service.db.execute("UPDATE lottery_ops SET updated_at=0 WHERE lottery_id=? AND kind='live'", (lid,))
    ops(e, lid)
    assert len(sent(e, 'editMessageText')) == count  # the same uncertain edit is never replayed
    e.script['editMessageText'] = [{'ok': False, 'error_code': 400, 'description': 'Bad Request: not enough rights'}]
    join(e, lid, ADMIN)
    e.service.db.execute("UPDATE lottery_ops SET updated_at=0 WHERE lottery_id=? AND kind='live'", (lid,))
    ops(e, lid)
    assert op(e, lid, 'live')['state'] == 'failed'
    count = len(sent(e, 'editMessageText'))
    draw(e, lid)
    assert len(sent(e, 'editMessageText')) == count  # a rejected editor is not retried
    assert e.service.row(lid)['state'] == 'settled'


def test_live_count_only_for_confirmed_launch_message(e):
    e.replies.append({'ok': False, 'error_code': 400, 'description': 'Bad Request: chat not found'})
    lid = create(e)
    assert notice(e, lid, 'launch')['state'] == 'failed' and notice(e, lid, 'launch')['message_id'] is None
    join(e, lid, MEMBER)
    ops(e, lid)
    assert not sent(e, 'editMessageText') and not sent(e, 'pinChatMessage') and op(e, lid, 'live') is None


# --- c. copy -----------------------------------------------------------------

def test_copy_creates_draft_with_cleared_deadline_and_audit(e):
    fund(e, 'u2', 100)
    source = create(e, config(entry_fee=30, audience='member', min_points=10, reminder=False, pin=True,
                              anti_multi=False, draw_mode='timed'))
    result = req(e, 'POST', f'/{source}/copy')
    assert result.status_code == 200, result.text
    draft = result.json()
    assert draft['id'] != source and draft['state'] == 'draft' and draft['revision'] == 1
    c, old = draft['config'], e.service.row(source)['config']
    assert c['deadline'] is None
    for key in ('title', 'audience', 'entry_fee', 'min_level', 'min_points', 'min_watch_hours', 'reminder', 'pin',
                'anti_multi', 'draw_mode', 'thread_id', 'chat_id'):
        assert c[key] == old[key], key
    assert c['prizes'] == old['prizes']
    assert 'winner_dm' not in c and 'live_count' not in c
    assert req(e, 'POST', f'/{draft["id"]}/publish', json={'revision': 1}).json()['detail'] == '定时开奖须填写截止时间'
    assert req(e, 'PUT', '/' + draft['id'], json={**c, 'revision': 1}).status_code == 400
    saved = req(e, 'PUT', '/' + draft['id'], json={**c, 'deadline': int(time.time()) + 7200, 'revision': 1})
    assert saved.status_code == 200
    assert req(e, 'POST', f'/{draft["id"]}/publish', json={'revision': 2}).json()['state'] == 'published'
    audit = e.service.db.one("SELECT * FROM audit_log WHERE action='lottery.copy'")
    assert audit['subject'] == draft['id'] and audit['detail'] == f'from={source}' and audit['actor'] == str(ADMIN)
    assert req(e, 'POST', f'/{source}/copy', MEMBER).status_code == 403


def test_copy_revalidates_shop_prizes_and_legacy_switches_copy_as_off(e):
    item = next(i for i in e.service.shop.items() if i['kind'] == 'invite_card')
    source = create(e, config(prizes=[{'kind': 'shop', 'name': item['name'], 'quantity': 1, 'item_id': item['id']}]))
    strip_new_fields(e, source)
    draft = req(e, 'POST', f'/{source}/copy').json()
    assert draft['config']['reminder'] is False and draft['config']['pin'] is False
    assert draft['config']['min_points'] == 0 and draft['config']['prizes'][0]['spec']['id'] == item['id']
    e.service.db.execute('UPDATE shop_items SET enabled=0 WHERE id=?', (item['id'],))
    result = req(e, 'POST', f'/{source}/copy')
    assert result.status_code == 400 and '已下架' in result.json()['detail']
    assert len(e.service.db.query("SELECT * FROM lotteries WHERE state='draft'")) == 1


def test_manual_draw_without_deadline_stays_open_until_drawn(e):
    rejected = req(e, 'POST', '/admin/drafts', json=config(deadline=None))
    assert rejected.status_code == 400 and rejected.json()['detail'] == '定时开奖须填写截止时间'
    lid = create(e, config(draw_mode='manual', deadline=None))
    launch = json.loads(notice(e, lid, 'launch')['payload_json'])['text']
    assert '管理员开奖 · 开奖前均可报名' in launch and '截止' not in launch
    join(e, lid, MEMBER)
    assert lid in {i['id'] for i in req(e, 'GET', '/ongoing', MEMBER).json()['items']}
    e.service.stage_reminders(now=int(time.time()) + 400 * 86400)
    assert notice(e, lid, 'reminder') is None
    assert req(e, 'GET', '/' + lid).json()['operations']['reminder']['state'] == 'off'
    ops(e, lid)
    assert sent(e, 'editMessageText')[-1]['text'].split('\n')[0].endswith(live_status('open:1', {}))
    asyncio.run(e.service.tick())  # never drawn automatically
    assert e.service.row(lid)['state'] == 'published'
    join(e, lid, GUEST)
    draw(e, lid)
    assert e.service.row(lid)['state'] == 'settled'
    late = req(e, 'POST', f'/{lid}/join', ADMIN)
    assert late.status_code == 400 and lid not in {i['id'] for i in req(e, 'GET', '/ongoing', MEMBER).json()['items']}


# --- d. delete / cancel --------------------------------------------------------

def test_delete_only_confirmed_drafts_with_audit(e):
    lid = create(e, publish=False)
    for body in ({}, {'confirm': 'true'}, {'confirm': 1}):
        assert req(e, 'POST', f'/{lid}/delete', json=body).status_code == 400
    assert req(e, 'POST', f'/{lid}/delete', MEMBER, json={'confirm': True}).status_code == 403
    assert req(e, 'POST', f'/{lid}/delete', json={'confirm': True}).json() == {'deleted': True, 'id': lid}
    assert req(e, 'GET', '/' + lid).status_code == 404
    assert req(e, 'POST', f'/{lid}/delete', json={'confirm': True}).status_code == 404
    assert e.service.db.one("SELECT * FROM audit_log WHERE action='lottery.delete'")['subject'] == lid
    published = create(e)
    result = req(e, 'POST', f'/{published}/delete', json={'confirm': True})
    assert result.status_code == 400 and '取消' in result.json()['detail']


def test_cancel_paid_refunds_once_notifies_unpins_and_blocks_entry_and_draw(e):
    fund(e, 'u1', 100)
    fund(e, 'u2', 100)
    lid = create(e, config(entry_fee=40))
    launch = notice(e, lid, 'launch')
    for user in (MEMBER, ADMIN):
        assert req(e, 'POST', f'/{lid}/join', user, json={'browser_id': browser_id(user),
                                                         'expected_entry_fee': 40}).status_code == 200
    assert e.service.points.balance('u2') == 60 and e.service.points.balance('u1') == 60
    assert req(e, 'POST', f'/{lid}/cancel', json={}).status_code == 400
    assert req(e, 'POST', f'/{lid}/cancel', MEMBER, json={'confirm': True}).status_code == 403
    result = req(e, 'POST', f'/{lid}/cancel', json={'confirm': True}).json()
    assert result['state'] == 'cancelled' and result['cancelled_at']
    assert result['refunds'] == {'count': 2, 'points': 80}
    assert e.service.points.balance('u2') == 100 and e.service.points.balance('u1') == 100
    refunds = e.service.db.query("SELECT * FROM points_ledger WHERE reason='lottery.refund' ORDER BY ref")
    assert sorted(r['ref'] for r in refunds) == sorted(f'lottery.refund:{lid}:{t}' for t in (MEMBER, ADMIN))
    # Short cancel notice replies to the launch notice; no forbidden wording.
    cancel = [p for p in sent(e, 'sendMessage') if '本期不再报名和开奖' in p['text']]
    assert len(cancel) == 1 and cancel[0]['reply_parameters']['message_id'] == launch['message_id']
    assert '全额退还' in cancel[0]['text'] and no_forbidden(cancel[0]['text']) and 'reply_markup' not in cancel[0]
    assert sent(e, 'unpinChatMessage') == [{'chat_id': CHAT, 'message_id': launch['message_id']}]
    assert sent(e, 'editMessageText')[-1]['text'].split('\n')[0].endswith(live_status('cancelled:2', {'entry_fee': 40}))
    # Repeated cancel: success no-op, no second refund, no second notice.
    assert req(e, 'POST', f'/{lid}/cancel', json={'confirm': True}).status_code == 200
    assert len(e.service.db.query("SELECT * FROM points_ledger WHERE reason='lottery.refund'")) == 2
    assert len([p for p in sent(e, 'sendMessage') if '本期不再报名和开奖' in p['text']]) == 1
    joined = req(e, 'POST', f'/{lid}/join', GUEST)
    assert joined.status_code == 400 and '已取消' in joined.json()['detail']
    assert req(e, 'POST', f'/{lid}/draw').json()['detail'] == '活动已取消，不能开奖'
    store(e, lid, deadline=int(time.time()) - 1)
    asyncio.run(e.service.tick())
    assert e.service.row(lid)['state'] == 'cancelled' and not e.service.db.query('SELECT * FROM lottery_awards')
    assert e.service.db.one("SELECT * FROM audit_log WHERE action='lottery.cancel'")['detail'] == 'refunds=2 points=80'
    assert {r['outcome'] for r in e.service.records(lid)['entries']} == {'cancelled'}
    assert 'refunds' not in req(e, 'GET', '/' + lid, MEMBER).json()


def test_cancel_rules_settled_draft_free_and_unsent_launch(e):
    settled = create(e)
    draw(e, settled)
    assert req(e, 'POST', f'/{settled}/cancel', json={'confirm': True}).json()['detail'] == '已开奖的活动不能取消'
    draft = create(e, publish=False)
    assert '删除草稿' in req(e, 'POST', f'/{draft}/cancel', json={'confirm': True}).json()['detail']
    free = create(e)
    join(e, free, GUEST)
    assert req(e, 'POST', f'/{free}/cancel', json={'confirm': True}).json()['refunds'] == {'count': 0, 'points': 0}
    text = next(p for p in sent(e, 'sendMessage') if '本期不再报名和开奖' in p['text'])['text']
    assert '退还' not in text
    e.replies.append({'ok': False, 'error_code': 400, 'description': 'Bad Request: chat not found'})
    unsent = create(e)
    assert notice(e, unsent, 'launch')['state'] == 'failed'
    req(e, 'POST', f'/{unsent}/cancel', json={'confirm': True})
    assert notice(e, unsent, 'cancel') is None  # the group never saw this activity


def test_concurrent_cancels_refund_exactly_once_across_connections(e):
    fund(e, 'u2', 100)
    lid = create(e, config(entry_fee=25))
    assert req(e, 'POST', f'/{lid}/join', MEMBER, json={'browser_id': browser_id(MEMBER),
                                                       'expected_entry_fee': 25}).status_code == 200
    other = copy.copy(e.service)
    other.db = Database(e.service.db.path)
    gate = threading.Barrier(8)

    def cancel(service):
        gate.wait()
        return service.cancel(lid, 'test', confirm=True)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(cancel, [e.service, other] * 4))
    finally:
        other.db.close()
    assert results.count(True) == 1
    assert len(e.service.db.query("SELECT * FROM points_ledger WHERE reason='lottery.refund'")) == 1
    assert e.service.points.balance('u2') == 100


def race_cancel_and_draw(e, lid):
    other = copy.copy(e.service)
    other.db = Database(e.service.db.path)
    gate = threading.Barrier(2)

    def attempt(action):
        gate.wait()
        try:
            action()
            return 'ok'
        except ValueError as exc:
            return str(exc)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(attempt, [lambda: e.service.cancel(lid, 'a', confirm=True),
                                           lambda: other.draw(lid, 'b')]))
    finally:
        other.db.close()


def test_cancel_and_draw_are_mutually_exclusive(e):
    fund(e, 'u2', 500)
    outcomes = set()
    for _ in range(6):
        lid = create(e, config(entry_fee=10))
        assert req(e, 'POST', f'/{lid}/join', MEMBER, json={'browser_id': browser_id(MEMBER),
                                                           'expected_entry_fee': 10}).status_code == 200
        results = race_cancel_and_draw(e, lid)
        state = e.service.row(lid)['state']
        awards = e.service.db.query('SELECT * FROM lottery_awards WHERE lottery_id=?', (lid,))
        refunds = e.service.db.query("SELECT * FROM points_ledger WHERE reason='lottery.refund' AND ref LIKE ?",
                                     (f'lottery.refund:{lid}:%',))
        if state == 'cancelled':
            assert results == ['ok', '活动已取消，不能开奖'] and not awards and len(refunds) == 1
        else:
            assert state == 'settled' and results == ['已开奖的活动不能取消', 'ok'] and awards and not refunds
        outcomes.add(state)
    assert outcomes <= {'cancelled', 'settled'}


# --- e. member home -------------------------------------------------------------

def test_ongoing_lists_open_activities_locally_without_internal_fields(e):
    open_one = create(e, config(title='进行中一', entry_fee=5, prizes=[
        {'kind': 'points', 'name': f'奖{i}', 'quantity': 1, 'amount': 5} for i in range(4)]))
    joined = create(e, config(title='已报名'))
    expired = create(e, config(title='已截止'))
    cancelled = create(e, config(title='已取消'))
    settled = create(e, config(title='已开奖'))
    create(e, config(title='草稿'), publish=False)
    join(e, joined, GUEST)
    store(e, expired, deadline=int(time.time()) - 1)
    req(e, 'POST', f'/{cancelled}/cancel', json={'confirm': True})
    draw(e, settled)
    before = len(e.calls)
    for user in (GUEST, MEMBER):
        result = req(e, 'GET', '/ongoing', user)
        assert result.status_code == 200
        items = {i['id']: i for i in result.json()['items']}
        assert set(items) == {open_one, joined}
        assert items[joined]['joined'] is (user == GUEST) and items[open_one]['joined'] is False
        item = items[open_one]
        assert set(item) == {'id', 'title', 'deadline', 'audience', 'entry_fee', 'joined', 'prizes', 'prize_summary', 'slots'}
        assert item['entry_fee'] == 5 and item['slots'] == 4 and item['prize_summary'] == '奖0 ×1、奖1 ×1、奖2 ×1 等 4 项'
        assert CHAT not in json.dumps(result.json()) and 'anti_multi' not in json.dumps(result.json())
    assert len(e.calls) == before  # no Telegram lookups for the list
    assert e.client.get(BASE + '/ongoing').status_code == 401
    bad = headers(GUEST)
    bad['X-Telegram-Init-Data'] += 'tampered'
    assert e.client.get(BASE + '/ongoing', headers=bad).status_code == 401


def test_ongoing_scoped_to_groups_known_from_local_entries(e):
    home = create(e, config(title='本群'))
    elsewhere = create(e, config(title='他群'))
    store(e, elsewhere, chat_id=OTHER_CHAT)
    # No local evidence yet: everything open is listed.
    assert {i['id'] for i in req(e, 'GET', '/ongoing', GUEST).json()['items']} == {home, elsewhere}
    earlier = create(e, config(title='往期'))
    join(e, earlier, GUEST)
    draw(e, earlier)
    assert {i['id'] for i in req(e, 'GET', '/ongoing', GUEST).json()['items']} == {home}
    assert {i['id'] for i in req(e, 'GET', '/ongoing', MEMBER).json()['items']} == {home, elsewhere}


# --- f. participation thresholds --------------------------------------------------

@pytest.mark.parametrize('patch', [
    {'min_level': 9}, {'min_level': -1}, {'min_level': True}, {'min_level': '3'}, {'min_points': 1000001},
    {'min_points': 1.5}, {'min_watch_hours': 721}, {'min_watch_hours': -1}, {'reminder': 'yes'}, {'pin': 1}])
def test_threshold_and_switch_validation(e, patch):
    result = req(e, 'POST', '/admin/drafts', json=config(**patch))
    assert result.status_code == 400
    assert not e.service.db.query('SELECT * FROM lotteries')


def test_threshold_defaults_and_exclusions(e):
    draft = create(e, publish=False)
    c = e.service.row(draft)['config']
    assert (c['min_level'], c['min_points'], c['min_watch_hours'], c['reminder'], c['pin']) == (0, 0, 0, True, True)
    assert create(e, config(min_points=None, min_watch_hours=0), publish=False)
    result = req(e, 'POST', '/admin/drafts', json=config(min_points=10, audience='guest'))
    assert result.status_code == 400 and '仅未注册' in result.json()['detail']
    registration = [{'kind': 'registration', 'name': '本人资格', 'quantity': 1, 'group_id': 'standard', 'days': 10}]
    result = req(e, 'POST', '/admin/drafts', json=config(min_watch_hours=2, prizes=registration))
    assert result.status_code == 400 and '注册资格' in result.json()['detail']
    result = req(e, 'POST', '/admin/drafts', json=config(min_level=2))
    assert result.status_code == 400 and '等级体系未启用' in result.json()['detail']


def test_points_threshold_checked_not_debited_and_public(e):
    fund(e, 'u2', 40)
    lid = create(e, config(min_points=50))
    view = req(e, 'GET', '/' + lid, MEMBER).json()
    assert '参与门槛：积分余额不少于 50（积分只校验不扣）' in view['rules']
    assert '积分余额不少于 50 · 积分只校验不扣 · 须已注册' in json.loads(notice(e, lid, 'launch')['payload_json'])['text']
    result = req(e, 'POST', f'/{lid}/join', MEMBER)
    assert result.status_code == 400 and result.json()['detail'] == '需要积分余额不少于 50（当前 40，只校验不扣）'
    result = req(e, 'POST', f'/{lid}/join', GUEST)
    assert result.status_code == 400 and '须绑定已注册账户' in result.json()['detail']
    fund(e, 'u2', 10)
    assert join(e, lid, MEMBER)['joined'] and e.service.points.balance('u2') == 50
    assert not e.service.db.query("SELECT * FROM points_ledger WHERE reason LIKE 'lottery.%'")


def test_level_threshold_uses_existing_level_numbers(e):
    enable_levels(e, growth=400)  # Lv3 in the default level table
    assert app.state.members.find_by_telegram(str(MEMBER))['activity_level']['level'] == 3
    lid = create(e, config(min_level=4))
    result = req(e, 'POST', f'/{lid}/join', MEMBER)
    assert result.status_code == 400 and result.json()['detail'] == '需要 Lv4 及以上（当前 Lv3）'
    assert 'Lv4 及以上' in req(e, 'GET', '/' + lid, GUEST).json()['rules']
    ok = create(e, config(min_level=3))
    assert join(e, ok, MEMBER)['joined']


def test_watch_threshold_uses_member_facing_30_day_window(e):
    lid = create(e, config(min_watch_hours=1))
    result = req(e, 'POST', f'/{lid}/join', MEMBER)
    assert result.status_code == 400 and result.json()['detail'] == '需要近 30 天观看满 1 小时（当前约 0 小时）'
    now = time.time()
    e.service.db.execute('INSERT INTO watch_samples(session_key,emby_user_id,started_at,ended_at,seconds) '
                         'VALUES(?,?,?,?,?)', ('old', 'u2', now - 40 * 86400, now - 40 * 86400 + 7200, 7200))
    e.service.db.execute('INSERT INTO watch_samples(session_key,emby_user_id,started_at,ended_at,seconds) '
                         'VALUES(?,?,?,?,?)', ('recent', 'u2', now - 9000, now - 7200, 1800))
    assert app.state.stats.watch_summary('u2')['seconds_30d'] == 1800
    result = req(e, 'POST', f'/{lid}/join', MEMBER)
    assert result.json()['detail'] == '需要近 30 天观看满 1 小时（当前约 0.5 小时）'
    e.service.db.execute('INSERT INTO watch_samples(session_key,emby_user_id,started_at,ended_at,seconds) '
                         'VALUES(?,?,?,?,?)', ('more', 'u2', now - 5000, now - 3200, 1800))
    assert join(e, lid, MEMBER)['joined']


# --- g. reminders -------------------------------------------------------------------

def test_reminder_once_about_an_hour_before_deadline_replying_to_launch(e):
    lid = create(e, config(deadline=int(time.time()) + 7200))
    deadline = e.service.row(lid)['config']['deadline']
    e.service.stage_reminders(now=deadline - 3700)
    assert notice(e, lid, 'reminder') is None  # not yet due
    e.service.stage_reminders(now=deadline - 3500)
    e.service.stage_reminders(now=deadline - 3400)
    asyncio.run(e.service.deliver_notices(lid))
    reminders = [p for p in sent(e, 'sendMessage') if '即将截止' in p['text']]
    assert len(reminders) == 1
    reminder = reminders[0]
    assert reminder['reply_parameters']['message_id'] == notice(e, lid, 'launch')['message_id']
    assert reminder['reply_markup']['inline_keyboard'][0][0]['text'] == '立即参与'
    assert reminder['message_thread_id'] == 9 and no_forbidden(reminder['text'])
    e.service.stage_reminders(now=deadline - 1000)
    asyncio.run(e.service.deliver_notices(lid))
    assert len([p for p in sent(e, 'sendMessage') if '即将截止' in p['text']]) == 1
    assert req(e, 'GET', '/' + lid).json()['operations']['reminder']['state'] == 'sent'


def test_reminder_skips_short_window_missed_window_cancelled_and_disabled(e):
    short = create(e, config(deadline=int(time.time()) + 5000))
    missed = create(e, config(deadline=int(time.time()) + 7200))
    off = create(e, config(deadline=int(time.time()) + 7200, reminder=False))
    gone = create(e, config(deadline=int(time.time()) + 7200))
    req(e, 'POST', f'/{gone}/cancel', json={'confirm': True})
    e.service.stage_reminders(now=e.service.row(missed)['config']['deadline'] - 500)
    assert notice(e, missed, 'reminder') is None
    store(e, missed, reminder=False)  # keep it out of the following windows
    for lid in (short, off, gone):
        e.service.stage_reminders(now=e.service.row(lid)['config']['deadline'] - 3000)
        assert notice(e, lid, 'reminder') is None
    assert req(e, 'GET', '/' + short).json()['operations']['reminder']['state'] == 'skipped'
    assert req(e, 'GET', '/' + off).json()['operations']['reminder']['state'] == 'off'


def test_delayed_reminder_is_skipped_not_sent_late(e, monkeypatch):
    lid = create(e, config(deadline=int(time.time()) + 7200))
    original = TelegramBot._cfg
    monkeypatch.setattr(TelegramBot, '_cfg', lambda self: {**original(self), 'enabled': False})
    e.service.stage_reminders(now=e.service.row(lid)['config']['deadline'] - 3500)
    asyncio.run(e.service.deliver_notices(lid))
    assert notice(e, lid, 'reminder')['state'] == 'pending'
    monkeypatch.setattr(TelegramBot, '_cfg', original)
    store(e, lid, deadline=int(time.time()) + 300)  # Bot came back with < 10 minutes left
    asyncio.run(e.service.deliver_notices(lid))
    assert notice(e, lid, 'reminder')['state'] == 'skipped'
    assert not [p for p in sent(e, 'sendMessage') if '即将截止' in p['text']]


# --- h. silent pin -----------------------------------------------------------------------

def test_silent_pin_after_launch_and_unpin_after_draw(e):
    lid = create(e)
    mid = notice(e, lid, 'launch')['message_id']
    assert sent(e, 'pinChatMessage') == [{'chat_id': CHAT, 'message_id': mid, 'disable_notification': True}]
    assert op(e, lid, 'pin')['state'] == 'sent'
    ops(e, lid)
    assert len(sent(e, 'pinChatMessage')) == 1
    join(e, lid, MEMBER)
    draw(e, lid)
    assert sent(e, 'unpinChatMessage') == [{'chat_id': CHAT, 'message_id': mid}]
    ops(e, lid)
    assert len(sent(e, 'unpinChatMessage')) == 1


def test_pin_failure_recorded_not_retried_and_disabled_switch(e):
    e.script['pinChatMessage'] = [{'ok': False, 'error_code': 400, 'description': 'Bad Request: not enough rights to pin a message'}]
    lid = create(e)
    pin = req(e, 'GET', '/' + lid).json()['operations']['pin']
    assert pin['state'] == 'failed' and pin['error'] == 'Bot 没有置顶权限；未重试'
    ops(e, lid)
    assert len(sent(e, 'pinChatMessage')) == 1
    draw(e, lid)
    assert not sent(e, 'unpinChatMessage')  # nothing was pinned
    assert 'operations' not in req(e, 'GET', '/' + lid, MEMBER).json()
    off = create(e, config(pin=False))
    draw(e, off)
    assert len(sent(e, 'pinChatMessage')) == 1 and req(e, 'GET', '/' + off).json()['operations']['pin']['state'] == 'off'


def test_pin_unknown_is_not_repeated_and_bot_disabled_pauses(e, monkeypatch):
    original = TelegramBot._cfg
    e.script['pinChatMessage'] = ['timeout']
    lid = create(e)
    assert op(e, lid, 'pin')['state'] == 'unknown'
    ops(e, lid)
    assert len(sent(e, 'pinChatMessage')) == 1
    join(e, lid, MEMBER)
    monkeypatch.setattr(TelegramBot, '_cfg', lambda self: {**original(self), 'enabled': False})
    ops(e, lid)
    assert not sent(e, 'editMessageText')
    monkeypatch.setattr(TelegramBot, '_cfg', original)
    e.service.draw(lid)
    ops(e, lid)
    # Uncertain pin: unpin by message_id is still attempted (harmless if not pinned).
    assert sent(e, 'unpinChatMessage') == [{'chat_id': CHAT, 'message_id': notice(e, lid, 'launch')['message_id']}]


def test_pin_service_message_deleted_only_for_own_lottery_pin(e):
    lid = create(e)
    mid = notice(e, lid, 'launch')['message_id']
    bot = app.state.telegram

    def service_message(message_id, pinned, sender=BOT_ID):
        return {'message_id': message_id, 'date': int(time.time()), 'chat': {'id': int(CHAT), 'type': 'supergroup'},
                'from': {'id': sender, 'is_bot': True, 'first_name': 'bot'}, 'pinned_message': {'message_id': pinned}}
    asyncio.run(bot._lottery_pin_cleanup(service_message(9001, mid)))
    asyncio.run(bot._lottery_pin_cleanup(service_message(9002, mid + 1000)))
    asyncio.run(bot._lottery_pin_cleanup(service_message(9003, mid, sender=ADMIN)))
    assert sent(e, 'deleteMessage') == [{'chat_id': int(CHAT), 'message_id': 9001}]
    e.script['deleteMessage'] = [{'ok': False, 'error_code': 400, 'description': "Bad Request: message can't be deleted"}]
    asyncio.run(bot._lottery_pin_cleanup(service_message(9004, mid)))  # missing right: silently kept


# --- legacy compatibility & migration -------------------------------------------------------

def test_legacy_published_activity_keeps_behaviour_and_gets_no_new_operations(e):
    lid = create(e)
    raw = strip_new_fields(e, lid)
    e.calls.clear()
    join(e, lid, MEMBER)
    ops(e, lid)
    view = req(e, 'GET', '/' + lid).json()
    assert {view['operations'][k]['state'] for k in ('reminder', 'pin', 'unpin', 'live')} == {'off'}
    assert view['operations']['winner_dm'] == {'enabled': False, 'summary': {}}
    assert '参与门槛' not in view['rules']
    e.service.stage_reminders(now=raw['deadline'] - 3000)
    result = draw(e, lid)
    assert not e.service.db.query('SELECT * FROM lottery_dms') and result['awards'][0]['dm'] is None
    assert not sent(e, 'editMessageText') and not sent(e, 'pinChatMessage') and not sent(e, 'unpinChatMessage')
    assert notice(e, lid, 'reminder') is None
    stored = json.loads(e.service.db.one('SELECT config_json FROM lotteries WHERE id=?', (lid,))['config_json'])
    assert stored == raw  # never rewritten


def test_migration_adds_tables_and_column_without_rewriting_rows(tmp_path):
    path = tmp_path / 'legacy.db'
    Database(path).close()
    conn = sqlite3.connect(path)
    conn.execute('DROP TABLE lottery_dms')
    conn.execute('DROP TABLE lottery_ops')
    conn.execute('ALTER TABLE lotteries DROP COLUMN cancelled_at')
    legacy = json.dumps({'title': 'old', 'audience': 'all', 'chat_id': CHAT, 'thread_id': None, 'deadline': 1,
                         'draw_mode': 'manual', 'prizes': [], 'anti_multi': False}, ensure_ascii=False)
    conn.execute("INSERT INTO lotteries(id,state,config_json,created_by,created_at) VALUES('x','settled',?,'a',1)", (legacy,))
    conn.commit()
    conn.close()
    db = Database(path)
    try:
        columns = {r['name'] for r in db.query('PRAGMA table_info(lotteries)')}
        assert 'cancelled_at' in columns
        assert db.one("SELECT config_json,cancelled_at FROM lotteries WHERE id='x'") == {
            'config_json': legacy, 'cancelled_at': None}
        for table in ('lottery_dms', 'lottery_ops'):
            assert db.one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        for table in ('lottery_traits', 'lottery_trait_seen'):
            assert not db.one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        assert 'trait' not in {r['name'] for r in db.query('PRAGMA table_info(lottery_entries)')}
    finally:
        db.close()
