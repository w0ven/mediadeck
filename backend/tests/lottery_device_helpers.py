"""Synthetic device-check payloads for tests; no real device values."""
import base64
import hashlib
import json

from tests.test_lottery import browser_id


def traits(seed):
    def digest(part):
        return hashlib.sha256(f'synthetic-device:{seed}:{part}'.encode()).hexdigest()
    return {'a': digest('canvas'), 'b': f'Synthetic Vendor {seed}|Synthetic Renderer {seed}',
            'c': digest('webgl'), 'e': digest('audio'), 'f': '844x390x24x3', 'g': '8', 'h': '4', 'i': '5',
            'j': 'Pacific/Kiritimati', 'n': digest('fonts')}


def blob(payload):
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')


def device_ctx(user, seed=None, *, k=None, s=None):
    return blob({'v': 1, 'k': browser_id(user) if k is None else k, 's': traits(user if seed is None else seed)
                 if s is None else s})
