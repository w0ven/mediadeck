"""Window boundaries, restart durability and member-initiated Telegram reassignment."""
import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from test_bot_account_center import base_bot, bot, cb, msg  # noqa: F401
from test_membership import _session

from app.adapters.mock import MockEmby
from app.core.db import Database
from app.main import app
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.stats import StatsService
from app.modules.usage import UsageSampler


@pytest.fixture
def rb(request):
    return request.getfixturevalue('bot')


@pytest.fixture
def sampled(tmp_path, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr('app.modules.usage.time.time', lambda: clock[0])
    path = tmp_path / 'sample.db'
    db = Database(path)
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups)
    members.upsert('u1', 'viewer', {'group_id': 'standard'})
    emby = MockEmby()
    emby.set_sessions([_session('s', 'u1', 8_000_000)])
    sampler = UsageSampler(db, members, emby)
    return db, members, emby, sampler, clock, path


def test_crossing_old_session_is_not_false_zero(rb):
    now = time.time()
    rb.db.execute('INSERT INTO play_events(emby_user_id,seconds,started_at,ended_at) VALUES(?,?,?,?)',
                  ('u1', 1200, now-86400-600, now-86400+1800))
    summary = rb._stats.watch_summary('u1', now)
    assert summary['incomplete_24h'] and summary['seconds_24h'] == 0
    assert summary['seconds_30d'] == summary['recorded_seconds'] == 1200
    text = rb._watch_text(rb.members.get('u1'))
    assert '有跨界历史，时长无法完整还原' in text
    assert '近24小时观看：0秒' not in text


def test_sample_crossing_24h_is_clipped_exactly(sampled):
    db, _members, _emby, sampler, clock, _path = sampled
    asyncio.run(sampler.tick())
    clock[0] += 30
    asyncio.run(sampler.tick())
    stats = StatsService(db)
    stats.bind_live_watch(sampler.live_watch)
    now = clock[0] - 10 + 86400
    out = stats.watch_summary('u1', now)
    assert out['seconds_24h'] == 10 and not out['incomplete_24h']
    assert out['recorded_seconds'] == 30
    assert stats.watch_window(clock[0]-20, clock[0]-5, 'u1')['seconds'] == 15
    # Replay the exact sample: durable totals do not advance twice.
    sampler._record_watch('s', sampler._live['s'], clock[0]-30, clock[0])
    assert stats.watch_summary('u1', now)['recorded_seconds'] == 30


def test_restart_skips_outage_preserves_live_then_finished_once(sampled):
    db, members, emby, sampler, clock, path = sampled
    asyncio.run(sampler.tick())
    clock[0] += 30
    asyncio.run(sampler.tick())
    db.close()
    db = Database(path)
    members = MemberService(db, GroupService(db))
    sampler = UsageSampler(db, members, emby)
    stats = StatsService(db)
    stats.bind_live_watch(sampler.live_watch)
    assert stats.watch_summary('u1')['recorded_seconds'] == 30
    clock[0] += 3600
    asyncio.run(sampler.tick())
    assert stats.watch_summary('u1')['recorded_seconds'] == 30
    clock[0] += 15
    asyncio.run(sampler.tick())
    assert stats.watch_summary('u1')['recorded_seconds'] == 45
    emby.set_sessions([])
    clock[0] += 15
    asyncio.run(sampler.tick())
    assert db.one('SELECT seconds,sampled FROM play_events') == {'seconds':45, 'sampled':1}
    assert stats.watch_summary('u1')['recorded_seconds'] == 45
    assert not db.query('SELECT * FROM watch_checkpoints')
    assert not db.query('SELECT * FROM watch_totals')
    db._migrate()
    assert stats.watch_summary('u1')['recorded_seconds'] == 45
    clock[0] += 500 * 86400
    stats.prune(400)
    assert not db.query('SELECT * FROM watch_samples')
    assert stats.watch_summary('u1')['recorded_seconds'] == 45
    db.close()


def test_pause_resume_long_gap_and_title_change(sampled):
    db, _members, emby, sampler, clock, _path = sampled
    asyncio.run(sampler.tick())
    clock[0] += 30
    asyncio.run(sampler.tick())
    emby.set_sessions([_session('s', 'u1', 8_000_000, paused=True)])
    clock[0] += 30
    asyncio.run(sampler.tick())
    emby.set_sessions([_session('s', 'u1', 8_000_000)])
    clock[0] += 60
    asyncio.run(sampler.tick())
    assert StatsService(db).watch_summary('u1')['recorded_seconds'] == 30
    clock[0] += 30
    asyncio.run(sampler.tick())
    clock[0] += 600
    asyncio.run(sampler.tick())
    assert StatsService(db).watch_summary('u1')['recorded_seconds'] == 60
    emby.set_sessions([_session('s', 'u1', 8_000_000, item='second')])
    clock[0] += 30
    asyncio.run(sampler.tick())
    clock[0] += 30
    asyncio.run(sampler.tick())
    assert StatsService(db).watch_summary('u1')['recorded_seconds'] == 90
    assert db.one('SELECT item_id,seconds FROM play_events') == {'item_id':'i1','seconds':60}


