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


def test_live_device_token_state_filters_ghosts_and_protects_shared_and_system_devices(tmp_path):
    import sqlite3
    import uuid

    import pytest

    from app.adapters.live import LiveEmby
    from app.core.errors import UpstreamError

    u_member = '11111111222233334444555566667777'
    u_admin = 'aaaaaaaa222233334444555566667777'
    u_other = 'bbbbbbbb222233334444555566667777'
    with sqlite3.connect(tmp_path / 'users.db') as users:
        users.execute('CREATE TABLE LocalUsersv2 (Id INTEGER, guid BLOB)')
        users.executemany('INSERT INTO LocalUsersv2 VALUES (?, ?)', [
            (1, uuid.UUID(u_admin).bytes_le),
            (2, u_other),
            (7, uuid.UUID(u_member).bytes_le),
        ])
    with sqlite3.connect(tmp_path / 'authentication.db') as auth:
        auth.execute(
            'CREATE TABLE Tokens_2 (Id INTEGER PRIMARY KEY, AccessToken TEXT, DeviceId INT, '
            'AppName TEXT, AppVersion TEXT, UserId INT, IsActive INT, '
            'DateCreatedInt INT, DateLastActivityInt INT)'
        )
        auth.executemany('INSERT INTO Tokens_2 VALUES (?,?,?,?,?,?,?,?,?)', [
            # API key device (Id=3) mixed with old inactive user token: never dead.
            (1, 'apikey', 3, 'Panel Key', '1.0', None, 1, NOW - 9000, NOW - 10),
            (2, 'oldbot', 3, 'Bot', '1.0', 7, 0, NOW - 9000, NOW - 8000),
            # Shared device (Id=90): u_admin active, u_other signed out more recently.
            (3, 'adm', 90, 'RodelPlayer', '2.0', 1, 1, NOW - 5000, NOW - 3000),
            (4, 'oth', 90, 'RodelPlayer', '2.1', 2, 0, NOW - 2000, NOW - 100),
            # Member: one live install (35) and two signed-out ghosts (29, 31).
            (5, 'live', 35, 'PlayerA', '2.1.0', 7, 1, NOW - 1000, NOW - 30),
            (6, 'ghost1', 29, 'PlayerA', '2.1.0', 7, 0, NOW - 86400 * 40, NOW - 86400 * 30),
            (7, 'ghost2', 31, 'PlayerA', '2.1.0', 7, 0, NOW - 86400 * 40, NOW - 86400 * 31),
        ])

    unconfigured = LiveEmby(lambda: {'enabled': True, 'url': 'http://emby.local', 'api_key': 'k'})
    assert asyncio.run(unconfigured.device_token_state()) is None

    mismatched = LiveEmby(
        lambda: {'enabled': True, 'url': 'http://other.local', 'api_key': 'k'},
        identity_data_dir=str(tmp_path), identity_url='http://emby.local',
    )
    with pytest.raises(UpstreamError):
        asyncio.run(mismatched.device_token_state())

    live = LiveEmby(
        lambda: {'enabled': True, 'url': 'http://emby.local', 'api_key': 'k'},
        identity_data_dir=str(tmp_path), identity_url='http://emby.local',
    )
    state = asyncio.run(live.device_token_state())
    assert state['dead_device_ids'] == {'29', '31'}
    assert '3' not in state['dead_device_ids'] and '90' not in state['dead_device_ids']

    emby = MockEmby()
    emby._device_token_state = state
    emby._devices = [
        {'Id': '3', 'Name': 'API Host', 'AppName': 'Bot', 'LastUserId': u_member,
         'DateLastActivity': iso(NOW - 8000)},
        {'Id': '90', 'Name': 'Shared PC', 'AppName': 'RodelPlayer', 'AppVersion': '2.1',
         'LastUserId': u_other, 'DateLastActivity': iso(NOW - 100)},
        {'Id': '35', 'Name': 'Phone Model X', 'AppName': 'PlayerA', 'AppVersion': '2.1.0',
         'LastUserId': u_member, 'DateLastActivity': iso(NOW - 5)},
        {'Id': '29', 'Name': 'Phone Model Y', 'AppName': 'PlayerA', 'AppVersion': '2.1.0',
         'LastUserId': u_member, 'DateLastActivity': iso(NOW - 86400 * 30)},
        {'Id': '31', 'Name': 'Phone Model X', 'AppName': 'PlayerA', 'AppVersion': '2.1.0',
         'LastUserId': u_member, 'DateLastActivity': iso(NOW - 86400 * 31)},
    ]
    service = MemberDevices(lambda: emby)
    member_devices = asyncio.run(service.for_user(u_member))
    assert [d['id'] for d in member_devices] == ['35']
    assert member_devices[0]['last_active'] == NOW - 5
    assert asyncio.run(service.for_user(u_other)) == []
    admin_devices = asyncio.run(service.for_user(u_admin))
    assert [(d['id'], d['version'], d['last_active']) for d in admin_devices] == [('90', '2.0', NOW - 3000)]


