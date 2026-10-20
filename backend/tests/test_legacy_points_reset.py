"""Offline maintenance never refunds spent money, loses new earnings or erases history."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.db import Database
from tools.reset_legacy_points import REASON, apply, connect, preview

CUTOFF = 2000000000


@pytest.fixture
def conn(tmp_path):
    path = tmp_path/'ledger.db'
    db = Database(path)
    db.close()
    c = connect(path, writable=True)
    yield c
    c.close()


def add(c, uid, amount, reason='checkin', at=CUTOFF-1):
    balance = c.execute('SELECT COALESCE(SUM(delta),0) FROM points_ledger WHERE emby_user_id=?', (uid,)).fetchone()[0]
    c.execute('INSERT INTO points_ledger(emby_user_id,delta,balance_after,reason,ref,actor,created_at) VALUES(?,?,?,?,?,?,?)',
              (uid, amount, balance+amount, reason, '', 'test', at))


def test_all_accounts_old_first_exact_boundary_no_refund_and_idempotence(conn):
    for uid in ['ordinary','whitelist','admin','zero']:
        conn.execute('INSERT INTO members(emby_user_id,username,created_at,updated_at) VALUES(?,?,0,0)', (uid, uid))
    add(conn,'ordinary',900)
    add(conn,'ordinary',10,at=CUTOFF)
    add(conn,'ordinary',-888,'shop.redeem',CUTOFF+1)
    add(conn,'whitelist',40)
    add(conn,'whitelist',21,at=CUTOFF)
    add(conn,'admin',80)
    add(conn,'deleted',4)
    add(conn,'today',18,at=CUTOFF)
    report = preview(conn, CUTOFF, 'test-reset')
    assert report['summary'] == {'all_users':6,'clear_users':4,'retain_users':3,'current':185,'clear':136,'retain':49}
    rows = {r['user_id']:r for r in report['users']}
    assert rows['ordinary']['old_spent']==888 and rows['ordinary']['retain']==10 and rows['ordinary']['clear']==12
    assert not rows['deleted']['member_exists']
    before = [tuple(r) for r in conn.execute('SELECT * FROM points_ledger')]
    assert apply(conn,CUTOFF,'test-reset',report['hash'],now=CUTOFF+2)==report
    assert [tuple(r) for r in conn.execute('SELECT * FROM points_ledger WHERE reason<>?',(REASON,))]==before
    assert conn.execute('SELECT SUM(delta) FROM points_ledger').fetchone()[0]==49
    count = conn.execute('SELECT COUNT(*) FROM points_ledger').fetchone()[0]
    add(conn,'today',8,'watch.reward',CUTOFF+3)
    assert apply(conn,CUTOFF,'test-reset',report['hash'],now=CUTOFF+4)==report
    assert conn.execute('SELECT COUNT(*) FROM points_ledger').fetchone()[0]==count+1
    assert preview(conn,CUTOFF,'test-reset')==report
    with pytest.raises(ValueError, match='different reset'):preview(conn,CUTOFF,'other-batch')
    with pytest.raises(ValueError, match='different cutoff'):preview(conn,CUTOFF+1,'test-reset')


def test_today_partly_consumed_and_exhausted_buckets(conn):
    add(conn,'partial',5)
    add(conn,'partial',10,at=CUTOFF)
    add(conn,'partial',-12,'shop.redeem',CUTOFF+1)
    add(conn,'zero',5)
    add(conn,'zero',-5,'shop.redeem',CUTOFF)
    report=preview(conn,CUTOFF,'b')
    assert report['summary']['retain']==3 and report['summary']['clear']==0
    assert apply(conn,CUTOFF,'b',report['hash'],now=CUTOFF+2)==report
    assert conn.execute('SELECT COUNT(*) FROM points_ledger WHERE reason=?',(REASON,)).fetchone()[0]==0


@pytest.mark.parametrize('reason,delta',[('transfer.in',1),('transfer.out',-1),('play.payout',1),
    ('play.reserve',-1),('play.release',1),('shop.refund',1),('checkin',-1),('unknown',1)])
def test_ambiguous_sources_stop_before_mutation(conn,reason,delta):
    add(conn,'u',10)
    add(conn,'u',delta,reason,CUTOFF)
    with pytest.raises(ValueError,match='ambiguous'):preview(conn,CUTOFF,'b')
    assert conn.execute('SELECT COUNT(*) FROM economy_receipts').fetchone()[0]==0


@pytest.mark.parametrize('table,sql',[
    ('play_escrows', "INSERT INTO play_escrows(scope,ref,user_id,amount,reserved,created_at) VALUES('test','r','u',1,1,0)"),
    ('play_funds', "INSERT INTO play_funds(kind,ref,amount,received) VALUES('fee','r',1,1)"),
    ('red_packets', "INSERT INTO red_packets(nonce,chat_id,command_message_id,actor_tg_id,actor_user_id,actor_name,funding,mode,audience,total,parts,config_json,command_json,created_at,confirm_by,status) VALUES('r','1',1,'1','u','Demo','user','random','all',1,1,'{}','{}',0,10,'pending')")])
def test_unsettled_blocks(conn,table,sql):
    conn.execute(sql)
    with pytest.raises(ValueError,match=table):preview(conn,CUTOFF,'b')


def test_stale_preview_and_atomic_failure(conn):
    add(conn,'a',10);add(conn,'b',20)
    report=preview(conn,CUTOFF,'b')
    add(conn,'a',1,at=CUTOFF)
    with pytest.raises(ValueError,match='changed'):apply(conn,CUTOFF,'b',report['hash'],now=CUTOFF+1)
    report=preview(conn,CUTOFF,'b')
    conn.execute("CREATE TRIGGER reject_second BEFORE INSERT ON points_ledger WHEN NEW.reason='admin.reset_legacy' AND NEW.emby_user_id='b' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError,match='injected'):apply(conn,CUTOFF,'b',report['hash'],now=CUTOFF+1)
    assert conn.execute('SELECT COUNT(*) FROM points_ledger WHERE reason=?',(REASON,)).fetchone()[0]==0
    assert conn.execute('SELECT COUNT(*) FROM economy_receipts').fetchone()[0]==0


def test_negative_and_temporal_deficit_refused(conn):
    add(conn,'u',-1)
    with pytest.raises(ValueError,match='negative'):preview(conn,CUTOFF,'b')
    add(conn,'u',1)
    add(conn,'u',-3,'shop.redeem',CUTOFF)
    add(conn,'u',5,at=CUTOFF+1)
    with pytest.raises(ValueError,match='chronological'):preview(conn,CUTOFF,'b')


def test_cli_default_read_only_private_report(conn,tmp_path):
    add(conn,'u',10)
    path=conn.execute('PRAGMA database_list').fetchone()[2]
    report=tmp_path/'audit.json'
    cmd=[sys.executable,str(Path(__file__).parents[1]/'tools/reset_legacy_points.py'),
         '--database',path,'--cutoff',str(CUTOFF),'--batch','demo','--output',str(report)]
    run=subprocess.run(cmd,capture_output=True,text=True,check=True)
    assert json.loads(run.stdout)['mode']=='dry-run'
    assert 'user_id' not in run.stdout
    assert report.stat().st_mode & 0o777 == 0o600
    assert conn.execute('SELECT COUNT(*) FROM points_ledger').fetchone()[0]==1
    retry=subprocess.run(cmd,capture_output=True,text=True,check=False)
    assert retry.returncode!=0  # no silent audit overwrite
