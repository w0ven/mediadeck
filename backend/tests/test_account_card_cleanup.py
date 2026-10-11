"""Own compact cards are button-free; group-only exact-ID expiry is durable."""
import asyncio
import json

import pytest
from test_blackwhite import env as bw_env  # noqa: F401
from test_bot_reply_templates import demo_bot, plain
from test_checkin_interaction import economy_env, interaction_env  # noqa: F401
from test_group_command_cleanup import env as group_env  # noqa: F401
from test_group_command_cleanup import msg

from app.modules.checkin_cleanup import AccountCardCleanup


@pytest.fixture
def env(request):
    return request.getfixturevalue('group_env')


def test_compact_card_keeps_values_not_explanatory_notes():
    bot, member, _ = demo_bot()
    bot._stats.watch_summary = lambda uid: {'seconds_24h': 0, 'seconds_30d': 0,
        'recorded_seconds': 28860, 'coverage_note': '累计保留已记录观看；缺失片段不补算。',
        'unverified_playback_now': True}
    text = plain(bot._account_card(member))
    assert '累计观看：8小时1分' in text and '近24小时：0秒' in text
    assert not any(s in text for s in ('按设备名称', '非硬件', '累计保留', '待核验', '不补算'))
    assert len(text.splitlines()) <= 17
    assert '非硬件' in bot._account_card(member, managing=True)


@pytest.mark.parametrize('verb', ['/me', '/myinfo'])
def test_group_account_exact_id_due_time_restart_and_no_buttons(env, verb):
    async def run():
        env.services.registry.save('checkin_cleanup', enabled=False)
        await env.bot._dispatch_update({'message':msg(env, verb)})
        job = env.db.one("SELECT * FROM play_jobs WHERE kind='account.card.delete'")
        target = json.loads(job['payload_json'])
        sent = next(p for m,p in reversed(env.tg.calls) if m=='sendMessage')
        assert not sent.get('reply_markup') and target['thread_id'] == 88
        assert target['message_id'] not in (1901, 111)
        assert job['due_at'] - job['created_at'] == 60
        await env.bot._drain_account_cards(now=job['due_at']-.1)
        assert not any(m=='deleteMessage' and p['message_id']==target['message_id'] for m,p in env.tg.calls)
        service=AccountCardCleanup(env.db)
        assert service.claim('123', now=job['due_at'])
        await env.bot._drain_account_cards(now=job['due_at']+46)
        await env.bot._drain_account_cards(now=job['due_at']+47)
        assert sum(m=='deleteMessage' and p['message_id']==target['message_id'] for m,p in env.tg.calls)==1
        assert env.db.one('SELECT state FROM play_jobs WHERE id=?',(job['id'],))['state']=='deleted'
    asyncio.run(run())


def test_private_and_failed_send_not_queued(env):
    async def run():
        await env.bot._dispatch_update({'message':msg(env, '/me', private=True)})
        assert not env.db.query("SELECT * FROM play_jobs WHERE kind='account.card.delete'")
        keyboard = [p for m,p in env.tg.calls if m=='sendMessage'][-1]['reply_markup']
        assert {'resetpw', 'delete_account'} <= {
            b['callback_data'] for row in keyboard['inline_keyboard'] for b in row}
        env.public_mode='denied'
        await env.bot._dispatch_update({'message':msg(env, '/me', mid=1902)})
        assert not env.db.query("SELECT * FROM play_jobs WHERE kind='account.card.delete'")
    asyncio.run(run())
