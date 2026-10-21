"""Bounded read-only cloud-download status via the backend's existing SSH alias.

Deployed beside mediadeck-intake-collector.py. No cookies, writes or new owner.
The SSH alias and remote state locations come from MEDIADECK_CLOUD_DL_PATHS
(see docs/OPERATIONS-ENV.md); the defaults are neutral examples.
"""
from __future__ import annotations

import json
import math
import os
import re
import shlex
import subprocess
import time

SSH_TIMEOUT = 7
SOURCE_MAX_AGE = 180
LEGACY_OWNER = {'status': 'disabled', 'applicable': False,
                'reason': '旧后端MIG115-DISABLED；现役115由云下载单owner负责'}
DEFAULT_PATHS = {
    'host': 'cloud-dl',
    'state_root': '/var/lib/cloud-dl/state/',
    'owner_mode': 'primary',
    'progress_file': 'primary-progress.json',
    'jobs_dir': '/var/lib/cloud-dl/jobs',
    'gd_pending_dir': '/var/lib/cloud-dl/upload-events/pending',
    'gd_done_dir': '/var/lib/cloud-dl/upload-events/done',
}
_HOST_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,62}')


def cloud_paths(raw=None):
    """Defaults overridden key by key from the MEDIADECK_CLOUD_DL_PATHS object."""
    raw = os.environ.get('MEDIADECK_CLOUD_DL_PATHS', '') if raw is None else raw
    paths = dict(DEFAULT_PATHS)
    if raw.strip():
        override = json.loads(raw)
        if not isinstance(override, dict) or set(override) - set(DEFAULT_PATHS):
            raise ValueError('unknown cloud status keys')
        paths.update(override)
    if any(not isinstance(v, str) or not v for v in paths.values()):
        raise ValueError('cloud status values must be non-empty strings')
    # The alias is an ssh argv element: never let it be read as an option.
    if not _HOST_RE.fullmatch(paths['host']):
        raise ValueError('invalid cloud status SSH alias')
    return paths


try:
    PATHS, CONFIG_ERROR = cloud_paths(), None
except ValueError as exc:  # isolate a bad setting to the cloud section only
    PATHS, CONFIG_ERROR = dict(DEFAULT_PATHS), type(exc).__name__
SOURCE = {'role': 'cloud-download-owner', 'host': PATHS['host']}

# Executed on the cloud-download host; only allow-listed state is read. Never
# import the business scripts (their imports/CLI may write or invoke dispatch).
# Locations arrive as one JSON literal (see remote_script), never as shell text.
REMOTE_READ = r'''
import json, os, signal, time
from pathlib import Path

CONFIG = json.loads(__MEDIADECK_CONFIG__)

def alarm(*_):
    raise TimeoutError('read budget exhausted')
signal.signal(signal.SIGALRM, alarm)
signal.alarm(4)

def read(name):
    with Path(name).open() as f:
        text = f.read(2 * 1024 * 1024 + 1)
    if len(text) > 2 * 1024 * 1024:
        raise ValueError('state too large')
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError('expected object')
    return data

def count(path):
    n = 0
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.name.endswith('.json') and entry.is_file(follow_symlinks=False):
                n += 1
                if n > 100000:
                    raise ValueError('count capped')
    return n

root = CONFIG['state_root']
owner = read(root + 'ownership.json')
if owner.get('mode') != CONFIG['owner_mode']:
    raise ValueError('not active cloud-download owner')
p = read(root + CONFIG['progress_file'])
b = read(root + 'backlog.json')
if p.get('ssh_ok') is not True or not isinstance(p.get('files'), list) or not isinstance(b.get('rows'), list):
    raise ValueError('producer unavailable')
files = p['files']
result = {
    'source': {'host': CONFIG['host'], 'hostname': os.uname().nodename,
               'role': 'cloud-download-owner'},
    'observed_at': time.time(),
    'progress_generated_at': p['at'], 'backlog_generated_at': b['at'],
    'jobs': count(CONFIG['jobs_dir']),
    'progress_jobs': p['jobs_pending'], 'active_files': len(files),
    'downloaded_bytes': sum(int(f['downloaded']) for f in files),
    'total_bytes': sum(int(f['total']) for f in files),
    'live_mibs': p['live_mibs'], 'backlog': len(b['rows']),
    'gd_pending': count(CONFIG['gd_pending_dir']),
    'gd_done': count(CONFIG['gd_done_dir']),
}
# Timestamp is a new source-host observation, not a file mtime. Producer times
# remain separate and are validated by the receiver, never restamped.
result['observed_at'] = time.time()
print(json.dumps(result))
'''


