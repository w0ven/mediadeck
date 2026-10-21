"""Invite tree, departures and the one-level inviter case; local services only."""
import asyncio

import pytest
from test_tg_interaction_context import GROUP, VIEWER, click, command
from test_tg_interaction_context import env as interaction_env  # noqa: F401

from app.core.errors import ConfigError
from app.modules import member_ops
from app.modules.invite_tree import InviteTree, active_suspension, decide_case


@pytest.fixture
def env(request):
    return request.getfixturevalue('interaction_env')


def chain(env):
    """u1 invited u2, u2 invited u3."""
    env.db.execute("UPDATE members SET inviter_id='u1' WHERE emby_user_id='u2'")
    env.members.upsert('u3', 'ViewerC', {'group_id': 'standard', 'inviter_id': 'u2'})
    env.members.bind_telegram('u3', '905')
    env.db.execute("UPDATE members SET tg_display_name='小B' WHERE emby_user_id='u2'")
    env.db.execute("UPDATE members SET tg_display_name='小C' WHERE emby_user_id='u3'")
    return InviteTree(env.db, env.members)


def remove(env, uid, *, cascade=False, source='admin'):
    ids = [uid] + ([env.members.inviter_of(uid)['emby_user_id']] if cascade else [])
    return asyncio.run(member_ops.execute_delete(
        env.members, env.bot._emby, uid, actor='operator', cascade=cascade,
        confirm_ids=ids, review_source=source))


def test_admin_delete_keeps_departure_in_tree_and_opens_one_level_case(env):
    tree = chain(env)
    result = remove(env, 'u3')
    assert result['deleted'] and result['invite_case_id']
    case = tree.case(result['invite_case_id'])
    assert case['inviter_id'] == 'u2' and case['status'] == 'pending' and case['source'] == 'admin'
    assert case['deleted']['label'] == '小C' and case['inviter']['label'] == '小B'
    nodes = tree.downstream('u1', 2)
    assert [n['emby_user_id'] for n in nodes] == ['u2']
    child = nodes[0]['children'][0]
    assert child['emby_user_id'] == 'u3' and child['status'] == 'deleted'
    assert tree.count_descendants('u1') == 2
    assert [n['emby_user_id'] for n in tree.upstream('u2')] == ['u1']


@pytest.mark.parametrize('cascade,source', [(True, 'admin'), (False, ''), (False, 'expiry')])
def test_cascade_self_and_expiry_deletions_open_no_case(env, cascade, source):
    tree = chain(env)
    result = remove(env, 'u3', cascade=cascade, source=source)
    assert result['invite_case_id'] is None
    assert not env.db.query('SELECT * FROM invite_cases')
    assert env.db.one("SELECT source FROM member_departures WHERE emby_user_id='u3'")
    if cascade:
        assert env.members.get('u2') is None
        assert env.db.one("SELECT source FROM member_departures WHERE emby_user_id='u2'")['source'] == 'cascade'
    assert tree.children('u2')[0]['status'] == 'deleted'


def test_admin_inviter_is_never_put_up_for_deletion(env):
    env.members.upsert('u4', 'ViewerD', {'group_id': 'standard', 'inviter_id': 'admin'})
    assert remove(env, 'u4')['invite_case_id'] is None


def test_violation_deletion_opens_case(env):
    tree = chain(env)
    gone = env.members.delete('u3', actor='restriction', source='violation')
    case_id = tree.open_case(gone['departures']['u3'], 'violation', 'restriction')
    assert tree.case(case_id)['source_label'] == '违规规则自动删号'


def test_suspension_blocks_new_codes_and_existing_codes_until_lifted(env):
    tree = chain(env)
    registration = env.bot._registration
    env.db.execute("UPDATE members SET invite_quota=2 WHERE emby_user_id='u2'")
    code = registration.spend_quota_for_invite('u2')['code']
    case_id = remove(env, 'u3')['invite_case_id']
    case = asyncio.run(decide_case(tree, env.bot._emby, case_id, 'suspend', actor='tg:Operator'))
    assert case['status'] == 'suspended' and case['suspended_until']
    assert active_suspension(env.db, 'u2')
    with pytest.raises(ConfigError, match='暂停'):
        registration.spend_quota_for_invite('u2')
    admission = registration.resolve('777001', code)
    assert not admission.allowed and '暂时不可用' in admission.reason
    assert tree.lift('u2', actor='operator')
    assert registration.resolve('777001', code).allowed


def test_delete_decision_is_one_level_and_settles_once(env):
    tree = chain(env)
    case_id = remove(env, 'u3')['invite_case_id']
    case = asyncio.run(decide_case(tree, env.bot._emby, case_id, 'delete', actor='tg:Operator'))
    assert case['status'] == 'deleted' and env.members.get('u2') is None
    assert len(env.db.query('SELECT * FROM invite_cases')) == 1  # u1 is never asked about
    again = asyncio.run(decide_case(tree, env.bot._emby, case_id, 'ignore', actor='tg:Other'))
    assert again['already'] and again['status'] == 'deleted'


def test_group_card_is_admin_only_and_deletion_needs_second_tap(env):
    tree = chain(env)
    case_id = remove(env, 'u3')['invite_case_id']

    async def run():
        await env.bot._flush_invite_cases()
        card = tree.case(case_id)
        assert card['delivery'] == 'sent' and card['chat_id'] == str(GROUP)
        mid = int(card['message_id'])
        text = env.tg.text(GROUP, mid)
        assert '小C' in text and '小B' in text and 'ViewerB' not in text
        assert {b['callback_data'] for b in env.tg.actions(GROUP, mid)} == {
            f'ic:del:{case_id}', f'ic:sus:{case_id}', f'ic:ign:{case_id}'}
        await click(env, f'ic:ign:{case_id}', mid, user=VIEWER)
        assert tree.case(case_id)['status'] == 'pending'
        await click(env, f'ic:del:{case_id}', mid)
        assert '确认删除邀请人' in env.tg.text(GROUP, mid)
        assert env.members.get('u2') is not None
        await click(env, f'ic:delok:{case_id}', mid)
        assert tree.case(case_id)['status'] == 'deleted' and env.members.get('u2') is None
        assert '已连带删除邀请人' in env.tg.text(GROUP, mid)
        assert env.tg.actions(GROUP, mid) == []
    asyncio.run(run())


def test_member_sees_two_levels_including_deleted_invitees(env):
    chain(env)
    remove(env, 'u3')

    async def run():
        mid = await command(env, '/start', chat=VIEWER, user=VIEWER)
        await click(env, 'invites', mid, chat=VIEWER, user=VIEWER)
        assert any(b['callback_data'] == 'invitees' for b in env.tg.actions(VIEWER, mid))
        await click(env, 'invitees', mid, chat=VIEWER, user=VIEWER)
        text = env.tg.text(VIEWER, mid)
        assert '我邀请的人' in text and '小B' in text
        assert '└ ⛔' in text and '小C' in text and '已删除' in text
        assert 'ViewerB' not in text and 'ViewerC' not in text
    asyncio.run(run())
