"""Live rate freshness/scope and series poster regressions; no production traffic."""
import asyncio
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from test_dashboard_media import _live_emby
from test_loadprobe import _load

from app.adapters.live import LiveProbe
from app.core.config import StreamNode
from app.main import app, user_tag
from app.modules.scheduler import Scheduler


class Probe:
    def __init__(self, data):
        self.data = data

    async def load(self, url):
        return self.data[url]


def node(name):
    return StreamNode(name=name, base_url='https://example.test', probe_url=name)


def measured(now, bps=1048576):
    return {'ok':True,'active_streams':1,'egress_mbps':8.388608,
            'egress_sampled_at':now,'egress_window_seconds':8,
            'user_speeds':{'viewer':bps},'user_speeds_source':'socket',
            'user_speeds_sampled_at':now,'user_speeds_window_seconds':8}


def test_missing_sample_does_not_use_media_bitrate(monkeypatch):
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.scheduler, 'user_speed_view', dict)
        def forbidden_estimate():
            raise AssertionError('real-time view must not read the bitrate estimate')
        monkeypatch.setattr(app.state.usage, 'live_speeds', forbidden_estimate)
        out = client.get('/api/emby/sessions',auth=('admin','change-me')).json()
        assert out and out[0]['SpeedBps'] is None and out[0]['SpeedSource']=='unknown'


def test_user_rate_zero_and_multisession_scope_are_preserved(monkeypatch):
    with TestClient(app) as client:
        async def sessions():
            return [{'Id':'a','UserId':'u1'},{'Id':'b','UserId':'u1','Paused':True}]
        monkeypatch.setattr(app.state.emby,'active_sessions',sessions)
        app.state.cache.drop_prefix('emby:sessions')
        view={user_tag('u1'):{'bps':0,'collected_at':time.time(),'source':'node','time_basis':'collector','window_seconds':8,'nodes':['edge']}}
        monkeypatch.setattr(app.state.scheduler,'user_speed_view',lambda:view)
        out=client.get('/api/emby/sessions',auth=('admin','change-me')).json()
        assert len(out)==2
        assert all(s['SpeedBps']==0 and s['SpeedScope']=='user' and s['SpeedAccountSessions']==2 for s in out)


def test_stale_and_failed_nodes_cannot_contribute_old_rates(monkeypatch):
    now=1_800_000_000.0
    monkeypatch.setattr('app.modules.scheduler.time.time',lambda:now)
    probe=Probe({'a':measured(now),'b':measured(now-2)})
    scheduler=Scheduler([node('a'),node('b')],probe)
    asyncio.run(scheduler.refresh())
    assert scheduler.user_speed_view()['viewer']['bps']==2097152
    assert scheduler.user_speed_view()['viewer']['collected_at']==now-2
    probe.data['b']={'ok':False}
    asyncio.run(scheduler.refresh())
    view=scheduler.user_speed_view()['viewer']
    assert view['bps'] is None and view['coverage']=='partial'
    assert scheduler.snapshot()[1]['egress_mbps'] is None
    assert scheduler.snapshot()[1]['last_success_ts']==now
    now+=20
    assert scheduler.snapshot()[0]['egress_mbps'] is None
    assert scheduler.user_speed_view()['viewer']['bps'] is None


def test_source_time_not_response_time_and_legacy_basis_is_explicit():
    now=time.time()
    probe=Probe({'a':measured(now-60)})
    scheduler=Scheduler([node('a')],probe)
    asyncio.run(scheduler.refresh())
    assert scheduler.snapshot()[0]['egress_mbps'] is None
    assert scheduler.user_speed_view()['viewer']['bps'] is None
    probe.data['a']={'ok':True,'egress_mbps':8,'user_speeds':{'viewer':0}}
    asyncio.run(scheduler.refresh())
    assert scheduler.snapshot()[0]['egress_time_basis']=='probe_received'
    assert scheduler.user_speed_view()['viewer']['time_basis']=='probe_received'
    assert scheduler.user_speed_view()['viewer']['bps']==0


def test_node_probes_run_concurrently():
    async def run():
        entered=[]
        ready=asyncio.Event()
        class ConcurrentProbe:
            async def load(self,url):
                entered.append(url)
                if len(entered)==2:
                    ready.set()
                await asyncio.wait_for(ready.wait(),timeout=.5)
                return measured(time.time())
        scheduler=Scheduler([node('a'),node('b')],ConcurrentProbe())
        await scheduler.refresh()
        assert all(n['ok'] for n in scheduler.snapshot())
    asyncio.run(run())


@pytest.mark.parametrize('value', [None,-1,'invalid',float('nan')])
def test_invalid_egress_is_unknown_not_zero(value):
    scheduler=Scheduler([node('a')],Probe({'a':{'ok':True,'egress_mbps':value}}))
    asyncio.run(scheduler.refresh())
    assert scheduler.snapshot()[0]['egress_mbps'] is None


def test_probe_adapter_keeps_source_timestamp_and_respects_ok(monkeypatch):
    original=httpx.AsyncClient
    payload={**measured(time.time()-100),'ok':False}
    def client(**kwargs):
        return original(**kwargs,transport=httpx.MockTransport(lambda _:httpx.Response(200,json=payload)))
    monkeypatch.setattr('app.adapters.live.httpx.AsyncClient',client)
    got=asyncio.run(LiveProbe().load('http://example.test/load'))
    assert not got['ok'] and got['user_speeds_source']=='socket'
    assert got['egress_sampled_at']==payload['egress_sampled_at']


def test_episode_uses_series_poster_without_changing_playback_item():
    item={'Id':'episode','Name':'Episode','Type':'Episode','SeriesId':'series',
          'SeriesPrimaryImageTag':'series-image','ImageTags':{'Primary':'wide-still'},
          'PrimaryImageAspectRatio':16/9}
    emby=_live_emby(lambda _:httpx.Response(200,json=[{'Id':'session','NowPlayingItem':item}]))
    result=asyncio.run(emby.active_sessions())[0]
    assert result['ItemId']=='episode' and result['PosterItemId']=='series'
    assert result['PosterImageTag']=='series-image' and result['PosterKind']=='series'
    item.pop('SeriesPrimaryImageTag')
    fallback=asyncio.run(emby.active_sessions())[0]
    assert fallback['PosterItemId']=='episode' and fallback['PosterImageTag']=='wide-still'
    assert fallback['PosterAspectRatio']==16/9


def test_failed_socket_read_is_not_successful_empty_sample(monkeypatch):
    module=_load()
    def fail(*args,**kwargs):
        raise OSError('unavailable')
    monkeypatch.setattr(module.subprocess,'run',fail)
    assert module.conn_bytes({443}) is None
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout=''))
    assert module.conn_bytes({443})=={}


def test_collector_snapshot_marks_stale_data_invalid():
    module=_load()
    collector=module.Sampler.__new__(module.Sampler)
    collector.speedlog=SimpleNamespace(speeds_split=lambda:({'viewer':0},{}),sample_ok=False,sampled_at=time.time()-30,RATE_WINDOW=8)
    collector._lock=threading.Lock()
    collector.active=1
    collector.egress_mbps=10
    collector.egress_ok=True
    collector.egress_sampled_at=time.time()-30
    collector.egress_window_seconds=8
    result=collector.snapshot()
    assert not result['egress_ok'] and not result['user_speeds_ok']
    assert result['egress_window_seconds']==8
