#!/usr/bin/env python3
"""Read-only backend observability, called only by mediadeck-pipeline.service.

Uses the deployed MediaDeck pure filesystem collector, not a second queue owner.
No credentials or URLs are included in the snapshot.
"""
import importlib.util
import json
import os
import signal
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mediadeck_cloud_dl import collect_cloud

ROOT = Path(__file__).resolve().parent
source_path = ROOT / 'mediadeck-intake-source.py'
if not source_path.exists():  # Repository checkout; production has the deployed sibling.
    source_path = ROOT.parent / 'app/modules/intake.py'
spec = importlib.util.spec_from_file_location('mediadeck_intake_source', source_path)
source = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = source
spec.loader.exec_module(source)
OUT = Path('/var/lib/mediadeck/intake-backend-snapshot.json')
# Neutral example layout. A host overrides any of these keys with the JSON
# object in MEDIADECK_INTAKE_PATHS (see docs/OPERATIONS-ENV.md).
DEFAULT_PATHS = {
    'refresh_queue_dir': '/var/lib/mediadeck-intake/refresh/queue',
    'refresh_sent_dir': '/var/lib/mediadeck-intake/refresh/sent',
    'refresh_suppress_file': '/run/mediadeck-intake/suppress-refresh',
    'notify_pending_dir': '/var/lib/mediadeck-intake/notify/pending',
    'notify_log': '/var/log/mediadeck-intake/notify.log',
    'upload_lane_root': '/srv/intake/upload-lanes',
    'staging_dir': '/srv/intake/staging',
    'local_fallback_dir': '/var/lib/mediadeck-intake/fallback/Media',
    'quarantine_dir': '/srv/intake/quarantine',
    'upload_state_dir': '/var/lib/mediadeck-intake/upload-state',
}


def intake_paths(raw=None):
    raw = os.environ.get('MEDIADECK_INTAKE_PATHS', '') if raw is None else raw
    paths = dict(DEFAULT_PATHS)
    if raw.strip():
        override = json.loads(raw)
        if (not isinstance(override, dict) or set(override) - set(DEFAULT_PATHS)
                or any(not isinstance(v, str) for v in override.values())):
            raise ValueError('MEDIADECK_INTAKE_PATHS must map known keys to path strings')
        paths.update(override)
    return paths


PATHS = intake_paths()


def downloader(name, port):
    try:
        # These existing loopback services allow local read-only API requests.
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/v2/torrents/info?limit=2000', timeout=5) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError('oversized downloader response')
        rows = json.loads(raw)
        if not isinstance(rows, list) or len(rows) >= 2000:
            raise ValueError('invalid or capped downloader response')
        out = dict(name=name, available=True, total=len(rows), downloading=0,
                   downloading_bytes=0, completed=0, completed_bytes=0)
        for row in rows:
            group = 'completed' if float(row.get('progress') or 0) >= 1 else 'downloading'
            out[group] += 1
            out[group + '_bytes'] += max(0, int(row.get('total_size') or 0))
        return out
    except Exception as exc:
        return dict(name=name, available=False, reason=type(exc).__name__)


def local_snapshot():
    collector = source.IntakeCollector(paths=source.IntakePaths.from_mapping(PATHS))
    data = {'generated_at': time.time(),
            'source': {'role': 'download-backend', 'host': os.uname().nodename}}
    for key in ('refresh', 'notify', 'upload'):
        try:
            data[key] = getattr(collector, 'collect_' + key)()
        except Exception as exc:
            data[key] = source.unavailable('collection failed: ' + type(exc).__name__)
    data['downloader'] = {'clients': [downloader('qb-host', 8091), downloader('qb-remote', 18081)]}
    # Queue labels may contain media names; publish metrics, not title/path rows.
    data['refresh'].pop('top', None)
    return data


def main():
    # Cloud's 7s SSH budget overlaps the existing local/qB work rather than
    # being added to it. The original 18s collector / 20s parent cap remains.
    with ThreadPoolExecutor(max_workers=1) as pool:
        cloud = pool.submit(collect_cloud)
        data = local_snapshot()
        data['cloud'] = cloud.result()
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=OUT.parent,
                                         prefix='.intake-', delete=False) as f:
            tmp = Path(f.name)
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
            os.fchmod(f.fileno(), 0o644)
        os.replace(tmp, OUT)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


if __name__ == '__main__':
    def timeout(*_):
        raise TimeoutError('collection timeout')
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(18)
    try:
        main()
    except Exception as exc:
        print('intake collection failed: ' + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
