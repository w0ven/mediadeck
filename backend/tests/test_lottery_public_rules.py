"""Server-owned public wording: new/copy policy, exact legacy text and silent errors.

Real isolated SQLite/services/APIs; no production data or Telegram requests.
"""
import copy
import json
import time

import pytest

from app.core.db import Database
from app.modules import lottery_identity
from app.modules.lottery import ELIGIBILITY_RULES, PUBLIC_RULES_VERSION
from tests.test_lottery import ADMIN, GUEST, MEMBER, browser_id, config, create, draw, req
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_lottery_device import CONSENT, UNSET, assert_public, legacy_create, strict

# Literal v0.52 text, not constructed from the production constants under test.
OLD_FAIRNESS = '发现多账号等违规参与，将取消中奖资格，报名费不退。'
OLD_RULES = ('每个 Telegram 每期仅一次、最多一奖；报名时须属于发布群且符合参与对象。'
             '截止后不能报名；定时开奖到点自动开奖，手动开奖由管理员开奖，未设截止时开奖前均可报名。人数不足如实少发。'
             '本人专属注册资格仅在开奖时仍未注册者中抽取，先抽该类奖，再抽其他奖，'
             '同类奖项随机排序，每个名额在剩余合资格报名者中等概率抽取。'
             '账户类奖励未绑定时保留，绑定后可领取；商城道具入背包后使用生效。'
             '注册资格绑定中奖 Telegram，不可转赠，进入原 Bot 注册流程自行设置密码，受原注册通道开关约束。'
             '发现多账号等违规参与，将取消中奖资格，报名费不退。')
FORBIDDEN = ('多账号', '防小号', 'IP', '浏览器', '设备', '关联', '本地存储')
RETRY = '报名暂不可用，请关闭后重新打开活动，或稍后再试'


@pytest.fixture
def e(request):
    return request.getfixturevalue('lottery_env')


def stored(e, lid):
    return e.service.db.one('SELECT * FROM lotteries WHERE id=?', (lid,))


def notices(e, lid):
    return e.service.db.query('SELECT * FROM lottery_notices WHERE lottery_id=? ORDER BY id', (lid,))


def no_hints(text):
    assert not any(word in text for word in FORBIDDEN), text


def old_rules(fee):
    return ('报名扣 40 积分，成功报名后不退（未中奖不退），活动取消时全额退还；须绑定已注册积分账户。'
            if fee else '免费参与。') + OLD_RULES


@pytest.mark.parametrize('fee', [0, 40])
@pytest.mark.parametrize('anti_multi', [False, True])
def test_new_rules_preview_notice_preserve_qualification_fee_and_admin_scope(e, fee, anti_multi):
    raw = config(entry_fee=fee, anti_multi=anti_multi, audience='member' if fee else 'guest')
    if not fee:
        raw['prizes'] = [{'kind': 'registration', 'name': '本人资格', 'quantity': 2, 'group_id': 'standard', 'days': 10}]
    lid = create(e, raw, publish=False)
    preview = req(e, 'POST', f'/{lid}/preview').json()
    assert req(e, 'POST', f'/{lid}/publish', json={'revision': 1}).status_code == 200
    admin = req(e, 'GET', '/' + lid).json()
    assert admin['admin_rules'] == (lottery_identity.RULE if anti_multi else lottery_identity.RULE_OFF)
    assert admin['config']['public_rules_version'] == PUBLIC_RULES_VERSION == 2
    assert admin['config']['device_check'] is False and 'consent' not in admin
    for user in (MEMBER, GUEST):
        view = req(e, 'GET', '/' + lid, user).json()
        assert_public(view)
        assert 'consent' not in view and view['rules'] == admin['rules'] == preview['rules']
        no_hints(view['rules'])
    assert admin['rules'] == old_rules(fee).removesuffix(OLD_RULES) + ELIGIBILITY_RULES
    launch = json.loads(notices(e, lid)[0]['payload_json'])
    for text in (preview['text'], launch['text']):
        no_hints(text)
        assert '截止' in text and raw['prizes'][0]['name'] in text
        if fee:
            assert '仅已注册可参与' in text
            assert '报名 40 积分 · 成功报名不退，取消活动全额退还' in text
        else:
            assert '仅未注册可参与' in text and '本人资格 × 2 · 仅未注册' in text
    assert '<tg-emoji emoji-id=' in launch['text']
    assert launch['reply_markup']['inline_keyboard'][0][0]['icon_custom_emoji_id']


