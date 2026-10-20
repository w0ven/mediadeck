import shutil
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from test_entry_proxy_live import proxy as proxy  # noqa: PLC0414
from test_entry_proxy_live import test_caddy_websocket_upgrade_and_frame as _check_websocket
from test_external_entries import ADMIN
from test_external_entries import client as base_client  # noqa: F401

from app.main import app
from app.modules.entry_proxy import member_proxy_config

pytestmark = pytest.mark.skipif(
    not shutil.which('caddy') or not shutil.which('openssl'),
    reason='Caddy and OpenSSL are needed for the loopback proxy test')


@pytest.fixture
def proxy_entries():
    return ('friend-a',)


@pytest.fixture
def client(request, monkeypatch):
    base = request.getfixturevalue('base_client')
    if getattr(request, 'param', '') == 'same-origin':
        assert base.delete('/api/nodes/edge-b', auth=ADMIN).status_code == 200
        assert base.put('/api/nodes/edge-a', auth=ADMIN,
                        json={'base_url': 'https://emby.example.com'}).status_code == 200
    original = base.get

    def get(url, **kwargs):
        parsed = urlsplit(url)
        if parsed.path == '/api/integration/frontend':
            query = parse_qs(parsed.query)
            engine = query.get('server', ['caddy'])[0]
            entry = query.get('entry', ['friend-a'])[0]
            text = member_proxy_config('https://emby.example.com', app.state.settings_service.nodes(), engine)
            text = text.replace('proxy.example.com', entry+'.example.com')
            return SimpleNamespace(status_code=200, json=lambda: {'config': text})
        return original(url, **kwargs)

    monkeypatch.setattr(base, 'get', get)
    return base


@pytest.mark.parametrize('client', ['distinct-origins', 'same-origin'], indirect=True)
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_official_signed_redirect_is_wrapped_and_range_bytes_work(proxy, method, monkeypatch):
    http, requests, sni, _ = proxy
    async def media_paths(item_id):
        return {'source': '/media/Movies/测试 % # + &.mkv'}
    monkeypatch.setattr(app.state.emby, 'item_media_paths', media_paths)
    response = http.request(method, '/emby/Videos/film/stream.mkv?Static=true&api_key=member-token',
                            headers={'Host': 'friend-a.example.com',
                                     'X-Mediadeck-Entry': 'forged', 'X-Mediadeck-Entry-Key': 'forged'})
    assert response.status_code == 302
    target = urlsplit(response.headers['location'])
    assert target.hostname == 'friend-a.example.com'
    assert target.path.startswith('/_n/edge-')
    media = http.request(method, target.path+'?'+target.query,
                         headers={'Host': 'friend-a.example.com', 'Range': 'bytes=2-5',
                                  'If-Range': 'test-etag', 'Authorization': 'Bearer synthetic',
                                  'X-Emby-Token': 'synthetic', 'Cookie': 'session=synthetic'})
    assert media.status_code == 206
    assert media.headers['content-range'] == 'bytes 2-5/16'
    assert media.content == (b'2345' if method == 'GET' else b'')
    assert requests[-1]['headers'].get('If-Range') == 'test-etag'
    for secret in ('X-Emby-Token', 'Authorization', 'Cookie', 'X-Mediadeck-Entry-Key'):
        assert not requests[-1]['headers'].get(secret)
    assert 'emby.example.com' in sni
    assert 'private, no-store' in media.headers['cache-control']
    suffix = http.get(target.path+'?'+target.query,
                      headers={'Host': 'friend-a.example.com', 'Range': 'bytes=-4'})
    assert suffix.status_code == 206 and suffix.content == b'cdef'


@pytest.mark.parametrize('path', ['/s/main/a%20b%3F%23%25.mkv', '/s/gd3/a//b.mkv'])
def test_encoded_signed_media_paths_and_query_are_unchanged(proxy, path):
    http, requests, _, _ = proxy
    query = 'k=a%2Bb&empty=&repeat=1&repeat=2&target=https://untrusted.example.com/'
    http.get('/_n/edge-a'+path+'?'+query, headers={'Host': 'friend-a.example.com'})
    assert requests[-1]['path'] == path+'?'+query


def test_domestic_redirect_and_lookalike_node_domain_are_not_rewritten(proxy):
    http, _, _, _ = proxy
    for kind, expected in [('domestic', 'https://cloud.example.net/file?signature=unchanged'),
                           ('lookalike', 'https://edge-a.example.com.evil.example.net/s/main/a?k=unchanged')]:
        response = http.get('/redirect/'+kind, headers={'Host': 'friend-a.example.com'})
        assert response.status_code == 302
        assert response.headers['location'] == expected


def test_unknown_routes_and_tampered_signatures_are_refused(proxy):
    http, requests, _, _ = proxy
    before = len(requests)
    for path in ('/_n/unknown/s/main/demo.mkv', '/_n/edge-a/s/..%2f..%2fapi'):
        assert http.get(path, headers={'Host': 'friend-a.example.com'}).status_code == 404
    assert len(requests) == before
    assert http.get('/_n/edge-a/s/main/demo.mkv?k=invalid&e=9999999999',
                    headers={'Host': 'friend-a.example.com'}).status_code == 403


def test_member_proxy_websocket_frames(proxy):
    _check_websocket(proxy)


def test_member_proxy_preserves_auth_and_post_body_only_to_emby(proxy):
    http, requests, _, _ = proxy
    result = http.post('/Sessions/Playing/Progress?tag=a%2Bb&tag=2', content=b'{"PositionTicks":1}',
                       headers={'Host': 'friend-a.example.com', 'X-Emby-Token': 'synthetic',
                                'X-Mediadeck-Entry': 'forged', 'X-Mediadeck-Entry-Key': 'forged'})
    assert result.status_code == 200
    row = requests[-1]
    assert row['path'].endswith('?tag=a%2Bb&tag=2')
    assert row['body'] == b'{"PositionTicks":1}'
    assert row['headers']['X-Emby-Token'] == 'synthetic'
    assert not row['headers'].get('X-Mediadeck-Entry-Key')
    assert not row['headers'].get('X-Mediadeck-Entry')
