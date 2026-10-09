"""The real members API filters and sorts the full activity cohort before slicing."""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from test_member_activity import DAY, NOW, sample

from app.main import app
from app.modules.enforcement import desired_policy

AUTH = ('admin', 'change-me')


@pytest.fixture
def panel(monkeypatch):
    monkeypatch.setattr('app.main.time.time', lambda: NOW)
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.usage, 'tick', AsyncMock(return_value={'ok': True}))
        app.state.db.execute("UPDATE meta SET value=? WHERE key='watch_verified_since'", (str(NOW - 60 * DAY),))
        for index, (uid, group, status, age, bound) in enumerate([
            ('alpha', 'standard', 'active', 60, True),
            ('beta', 'standard', 'active', 60, True),
            ('gamma', 'standard', 'active', 60, True),
            ('delta', 'standard', 'active', 4, True),
            ('epsilon', 'standard', 'pending', 60, True),
            ('zeta', 'vip', 'active', 60, False),
        ]):
            app.state.members.upsert(uid, uid, {'group_id': group, 'status': status,
                                               'roles': ['admin'] if bound else []})
            app.state.db.execute('UPDATE members SET created_at=?,last_seen_at=?,tg_user_id=? WHERE emby_user_id=?',
                                 (NOW - age * DAY, NOW, str(index + 100) if bound else '', uid))
        sample(app.state.db, NOW - 40 * DAY - 30, NOW - 40 * DAY, 'alpha')
        sample(app.state.db, NOW - 14 * DAY - 30, NOW - 14 * DAY, 'beta')
        sample(app.state.db, NOW - DAY - 600, NOW - DAY, 'gamma')
        upstream = AsyncMock(return_value=[
            {'Id': uid, 'Name': uid, 'LastActivityDate': '2099-10-10T12:00:00Z',
             'Policy': {**desired_policy(app.state.members.get(uid)), 'IsAdministrator': False}}
            for uid in ('alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta')])
        monkeypatch.setattr(app.state.emby, 'list_users', upstream)
        monkeypatch.setattr(app.state.emby, 'apply_member_policy', AsyncMock())
        monkeypatch.setattr(app.state.emby, 'delete_user', AsyncMock())
        yield client


def listing(client, **params):
    response = client.get('/api/members', params=params, auth=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def ids(payload):
    return [m['emby_user_id'] for m in payload['members']]


@pytest.mark.parametrize('sort', ['last_seen', 'last_played'])
def test_lastplay_filter_sort_pages_offsets_totals_and_combined_filters(panel, sort):
    query = {'activity': 'inactive', 'sort': sort, 'order': 'desc', 'page_size': 1}
    pages = [listing(panel, page=p, **query) for p in (1, 2, 3)]
    assert [ids(p) for p in pages] == [['beta'], ['alpha'], ['zeta']]
    assert all(p['total'] == p['counts']['total'] == 3 for p in pages)
    assert all(m['playback_activity']['as_of'] == NOW for p in pages for m in p['members'])
    assert ids(listing(panel, offset=1, **query)) == ['alpha']
    combined = listing(panel, page=1, activity='inactive', group_id='standard', status='active',
                       role='admin', tg='bound', emby_status='present', search='a', sort=sort, order='desc')
    assert ids(combined) == ['beta', 'alpha'] and combined['total'] == 2
    assert ids(listing(panel, page=1, activity='inactive', group_id='vip', tg='unbound')) == ['zeta']
    assert ids(listing(panel, page=1, activity='observing')) == ['delta']
    assert ids(listing(panel, page=1, activity='pending')) == ['epsilon']
    assert len(ids(listing(panel, page=1))) == 6
    assert not ids(listing(panel, page=9, **query))
    # The unpaged compatibility limit also comes after activity filtering/sorting.
    limited = listing(panel, limit=1, activity='inactive', sort=sort, order='desc')
    assert ids(limited) == ['beta'] and limited['total'] == 3 and limited['truncated']


def test_four_day_realistic_global_coverage_has_no_long_term_candidates(panel):
    app.state.db.execute("UPDATE meta SET value=? WHERE key='watch_verified_since'", (str(NOW - 4 * DAY),))
    assert listing(panel, page=1, activity='inactive')['total'] == 0
    observed = listing(panel, page=1, activity='observing')
    assert observed['total'] == 5
    assert all(m['playback_activity']['observed_days'] == 4 for m in observed['members'])
    assert next(m for m in observed['members'] if m['emby_user_id'] == 'zeta')['playback_activity']['score'] is None


def test_list_get_visits_and_traffic_never_write_lastplay_or_enforcement(panel):
    tables = ('members', 'watch_verified_totals', 'watch_verified_samples', 'audit_log')
    before = {table: app.state.db.query(f'SELECT * FROM {table}') for table in tables}
    for _ in range(3):
        rows = listing(panel, page=1, sort='last_seen', order='desc')['members']
        assert next(m for m in rows if m['emby_user_id'] == 'alpha')['last_played_at'] == NOW - 40 * DAY
        assert next(m for m in rows if m['emby_user_id'] == 'alpha')['last_activity'].startswith('2099')
    assert {table: app.state.db.query(f'SELECT * FROM {table}') for table in tables} == before
    app.state.members.add_traffic('alpha', 1024)
    assert next(m for m in listing(panel, page=1)['members'] if m['emby_user_id'] == 'alpha')['last_played_at'] == NOW - 40 * DAY
    app.state.emby.apply_member_policy.assert_not_awaited()
    app.state.emby.delete_user.assert_not_awaited()


def test_read_failure_stays_unknown_and_does_not_break_list(panel, monkeypatch):
    original = app.state.db.query
    def failed(sql, *args):
        if 'FROM watch_verified_samples' in sql:
            raise RuntimeError('isolated read failure')
        return original(sql, *args)
    monkeypatch.setattr(app.state.db, 'query', failed)
    assert listing(panel, page=1, activity='inactive')['total'] == 0
    rows = listing(panel, page=1, activity='unavailable')['members']
    assert len(rows) == 5 and all(m['playback_activity']['score'] is None for m in rows)
    assert next(m for m in rows if m['emby_user_id'] == 'alpha')['last_played_at'] == NOW - 40 * DAY


def test_explicit_sampler_failure_prevents_candidate_even_if_upstream_visits_work(panel, monkeypatch):
    monkeypatch.setattr(app.state.usage, 'status', lambda: {'last_error': 'sample failed'})
    assert listing(panel, page=1, activity='inactive')['total'] == 0
    assert listing(panel, page=1, activity='unavailable')['total'] == 5


def test_api_still_requires_auth(panel):
    assert panel.get('/api/members?activity=inactive&page=1').status_code == 401
