"""Directed tests for 8339 sources.json reuse. No cloud-link cache."""
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2] / 'deploy' / 'gateway'
sys.path.insert(0, str(ROOT))
import gateway

PLACE_URL = 'https://example.invalid/d/x'


def _write(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj))


def _atomic_replace(path: Path, obj: dict) -> None:
    nxt = path.with_name(path.name + '.next')
    nxt.write_text(json.dumps(obj))
    nxt.replace(path)


def _clear_env():
    os.environ.pop('CMCC_GATEWAY_REGISTRY', None)
    os.environ.pop('CMCC_OPENLIST_PUBLIC_HOST', None)
    gateway.configure()


class RegistryCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.reg = Path(self.tmp.name) / 'sources.json'
        gateway.REG = self.reg
        gateway.reset_registry_cache()
        self.loads = 0
        self._real_loads = json.loads

        def counting_loads(raw, *a, **k):
            self.loads += 1
            return self._real_loads(raw, *a, **k)

        json.loads = counting_loads
        gateway.json.loads = counting_loads
        self.addCleanup(self._restore_loads)

    def _restore_loads(self):
        json.loads = self._real_loads
        gateway.json.loads = self._real_loads

    def test_unchanged_parses_once(self):
        _write(self.reg, {'a': {'url': PLACE_URL, 'item_ids': ['1']}})
        first = gateway.load_registry()
        second = gateway.load_registry()
        self.assertEqual(self.loads, 1)
        self.assertIs(first, second)

    def test_atomic_replace_invalidates(self):
        _write(self.reg, {'old': {'url': PLACE_URL + '/old', 'item_ids': ['1']}})
        gateway.load_registry()
        _atomic_replace(self.reg, {'new': {'url': PLACE_URL + '/new', 'item_ids': ['2']}})
        data = gateway.load_registry()
        self.assertEqual(self.loads, 2)
        self.assertIn('new', data)
        self.assertNotIn('old', data)

    def test_in_place_modify_invalidates(self):
        _write(self.reg, {'k': {'url': PLACE_URL + '/a', 'item_ids': ['1']}})
        gateway.load_registry()
        time.sleep(0.05)
        _write(self.reg, {'k': {'url': PLACE_URL + '/b', 'item_ids': ['1']}})
        os.utime(self.reg, None)
        data = gateway.load_registry()
        self.assertEqual(self.loads, 2)
        self.assertEqual(data['k']['url'], PLACE_URL + '/b')

    def test_delete_does_not_reuse(self):
        _write(self.reg, {'keep': {'url': PLACE_URL, 'item_ids': ['1']}})
        gateway.load_registry()
        self.reg.unlink()
        with self.assertRaises(FileNotFoundError):
            gateway.load_registry()
        self.assertIsNone(gateway._REG_CACHE)

    def test_corrupt_does_not_reuse(self):
        _write(self.reg, {'keep': {'url': PLACE_URL, 'item_ids': ['1']}})
        gateway.load_registry()
        self.reg.write_text('{not-json')
        with self.assertRaises(json.JSONDecodeError):
            gateway.load_registry()
        self.assertIsNone(gateway._REG_CACHE)
        _write(self.reg, {'fresh': {'url': PLACE_URL + '/y', 'item_ids': ['9']}})
        data = gateway.load_registry()
        self.assertIn('fresh', data)
        self.assertNotIn('keep', data)

    def test_non_object_json_does_not_reuse(self):
        _write(self.reg, {'keep': {'url': PLACE_URL, 'item_ids': ['1']}})
        gateway.load_registry()
        self.reg.write_text('[]')
        with self.assertRaises(ValueError):
            gateway.load_registry()
        self.assertIsNone(gateway._REG_CACHE)

    def test_concurrent_loads_once(self):
        _write(self.reg, {'a': {'url': PLACE_URL, 'item_ids': ['1']}})
        started = threading.Event()
        release = threading.Event()
        real = self._real_loads

        def slow_loads(raw, *a, **k):
            self.loads += 1
            started.set()
            release.wait(2)
            return real(raw, *a, **k)

        json.loads = slow_loads
        gateway.json.loads = slow_loads
        errors = []
        results = []

        def worker():
            try:
                results.append(gateway.load_registry())
            except Exception as exc:  # noqa: BLE001 - preserve failure diagnostics without breaking caller
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        self.assertTrue(started.wait(2))
        time.sleep(0.05)
        self.assertEqual(self.loads, 1)
        release.set()
        for t in threads:
            t.join(2)
        self.assertFalse(errors)
        self.assertEqual(self.loads, 1)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(row is results[0] for row in results))


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.reg = Path(self.tmp.name) / 'sources.json'
        url = 'https://example.invalid/d/file.mkv?sign=abc'
        self.url = url
        _write(self.reg, {
            'sid-cmcc': {'url': url, 'item_ids': ['42']},
            'named': {'url': url, 'item_ids': ['42']},
        })
        gateway.REG = self.reg
        gateway.reset_registry_cache()
        self.forwarded = []
        self.resolved = []
        self._orig_forward = gateway.Handler.forward
        self._orig_resolve = gateway.Handler.resolve

        def fake_forward(handler):
            self.forwarded.append(handler.path)
            handler.send(204, b'forwarded')

        def fake_resolve(handler, source):
            self.resolved.append(source['url'])
            handler.send(302, headers={
                'Location': 'https://example.cmecloud.cn/x',
                'Cache-Control': 'private, no-store',
            })

        gateway.Handler.forward = fake_forward
        gateway.Handler.resolve = fake_resolve
        self.addCleanup(self._restore)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), gateway.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.httpd.server_address[1]}'

    def _restore(self):
        gateway.Handler.forward = self._orig_forward
        gateway.Handler.resolve = self._orig_resolve
        self.httpd.shutdown()
        self.httpd.server_close()

    def _get(self, path, headers=None):
        class NoFollow(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoFollow)
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with opener.open(req, timeout=5) as r:
                return r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            return e.code, e.read(), {k.lower(): v for k, v in e.headers.items()}

    def test_health_skips_registry(self):
        self.reg.unlink()
        code, body, _ = self._get('/health')
        self.assertEqual(code, 200)
        self.assertEqual(body, b'ok')

    def test_unknown_edition_forwards_to_deck(self):
        code, _, _ = self._get('/emby/Videos/1/original.mkv?MediaSourceId=missing')
        self.assertEqual(code, 204)
        self.assertEqual(len(self.forwarded), 1)
        self.assertEqual(self.resolved, [])

    def test_cmcc_source_requires_capability(self):
        code, body, _ = self._get('/cmcc-source/named?cap=deadbeef')
        self.assertEqual(code, 403)
        self.assertEqual(body, b'Invalid source capability')
        self.assertEqual(self.resolved, [])

    def test_cmcc_source_valid_cap_resolves(self):
        cap = hashlib.sha256(self.url.encode()).hexdigest()
        code, _, headers = self._get('/cmcc-source/named?cap=' + cap)
        self.assertEqual(code, 302)
        self.assertEqual(self.resolved, [self.url])
        self.assertEqual(headers.get('location'), 'https://example.cmecloud.cn/x')
        self.assertIn('private, no-store', headers.get('cache-control', ''))

    def test_gd_stream_without_static_forwards(self):
        code, _, _ = self._get('/emby/Videos/42/stream.mkv?MediaSourceId=sid-cmcc')
        self.assertEqual(code, 204)
        self.assertEqual(self.forwarded, ['/emby/Videos/42/stream.mkv?MediaSourceId=sid-cmcc'])

    def test_deleted_registry_does_not_keep_cmcc_auth(self):
        cap = hashlib.sha256(self.url.encode()).hexdigest()
        code, _, _ = self._get('/cmcc-source/named?cap=' + cap)
        self.assertEqual(code, 302)
        self.reg.unlink()
        code, body, _ = self._get('/cmcc-source/named?cap=' + cap)
        self.assertEqual(code, 502)
        self.assertEqual(body, b'Playback dispatch unavailable')
        self.assertEqual(len(self.resolved), 1)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(_clear_env)
        _clear_env()

    def test_default_registry_is_sidecar_file(self):
        self.assertEqual(gateway.REG, Path(gateway.__file__).with_name('sources.json'))

    def test_env_registry_override(self):
        path = Path(self.tmp.name) / 'alt-registry.json'
        os.environ['CMCC_GATEWAY_REGISTRY'] = str(path)
        gateway.configure()
        self.assertEqual(gateway.REG, path)

    def test_unset_openlist_host_is_example_invalid(self):
        self.assertEqual(gateway.OPENLIST_PUBLIC_HOST, 'example.invalid')

    def test_env_openlist_host_loads(self):
        os.environ['CMCC_OPENLIST_PUBLIC_HOST'] = 'openlist.example'
        gateway.configure()
        self.assertEqual(gateway.OPENLIST_PUBLIC_HOST, 'openlist.example')

    def test_invalid_openlist_host_falls_back_and_refuses(self):
        os.environ['CMCC_OPENLIST_PUBLIC_HOST'] = 'https://not-a-host/path'
        gateway.configure()
        self.assertEqual(gateway.OPENLIST_PUBLIC_HOST, 'example.invalid')

    def test_blank_openlist_host_falls_back_and_refuses(self):
        os.environ['CMCC_OPENLIST_PUBLIC_HOST'] = '   '
        gateway.configure()
        self.assertEqual(gateway.OPENLIST_PUBLIC_HOST, 'example.invalid')


