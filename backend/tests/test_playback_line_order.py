"""Persisted route order is also the member-visible Bot order."""
import asyncio

from fastapi.testclient import TestClient
from test_telegram import ADMIN, _bot

from app.main import app


def test_saved_order_preserves_whitelist_and_bot_display():
    lines = [
        {'label': 'Third', 'url': 'https://c.example.test', 'hint': 'keep hint'},
        {'label': 'Private', 'url': 'https://b.example.test', 'whitelist_only': True},
        {'label': 'First', 'url': 'https://a.example.test'},
    ]
    with TestClient(app) as client:
        response = client.post('/api/settings/telegram', auth=ADMIN, json={
            'playback_lines': lines, 'playback_lines_show_load': False})
        assert response.status_code == 200
        saved = client.get('/api/settings/telegram', auth=ADMIN).json()
        assert saved['playback_lines'] == lines
        bot = _bot(cfg=saved)
        public = asyncio.run(bot._nodes_text({'group_id': 'standard', 'state': 'active'}))
        assert public.index('Third') < public.index('First')
        assert 'Private' not in public
        private = asyncio.run(bot._nodes_text({'group_id': 'whitelist', 'state': 'active'}))
        assert private.index('Third') < private.index('Private') < private.index('First')
        assert 'keep hint' in private
