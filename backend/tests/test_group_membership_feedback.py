"""Saved scan evidence must identify outcomes without running another scan."""
import asyncio
import copy
import re
from html.parser import HTMLParser

import pytest
from test_group_membership import CHANNEL, GROUP, VIEWER, scan
from test_group_membership import env as membership_env  # noqa: F401
from test_tg_interaction_context import env as context_env  # noqa: F401

from app.modules.group_membership import GroupMembership, result_counts, result_reason


@pytest.fixture
def env(request):
    return request.getfixturevalue('membership_env')


def final_message(env):
    chat, message_id = env.bot._job_progress['group_membership']
    return env.tg.text(chat, message_id)


def test_completed_scan_edits_original_card_with_deleted_identity_and_evidence(env):
    env.cfg['membership_rules']['delete_enabled'] = True
    env.states[(CHANNEL, str(VIEWER))] = 'left'
    out = asyncio.run(scan(env))
    text = final_message(env)
    assert '已完成' in text and '已核 4/4 · 删除 1 · 保留 3 · 取消 0 · 失败保留 0' in text
    assert '已删除账号（1）' in text and '<b>ViewerA</b>' in text
    assert '未加入/未关注：Actual ' + CHANNEL in text
    assert '未加入/未关注：Actual ' + str(GROUP) not in text
    assert env.deleted == ['u1']
    assert out['counts']['deleted'] == 1
    assert next(row for row in out['rows'] if row['user_id'] == 'u1')['reason'].endswith(CHANNEL)
    # No second notification stream, private TG identifier or unrelated member names.
    assert len([1 for method, _ in env.tg.calls if method == 'sendMessage']) == 1
    assert str(VIEWER) not in text and 'ViewerB' not in text
    assert any(method == 'editMessageText' for method, _ in env.tg.calls)


def test_failed_delete_is_named_counted_separately_and_never_reported_as_success(env):
    env.cfg['membership_rules']['delete_enabled'] = True
    env.states[(CHANNEL, str(VIEWER))] = 'left'
    async def fail(_uid):
        return False
    env.bot._emby.delete_user = fail
    out = asyncio.run(scan(env))
    text = final_message(env)
    assert '删除 0 · 保留 3 · 取消 0 · 失败保留 1' in text
    assert '删除失败，本地保留（1）' in text and '<b>ViewerA</b>' in text
    assert '删除未确认成功，本地账号保留' in text
    assert '已删除账号' not in text and env.members.get('u1')
    assert out['counts']['failed_retained'] == 1


def test_detection_only_names_noncompliant_user_without_claiming_deletion(env):
    env.states[(CHANNEL, str(VIEWER))] = 'left'
    asyncio.run(scan(env))
    text = final_message(env)
    assert '不合规，仅检测未删除（1）' in text and '<b>ViewerA</b>' in text
    assert '不合规仅检测 1' in text and '本次未删除账号' in text
    assert not env.deleted and env.members.get('u1')


def test_counts_partition_outcomes_and_keep_unknown_distinct_from_absence():
    rows = [{'action': 'deleted', 'state': 'absent'},
            {'action': 'failed_retained', 'state': 'absent'},
            {'action': 'cancelled', 'state': 'present'},
            {'action': 'detected', 'state': 'absent'},
            {'action': 'detected', 'state': 'unknown'},
            {'action': 'kept', 'state': 'exempt'}]
    counts = result_counts(rows)
    assert counts == {'deleted': 1, 'kept': 3, 'cancelled': 1, 'failed_retained': 1,
                      'unknown': 1, 'noncompliant': 1}
    assert sum(counts[key] for key in ('deleted', 'kept', 'cancelled', 'failed_retained')) == len(rows)
    assert '不作为删除依据' in result_reason(rows[4])
    assert '已取消删除' in result_reason(rows[2])


def test_saved_legacy_results_gain_reasons_without_mutating_storage_or_replaying(env):
    saved = {'id': 'old', 'running': False, 'processed': 1, 'total': 1, 'rows': [
        {'username': 'FormerUser', 'user_id': 'gone', 'state': 'absent', 'action': 'deleted',
         'targets': [{'title': 'Saved channel', 'state': 'absent'}]}]}
    env.bot.membership._save('latest', saved)
    restored = GroupMembership(env.bot)
    before = copy.deepcopy(restored._latest)
    result = restored.status()
    assert result['rows'][0]['reason'] == '未加入/未关注：Saved channel'
    assert 'FormerUser' in restored._progress_text()
    assert restored._latest == before == saved
    assert not env.deleted and restored._scan_task is None


def test_large_special_character_results_are_bounded_and_overflow_is_explicit(env):
    env.bot.membership._latest = {'running': False, 'total': 200, 'processed': 200, 'rows': [
        {'username': f'Account{index}<name&>', 'state': 'absent', 'action': 'deleted',
         'targets': [{'title': 'Channel<&>' * 25, 'state': 'absent'}]} for index in range(200)]}
    text = env.bot.membership._progress_text()
    assert len(text) < 3900 and '删除 200' in text
    assert '&lt;name&amp;&gt;' in text and '<name&>' not in text
    shown = text.count('\n• ')
    assert int(re.search(r'另有 (\d+) 条处理明细', text).group(1)) == 200 - shown
    assert text.count('<b>') == text.count('</b>')
    HTMLParser().feed(text)
    assert len(env.bot.membership.status()['rows']) == 200


@pytest.mark.parametrize('state,label', [({'cancelled': True}, '已取消'),
                                        ({'interrupted': True}, '已中断'),
                                        ({'error': 'OSError'}, '失败：OSError')])
def test_partial_job_is_not_labelled_completed(env, state, label):
    env.bot.membership._latest = {'running': False, 'rows': [], **state}
    text = env.bot.membership._progress_text()
    assert label in text and '已完成' not in text
