"""Synthetic confirmed intervals for aggregation tests, not production imports."""
import uuid


def verified(db, uid, start, seconds, *, title='Demo', kind='Movie', series='', item='demo', key=None):
    key = key or uuid.uuid4().hex
    with db.write() as conn:
        conn.execute('INSERT INTO watch_verified_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (key, uid, item, title, kind, series, 'TestClient', 'DirectStream',
                      start, start+seconds, seconds, 0, seconds, 1))
        conn.execute('INSERT INTO watch_verified_totals VALUES(?,?,?,?) '
                     'ON CONFLICT(emby_user_id) DO UPDATE SET seconds=seconds+excluded.seconds,'
                     'first_at=MIN(first_at,excluded.first_at),last_at=MAX(last_at,excluded.last_at)',
                     (uid, seconds, start, start+seconds))
    return key
