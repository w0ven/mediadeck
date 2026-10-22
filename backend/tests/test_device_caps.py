"""Per-level signed-in device caps: who is capped and which devices are signed out."""
import asyncio
import json
import time

from app.adapters.mock import MockEmby
from app.modules.levels import defaults, validate
from app.modules.member_devices import MemberDevices, device_cap

NOW = 1_800_000_000


def iso(ts):
    return time.strftime('%Y-%m-%dT%H:%M:%S.0000000Z', time.gmtime(ts))


def device(device_id, user, age):
    return {'Id': device_id, 'Name': f'Device {device_id}', 'AppName': 'Player', 'LastUserId': user,
            'DateLastActivity': iso(NOW - age)}


def test_device_cap_by_group_role_and_level():
    level = {'level': 2, 'max_devices': 4}
    assert device_cap(None) is None
    assert device_cap({'group_id': 'whitelist'}) == 10
    assert device_cap({'group_id': 'whitelist', 'roles': 'admin'}) is None
    assert device_cap({'group_id': 'standard', 'activity_managed': True, 'activity_level': level}) == 4
    assert device_cap({'group_id': 'standard', 'activity_level': level}) is None


def test_enforce_signs_out_least_recently_used_beyond_cap():
    emby = MockEmby()
    admin = next(iter(emby._users))
    emby._users[admin].setdefault('Policy', {})['IsAdministrator'] = True
    emby._devices = [
        device('a1', 'm1', 60 * 60), device('a2', 'm1', 3 * 86400), device('a3', 'm1', 20 * 86400),
        device('a4', 'm1', 40 * 86400), device('a5', 'm1', 5 * 60),
        device('b1', 'm2', 10), device('b2', 'm2', 20),
        device('c1', admin, 1), device('c2', admin, 2), device('c3', admin, 3),
        device('d1', 'free', 1), device('d2', 'free', 2),
    ]
    caps = {'m1': 3, 'm2': 1, admin: 1, 'free': None}
    service = MemberDevices(lambda: emby)
    removed = asyncio.run(service.enforce(caps.get, now=NOW))
    # m1 keeps its three most recent; m2's extra device was used minutes ago, so it waits.
    assert sorted(r['id'] for r in removed) == ['a3', 'a4']
    assert {d['Id'] for d in emby._devices} == {'a1', 'a2', 'a5', 'b1', 'b2', 'c1', 'c2', 'c3', 'd1', 'd2'}
    assert all(r['user_id'] == 'm1' and r['cap'] == 3 for r in removed)
    later = asyncio.run(service.enforce(caps.get, now=NOW + 3600))
    assert [r['id'] for r in later] == ['b2']


def test_enforce_respects_budget():
    emby = MockEmby()
    emby._devices = [device(f'x{i}', 'm1', 86400 * (i + 1)) for i in range(10)]
    service = MemberDevices(lambda: emby)
    first = asyncio.run(service.enforce(lambda uid: 2, budget=3, now=NOW))
    assert [r['id'] for r in first] == ['x2', 'x3', 'x4']
    rest = asyncio.run(service.enforce(lambda uid: 2, budget=40, now=NOW))
    assert len(rest) == 5 and {d['Id'] for d in emby._devices} == {'x0', 'x1'}


def test_level_config_gains_device_caps_and_validates():
    config = defaults()
    assert [row['max_devices'] for row in config['levels']] == [3, 4, 5, 6, 7, 8, 9, 10]
    validate(config)
    config['levels'][0]['max_devices'] = 0
    try:
        validate(config)
    except ValueError:
        pass
    else:
        raise AssertionError('zero devices accepted')


def test_saved_config_without_caps_reads_defaults(tmp_path):
    from app.core.db import Database
    from app.modules.levels import LevelService
    db = Database(tmp_path / 'levels.db')
    old = defaults()
    for row in old['levels']:
        row.pop('max_devices')
    db.execute('INSERT INTO activity_config(id,revision,config_json) VALUES(1,3,?)', (json.dumps(old),))
    service = LevelService(db, None, None, None)
    read = service.config()
    assert read['revision'] == 3
    assert [row['max_devices'] for row in read['config']['levels']] == [3, 4, 5, 6, 7, 8, 9, 10]
