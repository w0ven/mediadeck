"""Actual Chromium/panel scripts consume activity fields from the real isolated API."""
import json
import shutil
from pathlib import Path

import pytest
from test_member_activity import DAY, NOW
from test_member_activity_api import AUTH
from test_member_activity_api import (
    panel as api_panel,  # noqa: F401 - register shared pytest fixture
)
from test_members_ui import CHROMIUM, _parse_report, _run_chromium, _write_harness

from app.main import app


@pytest.mark.skipif(not CHROMIUM, reason='Chromium is required')
def test_activity_controls_labels_combination_pagination_and_selection(request, monkeypatch, tmp_path):
    panel = request.getfixturevalue('api_panel')
    app.state.members.upsert('eta', 'eta', {'group_id': 'vip'})
    app.state.db.execute('UPDATE members SET created_at=? WHERE emby_user_id=?', (NOW - 60 * DAY, 'eta'))
    original = app.state.db.query
    with monkeypatch.context() as context:
        def failed_totals(sql, *args):
            if 'FROM watch_verified_totals' in sql:
                raise RuntimeError('isolated failure')
            return original(sql, *args)
        context.setattr(app.state.db, 'query', failed_totals)
        failed = panel.get('/api/members?page=1', auth=AUTH).json()['members']
        unknown = next(m for m in failed if m['emby_user_id'] == 'eta')
    rows = panel.get('/api/members?page=1', auth=AUTH).json()['members']
    rows = [unknown if m['emby_user_id'] == 'eta' else m for m in rows]
    groups = panel.get('/api/groups', auth=AUTH).json()
    page = _write_harness(tmp_path)
    shutil.copyfile(Path(__file__).with_name('member_activity_ui_browser.js'), tmp_path / 'test.js')
    markup = page.read_text(encoding='utf-8')
    markup = markup.replace('<script src="test.js"></script>',
                            f'<script>window.__ACTIVITY_ROWS={json.dumps(rows)};'
                            f'window.__ACTIVITY_GROUPS={json.dumps(groups)};</script>'
                            '<script src="test.js"></script>')
    page.write_text(markup, encoding='utf-8')
    report = _parse_report(_run_chromium(page, tmp_path, screenshot=tmp_path / 'activity.png'))
    assert report.get('ok'), report
    assert report['checks'] >= 25