@pytest.mark.parametrize('submitted', [None, 1, 2, False, '1', {'version': 1}])
def test_client_cannot_select_public_rules_on_new_create_edit_or_publish(e, submitted):
    raw = config(public_rules_version=submitted, legacy=True, legacy_rules=True,
                 rules=OLD_RULES, public_rules=OLD_RULES, stored={'public_rules_version': 1})
    lid = create(e, raw, publish=False)
    assert json.loads(stored(e, lid)['config_json'])['public_rules_version'] == 2
    assert req(e, 'PUT', '/' + lid, json={**raw, 'revision': 1}).status_code == 200
    assert req(e, 'POST', f'/{lid}/publish', json={'revision': 2, 'public_rules_version': 1}).status_code == 200
    assert json.loads(stored(e, lid)['config_json'])['public_rules_version'] == 2
    for user in (MEMBER, GUEST, ADMIN):
        no_hints(req(e, 'GET', '/' + lid, user).json()['rules'])
    assert OLD_FAIRNESS not in notices(e, lid)[0]['payload_json']


@pytest.mark.parametrize('previous', [UNSET, 1, 2])
def test_existing_draft_keeps_stored_public_policy_and_legacy_consent(e, previous):
    lid = (create(e, config(entry_fee=40, audience='member'), publish=False) if previous == 2
           else legacy_create(e, strict(entry_fee=40, audience='member'), publish=False))
    if previous == 1:
        c = json.loads(stored(e, lid)['config_json'])
        c['public_rules_version'] = 1
        e.service.db.execute('UPDATE lotteries SET config_json=? WHERE id=?', (json.dumps(c), lid))
    new = previous == 2
    current_rules = old_rules(40).removesuffix(OLD_RULES) + ELIGIBILITY_RULES
    expected = current_rules if new else current_rules + OLD_FAIRNESS
    original = stored(e, lid)
    assert req(e, 'POST', f'/{lid}/preview').json()['rules'] == (expected if new else old_rules(40))
    assert stored(e, lid) == original  # a read/preview never backfills the absent version
    for revision, submitted in enumerate((UNSET, 2, 1, None, '2'), start=1):
        raw = config(entry_fee=40, audience='member', revision=revision, device_check=False, legacy=True)
        if submitted is not UNSET:
            raw['public_rules_version'] = submitted
        edited = req(e, 'PUT', '/' + lid, json=raw)
        assert edited.status_code == 200
        assert edited.json()['rules'] == expected
        assert edited.json()['config']['public_rules_version'] == (2 if new else 1)
        assert edited.json()['config']['device_check'] is (not new)
        if not new:
            assert edited.json()['consent'] == CONSENT
        snapshot = stored(e, lid)
        assert req(e, 'PUT', '/' + lid, json={**raw, 'public_rules_version': 2}).status_code == 400
        assert stored(e, lid) == snapshot
    published = req(e, 'POST', f'/{lid}/publish', json={'revision': 6, 'public_rules_version': 2}).json()
    assert published['rules'] == expected
    assert (OLD_FAIRNESS in json.loads(notices(e, lid)[0]['payload_json'])['text']) is (not new)
    if not new:
        response = req(e, 'POST', f'/{lid}/join', MEMBER, json={'browser_id': browser_id(MEMBER), 'expected_entry_fee': 40})
        assert response.status_code == 400 and response.json()['detail'] == '需同意设备校验后才能参加'
        assert req(e, 'GET', '/' + lid, GUEST).json()['consent'] == CONSENT


@pytest.mark.parametrize('state', ['draft', 'published', 'settled', 'cancelled'])
@pytest.mark.parametrize('fee', [0, 40])
def test_copy_always_gets_new_rules_without_rewriting_source(e, state, fee):
    source = legacy_create(e, config(entry_fee=fee, audience='member' if fee else 'all'), publish=state != 'draft')
    if state == 'settled':
        draw(e, source)
    elif state == 'cancelled':
        assert req(e, 'POST', f'/{source}/cancel', json={'confirm': True}).status_code == 200
    before, frozen, calls = stored(e, source), notices(e, source), copy.deepcopy(e.calls)
    copied = req(e, 'POST', f'/{source}/copy', json={'public_rules_version': 1, 'legacy': True}).json()
    lid = copied['id']
    assert copied['config']['public_rules_version'] == 2 and copied['config']['device_check'] is False
    no_hints(copied['rules'])
    assert 'consent' not in copied
    preview = req(e, 'POST', f'/{lid}/preview').json()
    no_hints(preview['rules'])
    no_hints(preview['text'])
    assert stored(e, source) == before and notices(e, source) == frozen and e.calls == calls
    edited = req(e, 'PUT', '/' + lid, json={**copied['config'], 'revision': 1,
                                           'deadline': int(time.time()) + 3600, 'public_rules_version': 1})
    assert edited.status_code == 200
    assert req(e, 'POST', f'/{lid}/publish', json={'revision': 2}).status_code == 200
    for user in (MEMBER, GUEST):
        view = req(e, 'GET', '/' + lid, user).json()
        assert_public(view)
        no_hints(view['rules'])
    text = json.loads(notices(e, lid)[0]['payload_json'])['text']
    no_hints(text)
    if fee:
        assert '报名 40 积分 · 成功报名不退，取消活动全额退还' in text
    assert stored(e, source) == before and notices(e, source) == frozen


