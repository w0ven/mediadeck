"""Actual updates and purchase handlers; no live Telegram delivery."""
import asyncio

import pytest
from test_economy_bot import env as bot_env  # noqa: F401
from test_tg_interaction_context import VIEWER, click, command
from test_tg_interaction_context import env as interaction_env  # noqa: F401


@pytest.fixture
def env(request):
    e = request.getfixturevalue('bot_env')
    e.services.points.add('u1', 500, 'isolated.test')
    return e


@pytest.mark.parametrize('fail_notice', [False, True])
def test_custom_actual_purchase_privately_delivers_or_preserves_failure_once_and_retrievable(env, fail_notice):
    item = env.services.shop.create({'kind': 'custom', 'name': '本地隔离自定义', 'cost': 99,
                                    'description': '公开简介', 'purchase_notice': '购买后私密内容<&>'})
    original = env.bot._call

    async def call(method, payload=None, **kwargs):
        if fail_notice and method == 'sendMessage' and '购买后说明</b>' in (payload or {}).get('text', ''):
            env.tg.calls.append((method, dict(payload)))
            return None
        return await original(method, payload, **kwargs)

    env.bot._call = call

    async def flow():
        mid = await command(env, '/start', chat=VIEWER, user=VIEWER)
        await click(env, 'shop', mid, chat=VIEWER, user=VIEWER)
        assert '购买后私密内容' not in env.tg.text(VIEWER, mid)
        await click(env, f"buy:{item['id']}", mid, chat=VIEWER, user=VIEWER)
        assert '保留7天' in env.tg.text(VIEWER, mid) and '购买后私密内容' not in env.tg.text(VIEWER, mid)
        await click(env, f"buyok:{item['id']}", mid, chat=VIEWER, user=VIEWER)
        await click(env, f"buyok:{item['id']}", mid, chat=VIEWER, user=VIEWER)
        card = env.services.bag.items('u1')[0]
        assert card['notice_state'] == ('failed' if fail_notice else 'sent')
        assert len(env.services.shop.orders('u1')) == 1
        private = [p for method, p in env.tg.calls if method == 'sendMessage' and '购买后私密内容' in p.get('text', '')]
        assert len(private) == 1 and str(private[0]['chat_id']) == str(VIEWER)
        assert '&lt;&amp;&gt;' in private[0]['text']
        feedback = [p for method, p in env.tg.calls if method == 'sendMessage' and '兑换成功' in p.get('text', '')][-1]
        assert feedback['reply_parameters']['message_id'] == mid
        assert ('私聊说明发送未确认' in feedback['text']) is fail_notice
        mid = await click(env, 'inventory', mid, chat=VIEWER, user=VIEWER)
        assert f"notice:{card['id']}" in {b['callback_data'] for b in env.tg.actions(VIEWER, mid)}
        assert '购买后私密内容' not in env.tg.text(VIEWER, mid)
        # Another bound user cannot access or receive the owner's paid snapshot.
        env.tg.calls.clear()
        other = await command(env, '/start', chat=904, user=904)
        await click(env, f"notice:{card['id']}", other, chat=904, user=904)
        assert not any('购买后私密内容' in p.get('text', '') for _, p in env.tg.calls)
        env.bot._call = original
        await click(env, f"notice:{card['id']}", mid, chat=VIEWER, user=VIEWER)
        assert env.services.bag.items('u1')[0]['notice_state'] == 'sent'
        assert env.services.points.balance('u1') == 451

    asyncio.run(flow())


def test_custom_long_paid_text_chunks_are_safe_bounded_and_binding_change_does_not_leak(env):
    notice = '<&>' * 2000
    item = env.services.shop.create({'kind': 'custom', 'name': '隔离长说明', 'cost': 1, 'purchase_notice': notice})
    result = env.services.shop.redeem('u1', item['id'])
    member = env.members.get('u1')
    assert asyncio.run(env.bot._send_custom_notice(member, result['card_id']))
    sent = [p for m, p in env.tg.calls if m == 'sendMessage']
    assert len(sent) > 1 and all(len(p['text']) < 4096 for p in sent)
    assert all(str(p['chat_id']) == str(VIEWER) for p in sent)
    env.members.bind_telegram('u1', '9876')
    env.tg.calls.clear()
    assert not asyncio.run(env.bot._send_custom_notice(member, result['card_id']))
    assert not env.tg.calls and env.services.bag.custom_notice('u1', result['card_id'])['notice_state'] == 'failed'
