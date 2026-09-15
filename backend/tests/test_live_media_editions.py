"""Preserve already-deployed grouped-edition routing in the formal release."""
import asyncio

import httpx
import pytest

from app.adapters.live import LiveEmby


@pytest.mark.parametrize('edition_status', [200, 503])
def test_grouped_editions_preserve_primary_and_ignore_remote_sources(monkeypatch, edition_status):
    def upstream(request):
        if request.url.path.endswith('/PlaybackInfo'):
            return httpx.Response(edition_status, json={'MediaSources': [
                {'Id': 'edition', 'Path': '/media/example/alternate.mkv'},
                {'Id': 'cloud', 'Path': 'https://media.example.invalid/stream'},
            ]})
        return httpx.Response(200, json={'Items': [{'Id': 'item', 'MediaSources': [
            {'Id': 'primary', 'Path': '/media/example/primary.mkv'},
        ]}]})
    adapter = LiveEmby(lambda: {'enabled': True, 'url': 'https://emby.example.invalid',
                               'api_key': 'synthetic-admin'})
    monkeypatch.setattr(adapter, '_client', lambda *_: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)))
    paths = asyncio.run(adapter.item_media_paths('item'))
    assert list(paths)[0] == 'primary'
    assert paths['primary'] == '/media/example/primary.mkv'
    assert 'cloud' not in paths
    assert ('edition' in paths) == (edition_status == 200)