@pytest.mark.parametrize('fee', [0, 40])
@pytest.mark.parametrize('state', ['published', 'settled'])
def test_legacy_pages_and_frozen_notices_stay_exact_no_requeue_or_edit(e, fee, state):
    lid = legacy_create(e, config(entry_fee=fee))
    if state == 'settled':
        draw(e, lid)
    launch = next(n for n in notices(e, lid) if n['kind'] == 'launch')
    assert (OLD_FAIRNESS in json.loads(launch['payload_json'])['text']) is bool(fee)
    original = stored(e, lid)
    assert 'public_rules_version' not in json.loads(original['config_json'])
    # Emulate definite failure / uncertain delivery as well as a sent old notice.
    # Reading, previewing, repeated publish, or migration must not rewrite/requeue it.
    for notice_state in ('sent', 'failed', 'unknown'):
        e.service.db.execute('UPDATE lottery_notices SET state=?,message_id=? WHERE id=?',
                             (notice_state, launch['message_id'] if notice_state == 'sent' else None, launch['id']))
        frozen, calls = notices(e, lid), copy.deepcopy(e.calls)
        for user in (MEMBER, GUEST, ADMIN):
            view = req(e, 'GET', '/' + lid, user).json()
            assert view['rules'] == old_rules(fee)
        assert req(e, 'POST', f'/{lid}/preview').json()['rules'] == old_rules(fee)
        assert req(e, 'POST', f'/{lid}/publish', json={'revision': 1, 'public_rules_version': 2}).status_code == 200
        edited = req(e, 'PUT', '/' + lid, json={**config(entry_fee=fee), 'revision': 1, 'public_rules_version': 2})
        assert edited.status_code == 400
        reopened = Database(e.service.db.path)
        reopened.close()
        assert stored(e, lid) == original
        assert notices(e, lid) == frozen and e.calls == calls


@pytest.mark.parametrize('token', [None, True, 123, '', 'a' * 63, 'A' * 64, {'id': 'a' * 64}])
@pytest.mark.parametrize('fee', [0, 40])
def test_invalid_installation_id_has_generic_error_and_rolls_back_all_writes(e, token, fee):
    lid = create(e, config(entry_fee=fee, audience='member' if fee else 'all', prizes=[
        {'kind': 'manual', 'name': '人工奖', 'quantity': 2, 'instructions': '私密'}]))
    user = MEMBER if fee else GUEST
    e.service.points.add('u2', 100, 'admin.mint', 'public-rule-test-fund')
    ledger = e.service.db.query('SELECT * FROM points_ledger ORDER BY id')
    body = {'expected_entry_fee': fee, 'browser_id': token}
    result = req(e, 'POST', f'/{lid}/join', user, json=body)
    assert result.status_code == 400 and result.json() == {'detail': RETRY}
    no_hints(result.json()['detail'])
    assert e.service.points.balance('u2') == 100
    assert e.service.db.query('SELECT * FROM points_ledger ORDER BY id') == ledger
    for table in ('lottery_entries', 'lottery_signals', 'lottery_device_entries', 'lottery_identity_key'):
        assert not e.service.db.query(f'SELECT * FROM {table}')
    # A valid retry succeeds; then idempotent repeats do not re-charge or
    # replace the original signal, even with invalid client data.
    accepted = req(e, 'POST', f'/{lid}/join', user, json={**body, 'browser_id': browser_id(user)})
    assert accepted.status_code == 200 and accepted.json()['joined']
    assert_public(accepted.json())
    again = req(e, 'POST', f'/{lid}/join', user, json=body)
    assert again.status_code == 200 and again.json() == accepted.json()
    assert e.service.points.balance('u2') == 100 - fee
    rows = e.service.db.query("SELECT * FROM points_ledger WHERE reason='lottery.entry'")
    assert len(rows) == (1 if fee else 0)
    assert e.service.db.one('SELECT COUNT(*) AS n FROM lottery_entries')['n'] == 1
    assert e.service.db.one('SELECT COUNT(*) AS n FROM lottery_signals')['n'] == 2
    assert not e.service.db.query('SELECT * FROM lottery_device_entries')