class MissingConfigRejectTests(unittest.TestCase):
    """Unset public host must refuse, even if the registry names another host."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(_clear_env)
        _clear_env()
        self.reg = Path(self.tmp.name) / 'sources.json'
        self.foreign = 'https://openlist.example/d/file.mkv?sign=abc'
        _write(self.reg, {'named': {'url': self.foreign, 'item_ids': ['42']}})
        os.environ['CMCC_GATEWAY_REGISTRY'] = str(self.reg)
        os.environ.pop('CMCC_OPENLIST_PUBLIC_HOST', None)
        gateway.configure()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), gateway.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        self.addCleanup(self._stop)

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _get(self, path):
        class NoFollow(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoFollow)
        req = urllib.request.Request(self.base + path)
        try:
            with opener.open(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_unset_host_rejects_registry_url_without_inferring(self):
        cap = hashlib.sha256(self.foreign.encode()).hexdigest()
        code, body = self._get('/cmcc-source/named?cap=' + cap)
        self.assertEqual(code, 502)
        self.assertEqual(body, b'Invalid source configuration')

    def test_configured_host_does_not_follow_foreign_registry_host(self):
        os.environ['CMCC_OPENLIST_PUBLIC_HOST'] = 'allowed.example'
        gateway.configure()
        cap = hashlib.sha256(self.foreign.encode()).hexdigest()
        code, body = self._get('/cmcc-source/named?cap=' + cap)
        self.assertEqual(code, 502)
        self.assertEqual(body, b'Invalid source configuration')

    def test_configured_host_allows_matching_url(self):
        os.environ['CMCC_OPENLIST_PUBLIC_HOST'] = 'openlist.example'
        gateway.configure()
        class FakeResp:
            code = 302
            headers = {'Location': 'https://cdn.example.cmecloud.cn/obj'}  # noqa: RUF012 - immutable test response fixture
            def close(self):
                pass
        cap = hashlib.sha256(self.foreign.encode()).hexdigest()
        with mock.patch.object(gateway.OPENER, 'open', return_value=FakeResp()):
            code, _ = self._get('/cmcc-source/named?cap=' + cap)
        self.assertEqual(code, 302)


class TimingCompareTests(unittest.TestCase):
    def test_warm_hits_skip_json_parse_and_report_timing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'sources.json'
        payload = {f'id{i}': {'url': f'https://example.invalid/d/{i}', 'item_ids': [str(i)]}
                   for i in range(8000)}
        path.write_text(json.dumps(payload))
        raw = path.read_text()
        t0 = time.perf_counter()
        for _ in range(8):
            json.loads(raw)
        old_ms = (time.perf_counter() - t0) * 1000
        gateway.REG = path
        gateway.reset_registry_cache()
        expected = gateway.load_registry()  # first/cold load is deliberately not a cache hit
        t0 = time.perf_counter()
        with mock.patch.object(gateway.json, 'loads', side_effect=AssertionError('warm hit parsed JSON')):
            for _ in range(8):
                self.assertIs(gateway.load_registry(), expected)
        new_ms = (time.perf_counter() - t0) * 1000
        # A loaded suite/GC can skew a wall-clock ratio. The invariant above
        # verifies the removed work; production protocol timing is measured separately.
        print(f'TIMING_8HITS old_reparse_ms={old_ms:.1f} new_cache_ms={new_ms:.1f}', flush=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
