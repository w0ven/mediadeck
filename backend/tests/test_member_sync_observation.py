"""List/detail observations are facts, separate from policy-write history."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.enforcement import desired_policy, fingerprint
from app.modules.member_ops import observe_one

AUTH = ('admin', 'change-me')
OBSERVATION = ('emby_status', 'emby_disabled', 'emby_is_admin', 'sync_status',
               'policy_matches', 'sync_recorded', 'applied_at', 'applied_fingerprint')


@pytest.fixture
def local(monkeypatch):
    with TestClient(app) as client:
        app.state.members.upsert('u1', 'local-member', {'group_id': 'standard'})
        policy = {**desired_policy(app.state.members.get('u1')), 'IsAdministrator': False}
        upstream = AsyncMock(return_value=[{'Id': 'u1', 'Policy': policy}])
        monkeypatch.setattr(app.state.emby, 'list_users', upstream)
        monkeypatch.setattr(app.state.emby, 'sessions_for_user', AsyncMock(return_value=[]))
        monkeypatch.setattr(app.state.emby, 'apply_member_policy', AsyncMock(return_value={'status': 'applied'}))
        yield client, policy, upstream


def observations(client):
    listing = client.get('/api/members?page=1', auth=AUTH)
    detail = client.get('/api/members/u1', auth=AUTH)
    assert listing.status_code == detail.status_code == 200
    a, b = listing.json()['members'][0], detail.json()['member']
    assert {k: a[k] for k in OBSERVATION} == {k: b[k] for k in OBSERVATION}
    return a, b


@pytest.mark.parametrize('case,emby,sync,matches,recorded', [
    ('success', 'present', 'in_sync', True, True),
    ('no_record_match', 'present', 'policy_match', True, False),
    ('no_record_drift', 'present', 'never_applied', False, False),
    ('drift', 'present', 'drift', False, True),
    ('external_match', 'present', 'policy_match', True, True),
    ('missing', 'missing', 'emby_missing', None, False),
    ('read_failure', 'unknown', 'unknown', None, False),
    ('missing_policy', 'present', 'unknown', None, False),
    ('partial_policy', 'present', 'unknown', None, False),
    ('admin', 'present', 'skipped_admin', None, False),
    ('write_failure', 'present', 'failed', False, False),
    ('write_failure_but_matches', 'present', 'failed', True, False),
    ('unrelated_failure', 'present', 'policy_match', True, False),
])
def test_list_and_detail_share_truthful_observation(local, case, emby, sync, matches, recorded):
    client, policy, upstream = local
    if recorded:
        fp = fingerprint(desired_policy(app.state.members.get('u1')))
        if case == 'external_match':
            fp = 'older-policy'
        app.state.db.execute('UPDATE members SET applied_fingerprint=?,applied_at=123 WHERE emby_user_id=?', (fp, 'u1'))
    if case in ('drift', 'no_record_drift', 'write_failure'):
        policy['SimultaneousStreamLimit'] += 1
    if case == 'missing':
        upstream.return_value = []
    elif case == 'read_failure':
        upstream.side_effect = RuntimeError('remote unavailable')
    elif case == 'missing_policy':
        upstream.return_value = [{'Id': 'u1'}]
    elif case == 'partial_policy':
        policy.pop('EnableContentDownloading')
    elif case == 'admin':
        policy['IsAdministrator'] = True
    if case in ('write_failure', 'write_failure_but_matches', 'unrelated_failure'):
        action = 'delete_emby' if case == 'unrelated_failure' else 'enforce'
        app.state.db.execute('UPDATE members SET last_remote_action=?,last_remote_ok=0 WHERE emby_user_id=?', (action, 'u1'))
    before = app.state.members.get('u1')
    listed, detail = observations(client)
    assert (detail['emby_status'], detail['sync_status'], detail['policy_matches'], detail['sync_recorded']) == (emby, sync, matches, recorded)
    assert app.state.members.get('u1') == before  # GET never manufactures sync history
    assert upstream.await_count == 1  # drawer shares the list's upstream observation
    app.state.emby.apply_member_policy.assert_not_awaited()
    if case in ('missing_policy', 'partial_policy'):
        assert detail['sync_status'] == 'unknown'
    assert listed['applied_at'] == (123 if recorded else None)


def test_expired_snapshot_failure_is_unknown_then_recovers_for_both_views(local):
    client, _policy, upstream = local
    observations(client)
    app.state.cache.set('members:emby', app.state.cache.get('members:emby'), ttl=-1)
    upstream.side_effect = RuntimeError('remote failed')
    # Detail first, then list: neither may reuse the expired positive snapshot.
    detail = client.get('/api/members/u1', auth=AUTH).json()['member']
    assert detail['emby_status'] == detail['sync_status'] == 'unknown'
    assert observations(client)[0]['sync_status'] == 'unknown'
    upstream.side_effect = None
    app.state.cache.set('members:emby', app.state.cache.get('members:emby'), ttl=-1)
    assert observations(client)[0]['sync_status'] == 'policy_match'
    assert upstream.await_count == 3
    assert app.state.members.get('u1')['applied_at'] is None


@pytest.mark.parametrize('apply', [False, True])
def test_reconcile_noop_never_invents_an_apply_timestamp(local, apply):
    result = asyncio.run(app.state.enforcement.reconcile(apply=apply, user_id='u1'))
    assert result['applied'] == 0
    row = app.state.members.get('u1')
    assert not row['applied_fingerprint'] and row['applied_at'] is None
    app.state.emby.apply_member_policy.assert_not_awaited()


@pytest.mark.parametrize('path', ['immediate', 'reconcile'])
@pytest.mark.parametrize('succeeds', [True, False])
def test_only_confirmed_policy_writes_record_history(local, path, succeeds):
    client, policy, _upstream = local
    policy['SimultaneousStreamLimit'] += 1
    async def apply(uid, want):
        if succeeds:
            policy.update(want)
        return {'status': 'applied' if succeeds else 'failed'}
    app.state.emby.apply_member_policy.side_effect = apply
    observations_before = client.get('/api/members?page=1', auth=AUTH).json()['members'][0]
    assert observations_before['sync_status'] == 'never_applied'
    if path == 'immediate':
        result = client.post('/api/members/u1/retry-remote', auth=AUTH).json()
    else:
        result = asyncio.run(app.state.enforcement.reconcile(apply=True, user_id='u1'))
    assert result['ok'] is succeeds
    member = app.state.members.get('u1')
    assert bool(member['applied_at']) is succeeds
    assert bool(member['applied_fingerprint']) is succeeds
    assert member['last_remote_action'] == 'enforce' and member['last_remote_ok'] is succeeds
    assert observations(client)[1]['sync_status'] == ('in_sync' if succeeds else 'failed')
    assert app.state.emby.apply_member_policy.await_count == 1


def test_unknown_policy_does_not_claim_enabled():
    row = observe_one({'emby_user_id': 'x'}, {'Id': 'x'}, emby_available=True)
    assert row['emby_disabled'] is None and row['emby_is_admin'] is None
    assert row['sync_status'] == 'unknown'
