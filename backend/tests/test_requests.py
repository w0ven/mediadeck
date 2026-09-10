"""Request-center lifecycle, preserved quota/history, concurrency and admin APIs."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.core.db import Database
from app.main import app
from app.modules.groups import GroupService
from app.modules.members import MemberService
from app.modules.requests import RequestError, RequestService, current_period, normalize_demand


@pytest.fixture()
def stack(tmp_path):
    db = Database(tmp_path / 'requests.db')
    groups = GroupService(db); groups.seed_defaults()
    members = MemberService(db, groups)
    for uid, tg, roles in [('u1','900',[]),('u2','901',[]),('up1','801',['uploader']),('up2','802',['uploader']),('admin1','800',['admin'])]:
        members.upsert(uid, uid, {'group_id':'standard'}, actor='test')
        members.bind_telegram(uid, tg, actor='test')
        members.set_roles(uid, roles, actor='test')
    service = RequestService(db, members, groups)
    yield db, groups, members, service
    db.close()


def create(s, uid='u1', tmdb=550, kind='movie', **kw):
    return asyncio.run(s.create(uid, kind, tmdb, **kw))


def test_quota_debit_is_atomic_once_and_only_at_creation(stack):
    db, _, _, s = stack
    assert s.remaining('u1') == 3
    row = create(s, create_key='once')
    assert row['status'] == 'open' and row['status_label'] == '待处理'
    assert s.remaining('u1') == 2
    again = create(s, create_key='once')
    assert again['id'] == row['id'] and not again['created']
    assert s.remaining('u1') == 2
    s.finish(row['id'], 'up1', 'rejected')
    assert s.remaining('u1') == 2
    create(s, tmdb=551); create(s, tmdb=552)
    with pytest.raises(RequestError, match='已用完'):
        create(s, tmdb=553)
    assert len(s.list()) == 3
    db.execute("UPDATE members SET request_period='2000-01' WHERE emby_user_id='u1'")
    assert s.remaining('u1') == 3
    create(s, tmdb=554)
    assert s.used('u1') == 1


def test_unlimited_group_preserves_usage(stack):
    _, groups, _, s = stack
    groups.update('standard', {'request_quota':0})
    for n in range(5):
        create(s, tmdb=n+1)
    assert s.remaining('u1') is None and s.used('u1') == 5


def test_duplicate_follows_even_with_no_quota_and_cannot_modify_other_user(stack):
    _, _, _, s = stack
    original = create(s)
    for n in range(3):
        create(s, uid='u2', tmdb=n+1)
    follow = create(s, uid='u2')
    assert follow['id'] == original['id'] and follow['followed'] and not follow['created']
    assert s.used('u2') == 3
    assert s.list(user_id='u2', followed=True)[0]['id'] == original['id']
    for call in [lambda:s.modify(original['id'],'u2',original['demand'],'x',1), lambda:s.finish(original['id'],'u2','cancelled'),lambda:s.message(original['id'],'u2','x')]:
        with pytest.raises(RequestError): call()


def test_demand_key_normalizes_and_distinguishes_versions_and_episodes(stack):
    _, _, _, s = stack
    a = create(s, kind='tv', demand={'scope':'episodes','seasons':'1','episodes':'3,1-2'})
    b = create(s, uid='u2', kind='tv', demand={'scope':'episodes','seasons':[1],'episodes':[1,2,3]})
    assert a['id'] == b['id']
    c = create(s, kind='tv', demand={'scope':'episodes','seasons':[1],'episodes':[4]})
    assert c['id'] != a['id']
    d = create(s, demand={'scope':'version','version':'Director Cut','quality':'4K'})
    e = create(s, uid='u2', demand={'scope':'version','version':'director cut','quality':'4k'})
    assert d['id'] == e['id']


@pytest.mark.parametrize('raw', [{'scope':'episodes','seasons':[1,2],'episodes':[1]}, {'scope':'season'}, {'scope':'version'}, {'scope':'episodes','seasons':[1],'episodes':'1-99999'}])
def test_invalid_requirements_rejected(raw):
    with pytest.raises(RequestError): normalize_demand('tv',raw)


@pytest.mark.parametrize('status', ['accepted','rejected','cancelled'])
def test_terminal_state_stale_buttons_and_followers_outbox(stack,status):
    db, _, _, s = stack
    row = create(s); s.follow(row['id'],'u2')
    actor = 'u1' if status=='cancelled' else 'up1'
    result = s.finish(row['id'],actor,status,revision=1)['request']
    assert result['status'] == status and result['revision']==2
    assert result['result_note'] == ''
    for other in ('accepted','rejected','cancelled'):
        with pytest.raises(RequestError): s.finish(row['id'],actor,other,revision=1)
    with pytest.raises(RequestError): s.message(row['id'],'u1','late')
    assert s.used('u1')==1
    outbox=db.query("SELECT * FROM request_outbox WHERE kind='result'")
    assert {r['user_id'] for r in outbox}=={'u1','u2'} and len(outbox)==2


def test_acceptance_is_final_and_same_accepted_demand_cannot_charge_again(stack):
    _, _, _, s = stack
    row=create(s); s.finish(row['id'],'up1','accepted')
    with pytest.raises(RequestError,match='已接受'): create(s)
    different=create(s,demand={'scope':'version','version':'4K 修复版'})
    assert different['created'] and s.used('u1')==2
    with pytest.raises(RequestError,match='旧'): s.claim(row['id'],'up2')
    with pytest.raises(RequestError,match='旧'): s.resolve(row['id'],'up1',True)


def test_concurrent_finalization_has_only_one_winner(stack):
    db, _, _, s = stack
    row=create(s)
    def attempt(n):
        try: return s.finish(row['id'],'up1' if n%2 else 'up2','accepted' if n%2 else 'rejected')['ok']
        except RequestError: return False
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt,range(16)))==1
    assert len(db.query("SELECT * FROM request_events WHERE kind IN ('accepted','rejected')"))==1
    assert len(db.query("SELECT * FROM request_outbox WHERE kind='result'"))==1


def test_concurrent_create_charges_only_one_user(stack):
    _, _, _, s = stack
    async def race():
        return await asyncio.gather(*(s.create('u1' if i%2 else 'u2','movie',550) for i in range(10)))
    rows=asyncio.run(race())
    assert len({r['id'] for r in rows})==1
    assert sum(r['created'] for r in rows)==1
    assert s.used('u1')+s.used('u2')==1


def test_question_reply_internal_and_edit_keep_pending_and_record_idempotently(stack):
    _, _, _, s=stack
    row=create(s)
    s.message(row['id'],'up1','请确认版本',staff=True,key='q')
    s.message(row['id'],'up1','请确认版本',staff=True,key='q')
    s.message(row['id'],'u1','原版即可',key='r')
    s.message(row['id'],'up2','不要外传',internal=True)
    assert s.get(row['id'])['status']=='open'
    assert len([e for e in s.events(row['id']) if e['kind']=='question'])==1
    assert '不要外传' not in json.dumps(s.events(row['id']),ensure_ascii=False)
    assert '不要外传' in json.dumps(s.events(row['id'],internal=True),ensure_ascii=False)
    new=s.modify(row['id'],'u1',dict(row['demand'],quality='4K'),'谢谢',1)
    assert new['revision']==2 and s.used('u1')==1
    with pytest.raises(RequestError): s.finish(row['id'],'up1','accepted',revision=1)


def test_permissions_rechecked_and_internal_messages_never_in_outbox(stack):
    db, _, members, s=stack
    row=create(s)
    with pytest.raises(RequestError): s.finish(row['id'],'u2','accepted')
    members.set_roles('up1',[],actor='test')
    with pytest.raises(RequestError): s.message(row['id'],'up1','q',staff=True)
    s.message(row['id'],'up2','private-note',internal=True)
    assert 'private-note' not in json.dumps(db.query('SELECT * FROM request_outbox'))


def test_admin_correction_refund_auditable_once_no_month_windfall(stack):
    db, _, _, s=stack
    row=create(s); s.finish(row['id'],'up1','rejected')
    with pytest.raises(RequestError): s.correct(row['id'],'up1','open','mistake',2)
    corrected=s.correct(row['id'],'admin1','open','误拒绝',2)
    assert corrected['status']=='open' and s.used('u1')==1
    with pytest.raises(RequestError): s.finish(row['id'],'up1','accepted',revision=1)
    s.refund(row['id'],'admin1','人工修正')
    assert s.used('u1')==0
    with pytest.raises(RequestError,match='已退回'): s.refund(row['id'],'admin1','again')
    assert {e['kind'] for e in s.events(row['id'],internal=True)} >= {'correction','refund'}
    row2=create(s,tmdb=551)
    db.execute('UPDATE media_requests SET created_at=1 WHERE id=?',(row2['id'],))
    with pytest.raises(RequestError,match='月份'): s.refund(row2['id'],'admin1','old')


def test_additive_legacy_migration_preserves_rows_quota_and_does_not_notify(tmp_path):
    path=tmp_path/'old.db'
    db=Database(path)
    groups=GroupService(db); groups.seed_defaults(); members=MemberService(db,groups)
    members.upsert('u','alice',{'group_id':'standard'})
    db.execute('UPDATE members SET request_used=3,request_period=? WHERE emby_user_id=?',(current_period(),'u'))
    # Simulate the old schema and real old states, not new-service test fixtures.
    db.close(); conn=sqlite3.connect(path)
    conn.execute('DROP TABLE media_requests')
    conn.execute("CREATE TABLE media_requests(id INTEGER PRIMARY KEY AUTOINCREMENT,emby_user_id TEXT NOT NULL,username TEXT DEFAULT '',tg_user_id TEXT DEFAULT '',tmdb_id INTEGER NOT NULL,media_type TEXT NOT NULL,title TEXT DEFAULT '',year INTEGER,poster_path TEXT DEFAULT '',note TEXT DEFAULT '',status TEXT DEFAULT 'open',claimed_by TEXT DEFAULT '',claimed_at INTEGER,resolved_at INTEGER,result_note TEXT DEFAULT '',created_at INTEGER NOT NULL)")
    for n,state in enumerate(('open','claimed','done','rejected'),1):
        conn.execute('INSERT INTO media_requests(id,emby_user_id,tmdb_id,media_type,status,claimed_by,note,created_at) VALUES(?,?,?,?,?,?,?,?)',(n,'u',n,'movie',state,'historic-uploader','原始备注',100))
    conn.execute("CREATE UNIQUE INDEX idx_mreq_open_title ON media_requests(tmdb_id,media_type) WHERE status IN ('open','claimed')")
    conn.commit();conn.close()
    for _ in range(2):
        db=Database(path)
        rows=db.query('SELECT * FROM media_requests ORDER BY id')
        assert [r['status'] for r in rows]==['open','accepted','accepted','rejected']
        assert [r['legacy_status'] for r in rows]==['','claimed','done','']
        assert all(r['note']=='原始备注' and r['claimed_by']=='historic-uploader' for r in rows)
        assert db.one('SELECT request_used FROM members')['request_used']==3
        assert not db.query('SELECT * FROM request_outbox')
        assert not db.query('SELECT * FROM request_events')
        db.close()


def test_filters_pagination_and_library_failure_does_not_imply_absence(stack):
    _, groups, _, s=stack
    groups.update('standard',{'request_quota':0})
    for n in range(13): create(s,tmdb=n+1,kind='movie' if n%2 else 'tv')
    assert len(s.list(limit=5,offset=5))==5
    assert all(r['media_type']=='tv' for r in s.list(media_type='tv'))
    assert len(s.list(search='13'))==1
    hint=asyncio.run(s.library_hint('movie',550,'u1'))
    assert hint['available'] is False
    assert create(s,tmdb=550)['status']=='open'


def test_outbox_leases_and_retry_never_mutate_request_or_quota(stack):
    db, _, _, s=stack
    row=create(s); s.finish(row['id'],'up1','accepted')
    before=s.get(row['id'])
    first=s.claim_delivery();second=s.claim_delivery()
    assert first['id']!=second['id']
    s.delivery_result(first['id'],False);s.retry_notifications(row['id'])
    assert s.claim_delivery()['id']==first['id']
    s.delivery_result(first['id'],True,77)
    assert s.get(row['id'])==before and s.used('u1')==1
    assert db.one('SELECT state FROM request_outbox WHERE id=?',(first['id'],))['state']=='sent'


def test_management_api_legacy_410_conflict_optional_reason_and_audit():
    auth=('admin','change-me')
    with TestClient(app) as client:
        members=app.state.members
        members.upsert('u1','alice',{'group_id':'standard'})
        row=create(app.state.requests)
        assert client.get('/api/requests').status_code==401
        for old in ('claim','resolve'):
            assert client.post(f'/api/requests/{row["id"]}/{old}',auth=auth,json={'done':True}).status_code==410
        assert client.post(f'/api/requests/{row["id"]}/accept',auth=auth,json={}).status_code==400
        assert client.post(f'/api/requests/{row["id"]}/reject',auth=auth,json={'revision':1}).status_code==200
        detail=client.get(f'/api/requests/{row["id"]}',auth=auth).json()
        assert detail['status']=='rejected' and detail['result_note']==''
        assert client.post(f'/api/requests/{row["id"]}/accept',auth=auth,json={'revision':1}).status_code==409
        assert client.post(f'/api/requests/{row["id"]}/correct',auth=auth,json={'revision':2,'status':'open','reason':'误点'}).status_code==200
        assert client.post(f'/api/requests/{row["id"]}/messages',auth=auth,json={'revision':3,'body':'内部信息','internal':True,'key':'once'}).status_code==200
        assert client.post(f'/api/requests/{row["id"]}/refund',auth=auth,json={'reason':'人工调整'}).status_code==200
        assert app.state.requests.used('u1')==0


ADMIN = ("admin", "change-me")

def test_the_tmdb_key_never_comes_back_from_the_settings_api() -> None:
    creds = "tmdb" + "-" + "placeholder-not-a-real-key"
    with TestClient(app) as client:
        saved = client.put("/api/settings/integration", auth=ADMIN, json={
            "tmdb_api_key": creds, "tmdb_language": "en-US"}).json()
        assert creds not in str(saved)
        assert saved["tmdb_api_key_set"] is True
        assert saved["tmdb_language"] == "en-US"

        fetched = client.get("/api/settings/integration", auth=ADMIN).json()
        assert creds not in str(fetched)
        assert "tmdb_api_key" not in fetched

        everything = client.get("/api/settings", auth=ADMIN).json()
        assert creds not in str(everything)


def test_editing_the_language_does_not_wipe_the_stored_key() -> None:
    creds = "tmdb" + "-" + "placeholder-not-a-real-key"
    with TestClient(app) as client:
        client.put("/api/settings/integration", auth=ADMIN,
                   json={"tmdb_api_key": creds})
        after = client.put("/api/settings/integration", auth=ADMIN,
                           json={"tmdb_language": "ja-JP"}).json()
        assert after["tmdb_api_key_set"] is True
        assert after["tmdb_language"] == "ja-JP"


def test_the_group_api_round_trips_the_request_quota() -> None:
    with TestClient(app) as client:
        created = client.post("/api/groups", auth=ADMIN, json={
            "id": "reqtest", "name": "求片测试组", "billing_mode": "none",
            "request_quota": 7}).json()
        assert created["request_quota"] == 7

        updated = client.put("/api/groups/reqtest", auth=ADMIN,
                             json={"request_quota": 0}).json()
        assert updated["request_quota"] == 0


def test_the_whitelist_group_is_present_on_a_booted_panel() -> None:
    with TestClient(app) as client:
        ids = {g["id"] for g in client.get("/api/groups", auth=ADMIN).json()}
        assert "whitelist" in ids



def test_independent_connections_concurrent_create_and_refund(stack):
    db, _, _, s = stack
    second_db=Database(db.path)
    groups=GroupService(second_db);members=MemberService(second_db,groups)
    second=RequestService(second_db,members,groups)
    try:
        def start(i):
            return create(s if i%2 else second,uid='u1' if i%2 else 'u2')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            rows=list(pool.map(start,range(8)))
        assert len({r['id'] for r in rows})==1
        assert s.used('u1')+s.used('u2')==1
        def refund(service):
            try: service.refund(rows[0]['id'],'admin1','manual');return True
            except RequestError:return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            assert sum(pool.map(refund,[s,second]))==1
    finally:second_db.close()


def test_library_adapter_reads_user_scoped_exact_tmdb_and_rejects_incomplete_response(stack):
    import httpx

    from app.adapters.live import LiveEmby
    _, _, _, service=stack
    calls=[]
    broken=False
    def handler(request):
        calls.append(request)
        assert request.method=='GET'
        assert request.url.params['UserId']=='u1'
        if broken:return httpx.Response(200,json={'unexpected':[]})
        if request.url.params.get('IncludeItemTypes')=='Episode':
            return httpx.Response(200,json={'Items':[{'ParentIndexNumber':1,'IndexNumber':2}],'TotalRecordCount':1})
        assert request.url.params['AnyProviderIdEquals']=='tmdb.550'
        return httpx.Response(200,json={'Items':[{'Id':'series-a','Name':'现有剧集','ProviderIds':{'Tmdb':'550'}}]})
    library=LiveEmby(lambda:{'enabled':True,'url':'https://emby.invalid','api_key':'local-test'})
    library._client=lambda timeout,verify:httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service._library=library
    result=asyncio.run(service.library_hint('tv',550,'u1'))
    assert result['available'] and result['items'][0]['episodes']==[{'season':1,'episode':2}]
    assert 'series-a' in result['items'][0]['url']
    broken=True
    result=asyncio.run(service.library_hint('movie',550,'u1'))
    assert result['available'] is False and '未知是否已有' in result['message']
    assert create(service)['created']