def test_watch_queries_do_not_grow_with_member_count(rb):
    for i in range(30):
        rb.members.upsert(f'x{i}', f'viewer{i}', {'group_id': 'standard'})
    statements = []
    rb.db._conn.set_trace_callback(statements.append)
    rb._stats.top_watchers()
    rb.db._conn.set_trace_callback(None)
    assert len([s for s in statements if s.startswith('SELECT')]) <= 5


def test_measured_month_ignores_estimates_and_applies_credit_once(rb):
    month = datetime.now(UTC).strftime('%Y-%m')
    rb.db.execute('INSERT INTO measured_usage_monthly(node,emby_user_id,month,bytes,utag) VALUES(?,?,?,?,?)', ('n1','u1',month,1000,1))
    rb.db.execute('INSERT INTO measured_usage_monthly(node,emby_user_id,month,bytes,utag) VALUES(?,?,?,?,?)', ('n2','u1',month,2000,1))
    rb.db.execute('INSERT INTO measured_credits(emby_user_id,month,credit_bytes,updated_at) VALUES(?,?,?,?)', ('u1',month,500,1))
    assert rb._stats.measured_month()['by_user']['u1'] == 2500
    out = rb._stats.overview()
    assert out['traffic']['month_bytes'] == 2500
    assert out['traffic']['window_bytes'] is None


def test_member_entry_handoff_password_then_group_approval(rb):
    rb._bot_username = 'example_bot'
    assert 'rebind' in str(rb.member_menu()) and 'rebind' in str(rb.info_menu())
    asyncio.run(rb._handle_callback(cb('rebind', user='901')))
    assert rb._pending['901'][0] == 'rebind_target'
    asyncio.run(rb._handle_message(msg('903', user='901')))
    links = [b['url'] for _, payload in rb.calls for row in payload.get('reply_markup',{}).get('inline_keyboard',[]) for b in row if 'url' in b and 'rebind_' in b['url']]
    assert links
    token = links[-1].split('rebind_')[1]
    assert len('rebind_'+token) <= 64
    row = rb.db.one('SELECT * FROM tg_rebind_handoffs')
    assert row['token_hash'] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in json.dumps(row)
    asyncio.run(rb._handle_message(msg('/start rebind_'+token)))
    asyncio.run(rb._handle_message(msg('alice private-test-only')))
    req = rb.db.one("SELECT * FROM tg_requests WHERE kind='rebind'")
    assert req['status']=='pending' and rb.members.get('u1')['tg_user_id']=='901'
    with pytest.raises(ValueError):
        rb._rebinding.open_handoff(token,'903')
    assert 'private-test-only' not in str(rb.calls)


@pytest.mark.parametrize('case', ['wrong_user','expired','binding_changed','wrong_account'])
def test_handoff_rejects_wrong_target_and_stale_intent(rb, case):
    token = rb._rebinding.handoff('901','903')
    if case == 'expired':
        rb.db.execute('UPDATE tg_rebind_handoffs SET expires_at=1')
    if case == 'binding_changed':
        rb.members.bind_telegram('u1','904','moved',actor='test')
    with pytest.raises(ValueError):
        if case == 'wrong_account':
            rb._rebinding.create('admin1','903','new',handoff=token)
        else:
            rb._rebinding.open_handoff(token, '905' if case=='wrong_user' else '903')
    assert not rb.db.query('SELECT * FROM tg_requests')


def test_new_tg_rescue_and_notification_retry_are_independent_of_old_tg(rb):
    original = rb._call
    async def failed_group(method, payload=None, timeout=20):
        if method == 'sendMessage' and str(payload['chat_id']).startswith('-'):
            raise RuntimeError('offline')
        return await original(method,payload,timeout)
    rb._call = failed_group
    asyncio.run(rb._handle_message(msg('/rebind')))
    asyncio.run(rb._handle_message(msg('alice private-test-only')))
    request_id = rb.db.one("SELECT id FROM tg_requests")['id']
    assert not rb.db.query('SELECT * FROM tg_rebind_notices')
    rb._call = original
    asyncio.run(rb._handle_callback(cb(f'rebind_retry:{request_id}')))
    assert len(rb.db.query('SELECT * FROM tg_rebind_notices')) == 1
    asyncio.run(rb._handle_callback(cb(f'rebind_retry:{request_id}')))
    assert len(rb.db.query('SELECT * FROM tg_rebind_notices')) == 1
    assert rb.members.get('u1')['tg_user_id']=='901'


def test_web_review_removed_and_member_watch_exposed():
    with TestClient(app) as client:
        auth=('admin','change-me')
        assert client.get('/api/telegram/requests',auth=auth).status_code == 410
        assert client.post('/api/telegram/requests/1/review',auth=auth,json={'approve':True}).status_code == 410
        app.state.members.upsert('test','viewer',{'group_id':'standard'})
        data = client.get('/api/members/test',auth=auth).json()
        assert data['watch']['window_basis']=='sample_intervals'
