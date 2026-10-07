"""Confirmed bonus-only cap and exact Mbps across actual handler/adapter paths."""
import asyncio
import json
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient
from test_economy import NOW, parallel
from test_economy import env as economy_env  # noqa: F401

from app.core.config import NodePool, StreamNode
from app.main import _inventory_expired_effects, _telegram_member_changed, app
from app.modules.playback import PlaybackRouter
from app.modules.signing import verify


@pytest.fixture
def env(request):
    e = request.getfixturevalue('economy_env')
    e.members.set_overrides('u', {'bandwidth_limit_kbps': 120000, 'max_streams': 2})
    e.points.add('u', 3000, 'isolated.test')
    return e


def buy(e, amount=100, days=1):
    item = e.shop.create({'kind': 'bandwidth_card', 'name': 'isolated 100Mbps',
                         'cost': 300, 'amount': amount, 'duration_days': days, 'enabled': True})
    return e.shop.redeem('u', item['id'])['card_id']


def test_positive_cap_only_contributions_not_base_and_expired_bonus_frees_capacity(env):
    env.registry.save('inventory', config={'bandwidth_cap_mbps': 100})
    a, b = buy(env), buy(env)
    used = env.bag.use('u', a)
    assert used['bandwidth_bonus_kbps'] == 100000 and env.members.get('u')['bandwidth_limit_kbps'] == 220000
    assert env.bag.use('u', a) == used
    with pytest.raises(ValueError, match='额外道具带宽上限'):
        env.bag.use('u', b)
    assert env.bag.items('u')[0]['used_at'] is None
    assert env.points.balance('u') == 2400
    env.clock[0] += 86400
    assert env.members.get('u')['bandwidth_limit_kbps'] == 120000
    env.bag.use('u', b)
    assert env.members.get('u')['bandwidth_limit_kbps'] == 220000
    assert env.members.get('u')['overrides']['bandwidth_limit_kbps'] == 120000


def test_zero_cap_has_no_bonus_ceiling_but_streams_cap_keeps_final_limit(env):
    assert env.registry.config('inventory')['bandwidth_cap_mbps'] == 0
    for _ in range(3):
        env.bag.use('u', buy(env))
    assert env.members.get('u')['bandwidth_limit_kbps'] == 420000
    env.registry.save('inventory', config={'streams_cap': 3})
    item = env.shop.create({'kind': 'streams_card', 'name': 'isolated streams', 'cost': 100,
                           'amount': 1, 'duration_days': 1, 'enabled': True})
    a = env.shop.redeem('u', item['id'])['card_id']
    b = env.shop.redeem('u', item['id'])['card_id']
    env.bag.use('u', a)
    with pytest.raises(ValueError, match='上限3路'):
        env.bag.use('u', b)
    assert env.members.get('u')['max_streams'] == 3


def test_two_connections_cannot_overconsume_positive_bonus_cap(env):
    env.registry.save('inventory', config={'bandwidth_cap_mbps': 100})
    a, b = buy(env), buy(env)
    counter = iter((a, b))
    lock = threading.Lock()
    def use(e):
        with lock:
            cid = next(counter)
        try:
            return e.bag.use('u', cid)['ok']
        except ValueError:
            return False
    assert sum(parallel(env, use, n=2)) == 1
    assert env.members.get('u')['card_contributions']['bandwidth_limit_kbps'] == 100000
    assert len([r for r in env.bag.items('u') if r['used_at'] is not None]) == 1


def test_already_applied_historical_card_keeps_exact_old_bonus_and_new_use_is_decimal(env):
    old = buy(env, days=2)
    result = {'ok': True, 'card_id': old, 'kind': 'bandwidth_card', 'effective': 222400,
              'expires_at': NOW + 2 * 86400, 'granted': '+100Mbps'}
    env.db.execute('UPDATE inventory SET used_at=?,expires_at=?,result_json=? WHERE id=?',
                   (NOW, NOW + 2 * 86400, json.dumps(result), old))
    before = env.db.one('SELECT * FROM inventory WHERE id=?', (old,))
    assert env.members.get('u')['bandwidth_limit_kbps'] == 222400
    assert env.bag.use('u', old) == result
    env.bag.use('u', buy(env, days=1))
    assert env.members.get('u')['bandwidth_limit_kbps'] == 322400
    assert env.db.one('SELECT * FROM inventory WHERE id=?', (old,)) == before
    env.clock[0] += 86400
    assert env.members.get('u')['bandwidth_limit_kbps'] == 222400
    env.clock[0] += 86400
    assert env.members.get('u')['bandwidth_limit_kbps'] == 120000