def test_enforce_ignores_ghosts_for_cap_and_sweeps_dead_rows_within_budget():
    emby = MockEmby()
    admin = next(iter(emby._users))
    emby._users[admin].setdefault('Policy', {})['IsAdministrator'] = True
    emby._devices = [
        device('live1', 'm1', 3600),
        device('live2', 'm1', 7200),
        device('live3', 'm1', 86400 * 5),
        # Signed out 10s ago: if counted as active, it would wrongly evict live3!
        device('ghost_recent', 'm1', 10),
        device('ghost_old', 'm1', 86400 * 20),
        device('ghost_admin', admin, 86400 * 20),
        {'Id': 'ghost_orphan', 'Name': 'Deleted User Phone', 'AppName': 'Player',
         'DateLastActivity': iso(NOW - 86400 * 20)},
    ]
    emby._device_token_state = {
        'active_by_device': {
            'live1': [{'user_id': 'm1', 'app': 'Player', 'version': '1', 'last_active': NOW - 3600}],
            'live2': [{'user_id': 'm1', 'app': 'Player', 'version': '1', 'last_active': NOW - 7200}],
            'live3': [{'user_id': 'm1', 'app': 'Player', 'version': '1', 'last_active': NOW - 86400 * 5}],
        },
        'dead_device_ids': {'ghost_recent', 'ghost_old', 'ghost_admin', 'ghost_orphan'},
    }
    service = MemberDevices(lambda: emby)
    removed = asyncio.run(service.enforce(lambda uid: 3, budget=1, now=NOW))
    assert removed == []
    # Only 1 dead member row swept under budget=1; live3 untouched; admin/orphan untouched.
    remaining = {d['Id'] for d in emby._devices}
    assert {'live1', 'live2', 'live3', 'ghost_admin', 'ghost_orphan'} <= remaining
    assert len(remaining & {'ghost_recent', 'ghost_old'}) == 1

    removed_next = asyncio.run(service.enforce(lambda uid: 3, budget=10, now=NOW))
    assert removed_next == []
    assert {d['Id'] for d in emby._devices} == {'live1', 'live2', 'live3', 'ghost_admin', 'ghost_orphan'}


def test_password_reset_invalidates_cache_and_prunes_user_dead_devices(tmp_path):
    from app.core.db import Database
    from app.modules.groups import GroupService
    from app.modules.members import MemberService
    from app.modules.telegram import TelegramBot

    db = Database(tmp_path / 'panel.db')
    groups = GroupService(db)
    groups.seed_defaults()
    members = MemberService(db, groups=groups)
    emby = MockEmby()
    user = asyncio.run(emby.create_user('demo_member'))
    uid = user['Id']
    members.upsert(uid, 'demo_member', {'group_id': 'standard'})
    emby._devices = [
        device('d1', uid, 60),
        device('d2', uid, 3600),
        device('other_d', 'someone_else', 60),
    ]
    emby._device_token_state = {
        'active_by_device': {
            'd1': [{'user_id': uid, 'app': 'Player', 'version': '1', 'last_active': NOW - 60}],
            'd2': [{'user_id': uid, 'app': 'Player', 'version': '1', 'last_active': NOW - 3600}],
            'other_d': [{'user_id': 'someone_else', 'app': 'Player', 'version': '1', 'last_active': NOW - 60}],
        },
        'dead_device_ids': set(),
    }
    service = MemberDevices(lambda: emby)
    # Prime the 20s cache before password change.
    assert [d['id'] for d in asyncio.run(service.for_user(uid))] == ['d1', 'd2']

    bot = TelegramBot(lambda: {'enabled': True}, members, emby=emby)
    bot._member_devices = service
    ok, notices = asyncio.run(bot._apply_password_reset(uid, 'NewPass99', 'miniapp:1001:custom'))
    assert ok is True and notices == []
    # Immediate query sees empty list (cache invalidated) and dead rows removed from Emby.
    assert asyncio.run(service.for_user(uid)) == []
    assert [d['Id'] for d in emby._devices] == ['other_d']


