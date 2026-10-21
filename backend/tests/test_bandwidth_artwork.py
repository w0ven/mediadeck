"""Node probe concurrency/timestamps and series poster regressions; no production traffic."""
import asyncio
import time

import httpx
from fastapi.testclient import TestClient
from test_dashboard_media import _live_emby

from app.adapters.live import LiveProbe
from app.core.config import StreamNode
from app.main import app
from app.modules.scheduler import Scheduler


def node(name):
    return StreamNode(name=name, base_url='https://example.test', probe_url=name)


def measured(now, bps=1048576):
    return {'ok':True,'active_streams':1,'egress_mbps':8.388608,
            'egress_sampled_at':now,'egress_window_seconds':8,
            'user_speeds':{'viewer':bps},'user_speeds_source':'socket',
            'user_speeds_sampled_at':now,'user_speeds_window_seconds':8}


def test_sessions_carry_no_rate_and_never_read_the_bitrate_estimate(monkeypatch):
    """Byte-rate display is retired (docs/TRAFFIC_RETIREMENT.md): no Speed* field,
    and the session view must not fall back to a media-bitrate estimate."""
    with TestClient(app) as client:
        def forbidden(*_args, **_kwargs):
            raise AssertionError('real-time view must not read rate telemetry or estimates')
        monkeypatch.setattr(app.state.scheduler, 'user_speed_view', forbidden)
        monkeypatch.setattr(app.state.usage, 'live_speeds', forbidden)
        out = client.get('/api/emby/sessions',auth=('admin','change-me')).json()
        assert out and not [k for s in out for k in s if k.startswith('Speed')]


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