def test_actual_handler_two_100_cards_sign_rate_and_emby_policy_320mbps_staggered_expiry_and_admin_base(monkeypatch):
    clock = [1791604800]
    monkeypatch.setattr('time.time', lambda: clock[0])
    with TestClient(app) as client:
        auth = ('admin', 'change-me')
        members, bot = app.state.members, app.state.telegram
        # Only isolated group fixture: 15 MB/s = 120000 kbps. Never a live write.
        app.state.groups.update('standard', {'bandwidth_limit_kbps': 120000})
        members.upsert('u1', 'demo-user-1', {'group_id': 'standard'})
        members.bind_telegram('u1', '903')
        app.state.points.add('u1', 900, 'isolated.test')
        assert client.post('/api/plugins/inventory', auth=auth,
                           json={'config': {'bandwidth_cap_mbps': 0}}).status_code == 200
        assert client.put('/api/settings/membership', auth=auth,
                          json={'enforcement_enabled': True}).status_code == 200
        delivered = []
        async def call(method, payload=None, **kwargs):
            delivered.append((method, payload or {}))
            return {'message_id': 100 + len(delivered)}
        monkeypatch.setattr(bot, '_call', call)
        secret = 'isolated-node-signing-only'
        node = StreamNode(name='isolated', base_url='https://isolated.example.test', probe_url='http://127.0.0.1/isolated', sign_secret=secret,
                          pools=[NodePool(name='main', emby_prefix='/media', url_prefix='/s/main')])
        router = PlaybackRouter(app.state.emby, SimpleNamespace(pick=lambda **kw: SimpleNamespace(node=node)),
                                lambda: {'enabled': True}, lambda: {'url': 'https://isolated-origin.test'},
                                rate_resolver=app.state.playback._rate_resolver)
        async def assert_execution(kbps, mb_s):
            decision = await router.route('item42', 'Videos/item42/stream.mkv', {'Static': 'true'}, 'tok:u1', True, 'card-device')
            assert decision.redirected and decision.signed and decision.rate_bps == int(mb_s * 1000000)
            url = urlsplit(decision.target)
            query = parse_qs(url.query)
            assert int(query['r'][0]) == kbps * 125
            assert verify(unquote(url.path), query[node.sign_arg_digest][0], int(query[node.sign_arg_expires][0]), secret,
                          now=clock[0], rate_bps=decision.rate_bps, utag=query['u'][0])
            assert app.state.emby._users['u1']['Policy']['RemoteClientBitrateLimit'] == kbps * 1000
            assert members.get('u1')['bandwidth_limit_kbps'] == kbps
        async def flow():
            await _telegram_member_changed('u1', None)
            await assert_execution(120000, 15)
            for index, days in enumerate((1, 2)):
                item = app.state.shop.create({'kind': 'bandwidth_card', 'name': 'isolated exact live specification',
                                             'cost': 300, 'amount': 100, 'duration_days': days, 'enabled': True})
                before = members.get('u1')
                cid = app.state.shop.redeem('u1', item['id'])['card_id']
                assert members.get('u1')['bandwidth_limit_kbps'] == before['bandwidth_limit_kbps']
                await bot._card_use(903, 80, before, str(cid))
                await bot._card_use(903, 80, members.get('u1'), str(cid))  # no double grant
                await assert_execution(220000 if index == 0 else 320000, 27.5 if index == 0 else 40)
            assert app.state.groups.get('standard')['bandwidth_limit_kbps'] == 120000
            assert members.get('u1')['overrides'] == {}
            clock[0] += 86400
            await app.state.inventory.reconcile_expired(_inventory_expired_effects)
            await assert_execution(220000, 27.5)
            # Administrator explicitly raises BASE to 160Mbps while remaining
            # card continues to add 100Mbps. Expiry must preserve that intention.
            before = members.get('u1')
            members.set_overrides('u1', {'bandwidth_limit_kbps': 160000})
            await _telegram_member_changed('u1', before['bandwidth_limit_kbps'])
            await assert_execution(260000, 32.5)
            clock[0] += 86400
            await app.state.inventory.reconcile_expired(_inventory_expired_effects)
            await assert_execution(160000, 20)
            assert members.get('u1')['overrides'] == {'bandwidth_limit_kbps': 160000}
            assert app.state.groups.get('standard')['bandwidth_limit_kbps'] == 120000
            assert app.state.plugins.config('inventory')['streams_cap'] == 10
            assert app.state.points.balance('u1') == 300
        asyncio.run(flow())
        assert any(m == 'sendMessage' and '+100Mbps' in p.get('text', '') for m, p in delivered)
