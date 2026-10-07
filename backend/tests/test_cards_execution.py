"""Full isolated application services consume card effects on real adapter paths."""
import asyncio

from fastapi.testclient import TestClient

from app.main import _inventory_expired_effects, _telegram_member_changed, app
from app.modules.economy_rules import DEFAULT_CARDS
from app.modules.members import rate_bytes_per_sec
from app.modules.streams import StreamAdmission


def test_invite_bandwidth_and_stream_cards_reach_invite_signing_adapter_and_admission(monkeypatch):
    clock = [1791604800]
    monkeypatch.setattr('time.time', lambda: clock[0])
    with TestClient(app) as client:
        auth = ('admin', 'change-me')
        app.state.members.upsert('u1', 'demo-user-1', {'group_id': 'standard'})
        app.state.members.set_overrides('u1', {'bandwidth_limit_kbps': 20 * 1024, 'max_streams': 2})
        assert client.put('/api/settings/membership', auth=auth,
                          json={'enforcement_enabled': True}).status_code == 200
        app.state.points.add('u1', 2000, 'isolated.test')
        app.state.emby.set_sessions([
            {'Id': sid, 'UserId': 'u1', 'DeviceId': sid} for sid in 'abc'
        ])
        guard = StreamAdmission(app.state.members, app.state.emby)

        async def flow():
            original_rate = await app.state.playback._rate_resolver('tok:u1', 'a')
            assert original_rate[0] == rate_bytes_per_sec(20 * 1024)
            assert (await guard.inspect('u1', 'a')).allowed
            assert (await guard.inspect('u1', 'b')).allowed
            assert not (await guard.inspect('u1', 'c')).allowed
            for index in (0, 1, 2):
                item = app.state.shop.create(dict(DEFAULT_CARDS[index], enabled=True,
                    duration_days=0 if index == 0 else 1))
                before = app.state.members.get('u1')
                bought = app.state.shop.redeem('u1', item['id'], request_id=f'chain:{index}')
                assert app.state.members.get('u1')['effective'] == before['effective']
                used = app.state.inventory.use('u1', bought['card_id'])
                assert app.state.inventory.use('u1', bought['card_id']) == used
                await _telegram_member_changed('u1', before['bandwidth_limit_kbps'])
                if index == 0:
                    invite = app.state.registration.spend_quota_for_invite('u1')
                    assert invite and app.state.registration.invite_quota('u1') == 0
                    assert app.state.registration.list_invites('u1')
                elif index == 1:
                    policy = app.state.emby._users['u1']['Policy']
                    assert policy['RemoteClientBitrateLimit'] == (20 * 1024 + 10 * 1000) * 1000
                    renewed_rate = await app.state.playback._rate_resolver('tok:u1', 'a')
                    assert renewed_rate[0] == rate_bytes_per_sec(20 * 1024 + 10 * 1000)
                    assert renewed_rate[1] == original_rate[1]
                    # Real rate reissue terminates old links/sessions. Reconnected
                    # devices acquire new Emby session IDs before stream admission.
                    app.state.emby.set_sessions([
                        {'Id': 'reconnect-' + sid, 'UserId': 'u1', 'DeviceId': sid} for sid in 'abc'
                    ])
                    assert (await guard.inspect('u1', 'a')).allowed
                    assert (await guard.inspect('u1', 'b')).allowed
                    assert not (await guard.inspect('u1', 'c')).allowed
                else:
                    assert app.state.emby._users['u1']['Policy']['SimultaneousStreamLimit'] == 3
                    assert (await guard.inspect('u1', 'c')).allowed
            assert app.state.points.balance('u1') == 600
            clock[0] += 86400
            await app.state.inventory.reconcile_expired(_inventory_expired_effects)
            assert app.state.emby._users['u1']['Policy']['RemoteClientBitrateLimit'] == 20 * 1024 * 1000
            assert app.state.emby._users['u1']['Policy']['SimultaneousStreamLimit'] == 2
            expired_rate = await app.state.playback._rate_resolver('tok:u1', 'a')
            assert expired_rate[0] == original_rate[0]
            assert app.state.members.get('u1')['overrides'] == {'bandwidth_limit_kbps': 20 * 1024, 'max_streams': 2}

        asyncio.run(flow())
