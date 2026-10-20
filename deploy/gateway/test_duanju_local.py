#!/usr/bin/env python3
"""Isolated short-form local-playback gateway checks (no production services)."""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gateway


class LocalPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.registry = self.home / 'local.json'
        self.storage = self.home / 'media'
        self.emby_root = '/virtual/fixture-library'
        self.root = self.emby_root + '/hg-12345'
        self.rel = Path('hg-12345/fixture/Season 01/fixture - S01E001.mp4')
        file = self.storage / self.rel
        file.parent.mkdir(parents=True)
        file.write_bytes(b'a' * 2048)
        st = file.stat()
        self.row = {
            'itemId': 123, 'ready': True, 'decoded': True, 'root': self.root,
            'path': self.emby_root + '/' + self.rel.as_posix(),
            'size': st.st_size, 'mtimeNs': st.st_mtime_ns,
            'sha256': 'a' * 64, 'sourceId': 'source-fixture',
        }
        self._save()
        self.cmcc = self.home / 'sources.json'
        self.cmcc.write_text('{}')
        env = mock.patch.dict(os.environ, {
            'CMCC_GATEWAY_REGISTRY': str(self.cmcc),
            'CMCC_DUANJU_LOCAL_REGISTRY': str(self.registry),
            'CMCC_DUANJU_EMBY_ROOT': self.emby_root,
            'CMCC_DUANJU_STORAGE_ROOT': str(self.storage),
        })
        env.start()
        self.addCleanup(gateway.configure)
        self.addCleanup(env.stop)
        gateway.configure()

        # These tests isolate File association/byte relay; the real admission
        # HTTP exchange is covered by test_gateway_admission_integration.py.
        patch_admit = mock.patch.object(gateway.Handler, 'admit', return_value=True)
        patch_admit.start()
        self.addCleanup(patch_admit.stop)
        self.forwarded = []

        def forward(handler):
            self.forwarded.append(handler.path)
            handler.send(204)

        patch_forward = mock.patch.object(gateway.Handler, 'forward', forward)
        patch_forward.start()
        self.addCleanup(patch_forward.stop)

        self.lookups = []
        self.sources = [{'Id': 'source-fixture', 'Protocol': 'File', 'Path': self.row['path']}]

        def lookup(req, timeout):
            self.lookups.append((req.full_url, req.get_header('X-emby-token'), timeout))
            return io.BytesIO(json.dumps({'MediaSources': self.sources}).encode())

        self.client_urlopen = urllib.request.urlopen
        patch_lookup = mock.patch.object(gateway.urllib.request, 'urlopen', side_effect=lookup)
        patch_lookup.start()
        self.addCleanup(patch_lookup.stop)

        self.upstream = []
        owner = self

        class FakeConnection:
            def __init__(self, host, port, timeout):
                owner.upstream.append((host, port, timeout, self))
                self.body = io.BytesIO(b'partial-media-data')

            def request(self, method, path, headers):
                self.method, self.path, self.headers = method, path, headers

            def getresponse(self):
                return self

            status = 206

            def getheaders(self):
                return [('Content-Range', 'bytes 0-17/2048'), ('Content-Length', '18'),
                        ('Accept-Ranges', 'bytes'), ('Connection', 'keep-alive')]

            def read(self, size):
                return self.body.read(size)

            def close(self):
                pass

        patch_connection = mock.patch.object(gateway, 'http',
                                             SimpleNamespace(client=SimpleNamespace(HTTPConnection=FakeConnection)))
        patch_connection.start()
        self.addCleanup(patch_connection.stop)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), gateway.Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._stop)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()

    def _save(self):
        self.registry.write_text(json.dumps({'123': self.row}))

    def _request(self, path, headers=None, method='GET'):
        req = urllib.request.Request(self.url + path, headers=headers or {}, method=method)
        try:
            with self.client_urlopen(req, timeout=5) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def _get(self, path=None, headers=None, method='GET'):
        return self._request(path or '/emby/Videos/123/stream.mp4?Static=true&MediaSourceId=source-fixture',
                             headers, method)

    def test_unconfigured_or_partial_config_falls_back(self):
        for key in ('CMCC_DUANJU_LOCAL_REGISTRY', 'CMCC_DUANJU_STORAGE_ROOT'):
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: ''}):
                gateway.configure()
                code, _, _ = self._get()
                self.assertEqual(code, 204)
        with mock.patch.dict(os.environ, {'CMCC_DUANJU_EMBY_ROOT': '../relative'}):
            gateway.configure()
            self.assertEqual(self._get()[0], 204)
        gateway.configure()
        self.assertEqual(len(self.forwarded), 3)
        self.assertFalse(self.lookups)

    def test_invalid_registry_and_file_metadata_fall_back(self):
        changes = [
            {'ready': False}, {'decoded': False}, {'itemId': 999},
            {'root': '/unrelated/hg-12345'}, {'path': self.root + '/x/../Season 01/fixture - S01E001.mp4'},
            {'sha256': 'bad'}, {'size': 1000}, {'mtimeNs': -1}, {'sourceId': ''},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.row.update(change)
                self._save()
                self.assertEqual(self._get()[0], 204)
                # Restore the exact fixture for the next independent case.
                self.row = self._fresh_row()
        self.registry.write_text('{invalid')
        self.assertEqual(self._get()[0], 204)
        self.assertFalse(self.lookups)

    def _fresh_row(self):
        st = (self.storage / self.rel).stat()
        return {'itemId': 123, 'ready': True, 'decoded': True, 'root': self.root,
                'path': self.emby_root + '/' + self.rel.as_posix(),
                'size': st.st_size, 'mtimeNs': st.st_mtime_ns,
                'sha256': 'a' * 64, 'sourceId': 'source-fixture'}

    def test_nonmatching_routes_fall_back(self):
        for path in ('/emby/Videos/123/stream.mp4', '/emby/Videos/123/stream.mkv?Static=true',
                     '/emby/Videos/other/original.mp4'):
            self.assertEqual(self._get(path)[0], 204)
        self.assertFalse(self.lookups)

    def test_missing_token_does_not_use_admin_or_upstream(self):
        code, body, _ = self._get()
        self.assertEqual((code, body), (401, b'Authentication required'))
        self.assertFalse(self.lookups)
        self.assertFalse(self.upstream)

    def test_denied_and_unavailable_playback_info_fail_closed(self):
        with mock.patch.object(gateway.urllib.request, 'urlopen', side_effect=urllib.error.HTTPError('', 403, '', {}, None)):
            self.assertEqual(self._get(headers={'X-Emby-Token': 'caller'})[0], 403)
        with mock.patch.object(gateway.urllib.request, 'urlopen', side_effect=OSError('unavailable')):
            self.assertEqual(self._get(headers={'X-Emby-Token': 'caller'})[0], 503)
        self.assertFalse(self.upstream)

    def test_source_id_protocol_and_path_must_match_caller_visibility(self):
        for sources, path in [
            (self.sources, '/emby/Videos/123/original.mp4?MediaSourceId=other'),
            ([{'Id': 'source-fixture', 'Protocol': 'Http', 'Path': self.row['path']}], None),
            ([{'Id': 'source-fixture', 'Protocol': 'File', 'Path': '/virtual/other'}], None),
            ([], None),
        ]:
            with self.subTest(sources=sources, path=path):
                self.sources = sources
                code, _, _ = self._get(path, {'X-Emby-Token': 'caller'})
                self.assertEqual(code, 403)
        self.assertFalse(self.upstream)

    def test_get_range_and_head_relay_without_buffering_and_keep_caller_token(self):
        path = '/emby/Videos/123/stream.mp4?Static=true&MediaSourceId=source-fixture&api_key=caller-query'
        code, data, headers = self._get(path, {'X-Emby-Token': 'caller-header', 'Range': 'bytes=0-17'})
        self.assertEqual((code, data), (206, b'partial-media-data'))
        self.assertEqual(headers['Content-Range'], 'bytes 0-17/2048')
        self.assertEqual(headers['Accept-Ranges'], 'bytes')
        self.assertEqual(self.lookups[0][1], 'caller-header')
        self.assertEqual(self.upstream[0][3].headers['Range'], 'bytes=0-17')
        self.assertEqual(self.upstream[0][3].path, path)
        self.assertNotIn('Host', self.upstream[0][3].headers)
        code, data, _ = self._get('/Videos/123/original.mp4?api_key=caller-query', method='HEAD')
        self.assertEqual((code, data), (206, b''))
        self.assertEqual(self.lookups[1][1], 'caller-query')
        self.assertEqual(self.upstream[1][3].method, 'HEAD')
        self.assertFalse(self.forwarded)


if __name__ == '__main__':
    unittest.main(verbosity=2)