def remote_script(paths=None):
    """REMOTE_READ with the locations embedded as a Python string literal."""
    literal = repr(json.dumps(PATHS if paths is None else paths))
    return REMOTE_READ.replace('__MEDIADECK_CONFIG__', literal, 1)


def unavailable(reason):
    return {'available': False, 'reason': reason, 'source': dict(SOURCE),
            'legacy_owner': dict(LEGACY_OWNER)}


def validate(raw, now=None):
    now = time.time() if now is None else now
    if not isinstance(raw, dict) or not isinstance(raw.get('source'), dict):
        raise TypeError('invalid source')
    if (raw['source'].get('host') != SOURCE['host']
            or raw['source'].get('role') != 'cloud-download-owner'):
        raise ValueError('wrong source')
    for field in ('observed_at', 'progress_generated_at', 'backlog_generated_at'):
        stamp = raw.get(field)
        if (type(stamp) not in (float, int) or not math.isfinite(stamp)
                or not -30 <= now - stamp <= SOURCE_MAX_AGE):
            raise ValueError('stale or invalid source generation')
    for field in ('jobs', 'progress_jobs', 'active_files', 'downloaded_bytes',
                  'total_bytes', 'backlog', 'gd_pending', 'gd_done'):
        if type(raw.get(field)) is not int or raw[field] < 0:
            raise ValueError('invalid count')
    speed = raw.get('live_mibs')
    if type(speed) not in (int, float) or not math.isfinite(speed) or speed < 0:
        raise ValueError('invalid rate')
    generated = min(raw[k] for k in ('observed_at', 'progress_generated_at', 'backlog_generated_at'))
    return {
        'available': True, 'source': raw['source'], 'generated_at': generated,
        'observed_at': raw['observed_at'],
        'snapshot_age_seconds': round(max(0, now - generated), 1),
        'progress_generated_at': raw['progress_generated_at'],
        'backlog_generated_at': raw['backlog_generated_at'],
        'jobs': raw['jobs'], 'progress_jobs': raw['progress_jobs'],
        'active_files': raw['active_files'], 'downloaded_bytes': raw['downloaded_bytes'],
        'total_bytes': raw['total_bytes'], 'live_mibs': speed,
        'backlog': {'available': True, 'rows': raw['backlog'], 'generated_ts': raw['backlog_generated_at']},
        'queue': {'available': True, 'depth': raw['jobs']},
        'active': {'available': True, 'manifest_items': raw['active_files']},
        'gd_upload': {'available': True, 'pending': raw['gd_pending'], 'done': raw['gd_done'],
                      'observed_at': raw['observed_at']},
        'legacy_owner': dict(LEGACY_OWNER),
    }


def collect_cloud():
    if CONFIG_ERROR:
        return unavailable('云下载状态配置无效: ' + CONFIG_ERROR)
    try:
        # BatchMode guarantees no prompts or new credentials. Both remote read
        # and local SSH are bounded; failure affects only cloud, never pipeline.
        command = 'timeout --kill-after=1s 5s python3 -B -c ' + shlex.quote(remote_script())
        proc = subprocess.run(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
             '-o', 'ConnectTimeout=2', '-o', 'ConnectionAttempts=1',
             '-o', 'ServerAliveInterval=2', '-o', 'ServerAliveCountMax=1',
             SOURCE['host'], command],
            capture_output=True, timeout=SSH_TIMEOUT, check=True)
        if len(proc.stdout) > 16384:
            raise ValueError('oversized source status')
        return validate(json.loads(proc.stdout))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as exc:
        return unavailable('云下载现役状态读取失败或过期: ' + type(exc).__name__)