def test_same_device_name_and_player_merges_for_display_cap_remove_and_auto_logout():
    emby = MockEmby()
    emby._devices = [
        # Two active server lines on the same Mac + SenPlayer: should merge into 1 entry.
        {'Id': 'mac_new', 'Name': 'Mac', 'AppName': 'SenPlayer', 'AppVersion': '6.2.3',
         'LastUserId': 'm1', 'DateLastActivity': iso(NOW - 3000)},
        {'Id': 'mac_old', 'Name': ' Mac ', 'AppName': 'SenPlayer', 'AppVersion': '6.2.2',
         'LastUserId': 'm1', 'DateLastActivity': iso(NOW - 86400 * 6)},
        # Different apps on iPhone stay separate.
        {'Id': 'ip_libra', 'Name': 'iPhone', 'AppName': 'Libra', 'AppVersion': '1.0',
         'LastUserId': 'm1', 'DateLastActivity': iso(NOW - 7200)},
        {'Id': 'ip_rex', 'Name': 'iPhone17', 'AppName': 'Rex', 'AppVersion': '1.1',
         'LastUserId': 'm1', 'DateLastActivity': iso(NOW - 86400)},
        # Generic unknown names must never merge across distinct DeviceIds.
        {'Id': 'unk1', 'Name': 'Unknown', 'AppName': 'SenPlayer', 'AppVersion': '6.2.3',
         'LastUserId': 'm2', 'DateLastActivity': iso(NOW - 3600)},
        {'Id': 'unk2', 'Name': '未知设备', 'AppName': 'SenPlayer', 'AppVersion': '6.2.3',
         'LastUserId': 'm2', 'DateLastActivity': iso(NOW - 7200)},
    ]
    service = MemberDevices(lambda: emby)
    m1_list = asyncio.run(service.for_user('m1'))
    assert [(d['id'], d['name'], d['app'], d['version']) for d in m1_list] == [
        ('mac_new', 'Mac', 'SenPlayer', '6.2.3'),
        ('ip_libra', 'iPhone', 'Libra', '1.0'),
        ('ip_rex', 'iPhone17', 'Rex', '1.1'),
    ]
    assert [d['id'] for d in asyncio.run(service.for_user('m2'))] == ['unk1', 'unk2']

    # Cap of 3 logical devices is NOT exceeded for m1 despite 4 raw Emby IDs.
    assert asyncio.run(service.enforce(lambda uid: 3 if uid == 'm1' else None, now=NOW)) == []
    assert {'mac_new', 'mac_old', 'ip_libra', 'ip_rex'} <= {d['Id'] for d in emby._devices}

    # Removing the merged Mac entry revokes BOTH underlying Emby device IDs at once.
    removed_one = asyncio.run(service.remove('m1', 'mac_new'))
    assert removed_one['name'] == 'Mac' and removed_one['app'] == 'SenPlayer'
    assert {'mac_new', 'mac_old'}.isdisjoint({d['Id'] for d in emby._devices})
    assert [d['id'] for d in asyncio.run(service.for_user('m1'))] == ['ip_libra', 'ip_rex']


def test_enforce_signs_out_all_underlying_ids_of_evicted_merged_group():
    emby = MockEmby()
    emby._devices = [
        {'Id': 'keep1', 'Name': 'iPhone', 'AppName': 'Libra', 'AppVersion': '1.0',
         'LastUserId': 'm1', 'DateLastActivity': iso(NOW - 3600)},
        {'Id': 'old_line_a', 'Name': 'TV', 'AppName': 'Yamby', 'AppVersion': '2.1',
         'LastUserId': 'm1', 'DateLastActivity': iso(NOW - 86400 * 5)},
        {'Id': 'old_line_b', 'Name': 'TV', 'AppName': 'Yamby', 'AppVersion': '2.1',
         'LastUserId': 'm1', 'DateLastActivity': iso(NOW - 86400 * 10)},
    ]
    service = MemberDevices(lambda: emby)
    evicted = asyncio.run(service.enforce(lambda uid: 1, now=NOW))
    # One logical audit entry, both underlying IDs removed from Emby.
    assert len(evicted) == 1 and evicted[0]['name'] == 'TV' and evicted[0]['app'] == 'Yamby'
    assert [d['Id'] for d in emby._devices] == ['keep1']


