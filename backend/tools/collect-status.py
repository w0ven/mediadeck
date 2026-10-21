#!/usr/bin/env python3
"""Read-only playback-host status, invoked by the existing backend sync owner.

No timer or watcher is created. Emby credentials remain in settings.json.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.adapters.live import LiveEmby
from app.core.config import settings
from app.modules.intake import IntakeCollector, unavailable

STATE = Path('/var/lib/mediadeck')
# Neutral example layout; a host sets MEDIADECK_STATUS_MOUNTS to
# {"label": ["/mount/path", "unit.service"]} (see docs/OPERATIONS-ENV.md).
DEFAULT_MOUNTS = {
    'media-main': ('/mnt/media-main', 'rclone-media-main.service'),
    'media-secondary': ('/mnt/media-secondary', 'rclone-media-secondary.service'),
}


def status_mounts(raw=None):
    raw = os.environ.get('MEDIADECK_STATUS_MOUNTS', '') if raw is None else raw
    if not raw.strip():
        return dict(DEFAULT_MOUNTS)
    data = json.loads(raw)
    if (not isinstance(data, dict)
            or any(not isinstance(v, list) or len(v) != 2
                   or not all(isinstance(x, str) and x for x in v) for v in data.values())):
        raise ValueError('MEDIADECK_STATUS_MOUNTS must map labels to [path, systemd unit]')
    return {label: tuple(value) for label, value in data.items()}


MOUNTS = status_mounts()


def publish(name, data):
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=STATE,
                                         prefix='.status-', delete=False) as f:
            tmp = Path(f.name)
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
            os.fchmod(f.fileno(), 0o644)
        os.replace(tmp, STATE / name)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def command(args, timeout=3):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout.strip()


def size_value(value):
    match = re.fullmatch(r'([0-9.]+)([KMGT]?)', value.upper())
    return int(float(match[1]) * 1024 ** ('KMGT'.find(match[2]) + 1)) if match else None


def mount_row(label, path, unit, mounts):
    row = {'label': label, 'kind': 'none', 'alive': False, 'readdir_ms': None, 'entries': 0,
           'stuck_processes': None, 'cache_bytes': None, 'cache_limit_bytes': None,
           'fs_free_bytes': None, 'fs_total_bytes': None, 'options': []}
    match = mounts.get(path)
    if not match:
        row['reason'] = 'expected mount is not mounted'
        return row
    fields, sep = match
    row.update(kind=fields[sep + 1], options=sorted(set(fields[5].split(',') + fields[sep + 3].split(',')) &
               {'ro', 'rw', 'allow_other', 'default_permissions'}))
    try:
        started = time.monotonic()
        count = command([sys.executable, '-B', '-c',
                         'import os,sys; print(len(os.listdir(sys.argv[1])))', path])
        row.update(alive=True, entries=int(count), readdir_ms=round((time.monotonic()-started)*1000, 1))
    except Exception as exc:  # noqa: BLE001 - isolate diagnostic sources; report safe type
        row['reason'] = 'directory probe failed: ' + type(exc).__name__
    try:
        pid = int(command(['systemctl', 'show', unit, '-p', 'MainPID', '--value']))
        args = Path(f'/proc/{pid}/cmdline').read_bytes().decode().split('\0')
        def flag(name):
            for i, arg in enumerate(args):
                if arg.startswith(name + '='):
                    return arg.split('=', 1)[1]
                if arg == name and i + 1 < len(args):
                    return args[i + 1]
            return ''
        cache = flag('--cache-dir')
        row['cache_limit_bytes'] = size_value(flag('--vfs-cache-max-size'))
        if cache:
            # GNU du's default is allocated blocks, not sparse logical sizes.
            try:
                row['cache_bytes'] = int(command(['du', '-s', '-B1', cache], timeout=4).split()[0])
            except Exception as exc:  # noqa: BLE001 - isolate diagnostic sources; report safe type
                row['cache_reason'] = 'cache usage unavailable: ' + type(exc).__name__
            usage = shutil.disk_usage(cache)
            row.update(fs_free_bytes=usage.free, fs_total_bytes=usage.total)
        # No cache on mergerfs: leave local backing capacity unknown, never
        # substitute the remote's nominal petabyte capacity.
    except Exception as exc:  # noqa: BLE001 - isolate diagnostic sources; report safe type
        row['cache_reason'] = 'effective process settings unavailable: ' + type(exc).__name__
    return row


def collect_mounts():
    mounts = {}
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        mounts[fields[4]] = (fields, fields.index('-'))
    rows = [mount_row(label, path, unit, mounts) for label, (path, unit) in MOUNTS.items()]
    alerts = []
    for row in rows:
        if not row['alive']:
            alerts.append({'level': 'bad', 'message': row['label'] + ': ' + row.get('reason', 'unavailable')})
        if row['cache_bytes'] is not None and row['cache_limit_bytes'] is not None and row['cache_bytes'] > row['cache_limit_bytes']:
            alerts.append({'level': 'warn', 'message': row['label'] + ': cache exceeds effective limit'})
    return {'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'source': {'role': 'playback-host', 'host': os.uname().nodename}, 'mounts': rows, 'alerts': alerts}


async def collect_intake():
    cfg = settings()
    store = json.loads(cfg.settings_file.read_text())
    collector = IntakeCollector(emby=LiveEmby(lambda: store.get('emby', {})))
    start = time.time()
    try:
        backend = json.loads((STATE / 'intake-backend-snapshot.json').read_text())
        generated = float(backend['generated_at'])
        if not -30 <= start - generated <= 180 or backend['source']['role'] != 'download-backend':
            raise ValueError('backend snapshot stale or invalid')
        if any(not isinstance(backend.get(k), dict) for k in ('refresh', 'notify', 'upload', 'cloud', 'downloader')):
            raise ValueError('invalid backend sections')
    except Exception as exc:  # noqa: BLE001 - isolate diagnostic sources; report safe type
        # Retain the old combined snapshot with its original timestamp. It will
        # age visibly instead of turning a failed backend into fresh green data.
        raise RuntimeError('backend snapshot unavailable: ' + type(exc).__name__) from None
    try:
        emby = await asyncio.wait_for(collector.collect_emby(), timeout=15)
    except Exception as exc:  # noqa: BLE001 - isolate diagnostic sources; report safe type
        emby = unavailable('Emby collection failed: ' + type(exc).__name__)
    data = {k: backend[k] for k in ('refresh', 'notify', 'upload', 'cloud', 'downloader')}
    data.update(generated_at=min(start, generated), emby=emby, thresholds=collector.thresholds,
                sources={'backend': backend['source'], 'emby': {'role': 'playback-host', 'host': os.uname().nodename},
                         'backend_generated_at': generated}, collect_ms=int((time.time()-start)*1000))
    data['health'] = collector.evaluate(data)
    # Partial collection is not a healthy green pipeline. Disabled historical
    # sources are also explicit rather than forged zeroes.
    missing = []
    def inspect(name, value):
        if isinstance(value, dict):
            if value.get('available') is False:
                missing.append(name)
                return
            for key, child in value.items():
                inspect(name + '.' + key, child)
        elif isinstance(value, list):
            for n, child in enumerate(value):
                inspect(name + '.' + str(n), child)
    for key in ('emby', 'refresh', 'notify', 'upload', 'cloud', 'downloader'):
        inspect(key, data[key])
    if missing:
        data['health']['alerts'].append({'level': 'warn', 'message': '部分来源不可用或已停用: ' + ', '.join(missing)})
        if data['health']['level'] != 'bad':
            data['health']['level'] = 'warn'
    return data


async def main():
    results = await asyncio.gather(asyncio.to_thread(collect_mounts), collect_intake(), return_exceptions=True)
    failed = False
    for name, result in zip(('mounts-snapshot.json', 'intake-snapshot.json'), results):
        if isinstance(result, Exception):
            print(name + ' collection failed: ' + type(result).__name__, file=sys.stderr)
            failed = True
        else:
            publish(name, result)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
