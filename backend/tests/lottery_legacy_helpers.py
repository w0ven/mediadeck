"""Explicit pre-eligibility-v2 stored fixtures; not a client-selectable API mode."""
import json
import secrets
import time


def legacy_create(e, raw=None, publish=True):
    """Simulate a real old draft without either server-owned rules version.

    Current validation's legacy branch is the unchanged publish-time validator.
    Only this isolated test database is seeded; normal create() stays the real API.
    """
    from tests.test_lottery import ADMIN, config, req

    raw = config() if raw is None else raw
    saved = e.service.validate(raw, legacy=True)
    saved.pop('eligibility_version')
    saved.pop('public_rules_version')
    saved['device_check'] = bool(raw.get('device_check', False) and raw.get('anti_multi', True))
    lid = secrets.token_hex(12)
    e.service.db.execute('INSERT INTO lotteries(id,config_json,created_by,created_at) VALUES(?,?,?,?)',
                         (lid, json.dumps(saved), str(ADMIN), int(time.time()) - 3600))
    if publish:
        result = req(e, 'POST', f'/{lid}/publish', json={'revision': 1})
        assert result.status_code == 200, result.text
    return lid
