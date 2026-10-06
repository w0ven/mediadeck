"""Temporary group replies: reproduce /usage@bot retention and lifecycle boundaries."""
import asyncio
from unittest.mock import Mock

import pytest
from test_bot_group_interactions import (
    ADMIN,
    ALICE,
    GROUP,
    _cb,
    _msg,
    base_bot,  # noqa: F401
    group_bot,  # noqa: F401
)

from app.modules.report_delivery import CALL_DELIVERY
from app.modules.telegram import _CALL_ERROR, GROUP_BRIEF_TTL


def message(text='/usage@cola_embybot', *, user=ALICE, thread=12, mid=42, chat=GROUP):
    msg = _msg(text, user=user, thread=thread, chat=chat,
               chat_type='private' if int(chat) > 0 else 'supergroup')
    msg['message_id'] = mid
    return msg


def deletions(bot):
    return [p['message_id'] for m, p in bot.calls if m == 'deleteMessage']


@pytest.mark.parametrize('command', ['/usage', '/me', '/myinfo', '/help', '/rules', '/start',
                                      '/register', '/claim', '/rebind', '/resetpw',
                                      '/requests', '/request', '/req', '/uploader'])
def test_addressed_group_command_cleans_trigger_and_schedules_reply(bot, monkeypatch, command):
    schedule = Mock()
    monkeypatch.setattr(bot, '_schedule_brief_cleanup', schedule)
    asyncio.run(bot._handle_message(message(command + '@cola_embybot')))
    assert any(m == 'sendMessage' and p.get('message_thread_id') == 12 for m, p in bot.calls)
    assert 42 in deletions(bot)
    schedule.assert_called_once_with(GROUP)


@pytest.mark.parametrize('failure', [None, False, True])
def test_usage_unconfirmed_send_does_not_delete_trigger_or_schedule(bot, monkeypatch, failure):
    schedule = Mock()
    monkeypatch.setattr(bot, '_schedule_brief_cleanup', schedule)
    original = bot._call

    async def call(method, payload=None, timeout=20):
        if method == 'sendMessage':
            return failure  # a send result without a message id is not a delivered card
        return await original(method, payload, timeout)

    bot._call = call
    asyncio.run(bot._handle_message(message()))
    assert not deletions(bot)
    schedule.assert_not_called()


@pytest.mark.parametrize('text,user,chat', [
    ('/usage', '9999', GROUP), ('/me', '9999', GROUP), ('/me extra', ALICE, GROUP),
    ('/usage@anotherbot', ALICE, GROUP), ('普通聊天', ALICE, GROUP),
    ('/unknown', ALICE, GROUP), ('/usage', ALICE, -1001111111111),
])
def test_unanswered_or_invalid_group_messages_are_kept(bot, monkeypatch, text, user, chat):
    schedule = Mock()
    monkeypatch.setattr(bot, '_schedule_brief_cleanup', schedule)
    asyncio.run(bot._handle_message(message(text, user=user, chat=chat)))
    assert not deletions(bot)
    schedule.assert_not_called()


@pytest.mark.parametrize('command', ['/usage', '/myinfo', '/help', '/rules', '/start'])
def test_private_lifecycle_is_unchanged(bot, monkeypatch, command):
    schedule = Mock()
    monkeypatch.setattr(bot, '_schedule_brief_cleanup', schedule)
    asyncio.run(bot._handle_message(message(command, chat=ALICE, thread=None)))
    assert not deletions(bot)
    assert any(m == 'sendMessage' for m, _ in bot.calls)
    schedule.assert_not_called()


def test_two_actors_topics_refresh_and_stale_timer_are_isolated(bot):
    async def scenario():
        await asyncio.gather(*(bot._dispatch_update({'message': message(user=user, thread=topic, mid=mid)})
                               for user, topic, mid in [(ALICE, 12, 42), (ADMIN, 12, 43), (ALICE, 13, 44)]))
        keys = [bot._session_key(GROUP, u, group=True, thread_id=t)
                for u, t in [(ALICE, 12), (ADMIN, 12), (ALICE, 13)]]
        mids = [bot._panel[k] for k in keys]
        assert len(set(mids)) == 3 and len(bot._brief_cleanups) == 3
        mid = mids[0]
        old_generation, old_task = bot._brief_cleanups[(str(GROUP), mid)]
        await bot._dispatch_update({'callback_query': _cb('me', user=ALICE, mid=mid, thread=12)})
        generation, new_task = bot._brief_cleanups[(str(GROUP), mid)]
        assert generation is not old_generation and new_task is not old_task
        await bot._expire_own_card(GROUP, mid, delay=0, generation=old_generation, session=keys[0])
        assert mid not in deletions(bot)
        # Exercise the current generation without waiting a wall-clock minute.
        await bot._expire_own_card(GROUP, mid, delay=0, generation=generation, session=keys[0])
        assert mid in deletions(bot) and keys[0] not in bot._panel
        assert bot._panel[keys[1]] == mids[1] and bot._panel[keys[2]] == mids[2]
        assert mids[1] not in deletions(bot) and mids[2] not in deletions(bot)
        assert (str(GROUP), mid) in bot._retired_panels
        assert all(p.get('message_id') != 42 for m, p in bot.calls if m == 'editMessageText')
    asyncio.run(scenario())


def test_ttl_is_sixty_seconds_and_expiry_removes_only_bot_reply(bot, monkeypatch):
    real_sleep = asyncio.sleep
    waits = []
    ready = asyncio.Event()
    release = asyncio.Event()

    async def sleep(delay):
        if delay == GROUP_BRIEF_TTL:
            waits.append(delay)
            ready.set()
            await release.wait()
        else:
            await real_sleep(delay)

    monkeypatch.setattr('app.modules.telegram.asyncio.sleep', sleep)

    async def scenario():
        await bot._handle_message(message())
        key = bot._session_key(GROUP, ALICE, group=True, thread_id=12)
        mid = bot._panel[key]
        await ready.wait()
        task = bot._brief_cleanups[(str(GROUP), mid)][1]
        assert deletions(bot) == [42] and waits == [60.0]
        release.set()
        await task
        assert deletions(bot) == [42, mid]
        assert key not in bot._panel and not bot._brief_cleanups
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['denied', 'timeout'])
def test_expiry_failure_is_quiet_and_does_not_claim_retirement(bot, failure):
    async def scenario():
        await bot._handle_message(message())
        key = bot._session_key(GROUP, ALICE, group=True, thread_id=12)
        mid = bot._panel[key]
        generation, _ = bot._brief_cleanups[(str(GROUP), mid)]
        bot._last_error = 'business failure'
        _CALL_ERROR.set('task business failure')
        CALL_DELIVERY.set({'state': 'unknown', 'reason': 'business outcome unknown'})

        async def refused(*args, **kwargs):
            bot._record_call_error('Forbidden')
            CALL_DELIVERY.set({'state': 'failed', 'reason': 'delete failure'})
            if failure == 'timeout':
                raise TimeoutError

        bot._call = refused
        await bot._expire_own_card(GROUP, mid, delay=0, generation=generation, session=key)
        assert bot._panel[key] == mid and (str(GROUP), mid) not in bot._retired_panels
        assert bot._card_owner(GROUP, mid) == ALICE
        assert bot._last_error == 'business failure' and _CALL_ERROR.get() == 'task business failure'
        assert CALL_DELIVERY.get() == {'state': 'unknown', 'reason': 'business outcome unknown'}
    asyncio.run(scenario())


def test_failed_edit_keeps_deadline_and_foreign_actor_cannot_refresh(bot):
    async def scenario():
        await bot._handle_message(message())
        mid = bot._panel[bot._session_key(GROUP, ALICE, group=True, thread_id=12)]
        before = bot._brief_cleanups[(str(GROUP), mid)]
        await bot._handle_callback(_cb('me', user=ADMIN, mid=mid, thread=12))
        assert bot._brief_cleanups[(str(GROUP), mid)] == before
        original = bot._call

        async def call(method, payload=None, timeout=20):
            if method == 'editMessageText':
                bot._record_call_error('Forbidden')
                return None
            return await original(method, payload, timeout)

        bot._call = call
        await bot._handle_callback(_cb('me', user=ALICE, mid=mid, thread=12))
        assert bot._brief_cleanups[(str(GROUP), mid)] == before
    asyncio.run(scenario())


def test_old_timer_cannot_delete_new_admin_target_or_confirmation(bot):
    async def scenario():
        await bot._handle_message(message('/me', user=ADMIN))
        key = bot._session_key(GROUP, ADMIN, group=True, thread_id=12)
        old_mid = bot._panel[key]
        generation, _ = bot._brief_cleanups[(str(GROUP), old_mid)]
        await bot._handle_message(message('/kk alice', user=ADMIN, mid=43))
        new_mid = bot._panel[key]
        await bot._handle_callback(_cb('admin_rm', user=ADMIN, mid=new_mid, thread=12))
        assert '不可恢复' in str(bot.calls)
        bot.calls.clear()
        await bot._expire_own_card(GROUP, old_mid, delay=0, generation=generation, session=key)
        assert not deletions(bot) and not bot._brief_cleanups
        assert bot._panel[key] == new_mid and bot._admin_panel(GROUP, new_mid)
    asyncio.run(scenario())


def test_expired_or_missing_message_replacement_gets_its_own_timer(bot):
    async def scenario():
        await bot._handle_message(message('/me'))
        key = bot._session_key(GROUP, ALICE, group=True, thread_id=12)
        old_mid = bot._panel[key]
        original = bot._call

        async def call(method, payload=None, timeout=20):
            if method == 'editMessageText':
                bot._record_call_error('message to edit not found')
                return None
            return await original(method, payload, timeout)

        bot._call = call
        await bot._handle_callback(_cb('me', user=ALICE, mid=old_mid, thread=12))
        new_mid = bot._panel[key]
        assert new_mid != old_mid
        assert (str(GROUP), old_mid) not in bot._brief_cleanups
        assert (str(GROUP), new_mid) in bot._brief_cleanups
    asyncio.run(scenario())


def test_announcements_and_broadcasts_never_get_ttl(bot, monkeypatch):
    async def scenario():
        await bot.post_job_progress('test', '<b>任务完成</b>')
        await bot.send_message(GROUP, '<b>处罚通知</b>')
        monkeypatch.setattr(bot, '_watch_rank_poster', lambda *args, **kwargs: no_photo())
        await bot.broadcast_watch_rank(str(GROUP))
        assert not bot._brief_cleanups
        assert not deletions(bot)

    async def no_photo():
        return None

    asyncio.run(scenario())


def test_refresh_serializes_against_expiry_in_same_chat(bot):
    async def scenario():
        await bot._dispatch_update({'message': message('/me')})
        key = bot._session_key(GROUP, ALICE, group=True, thread_id=12)
        mid = bot._panel[key]
        generation, _ = bot._brief_cleanups[(str(GROUP), mid)]
        entered, release = asyncio.Event(), asyncio.Event()
        original = bot._call

        async def call(method, payload=None, timeout=20):
            if method == 'editMessageText':
                entered.set()
                await release.wait()
            return await original(method, payload, timeout)

        bot._call = call
        refresh = asyncio.create_task(bot._dispatch_update({
            'callback_query': _cb('me', user=ALICE, mid=mid, thread=12)}))
        await entered.wait()
        expiry = asyncio.create_task(bot._expire_own_card(
            GROUP, mid, delay=0, generation=generation, session=key))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert mid not in deletions(bot)
        release.set()
        await refresh
        await expiry
        assert mid not in deletions(bot) and bot._panel[key] == mid
        assert bot._brief_cleanups[(str(GROUP), mid)][0] is not generation
    asyncio.run(scenario())


def test_not_modified_edit_refreshes_existing_timer_without_new_send(bot):
    async def scenario():
        await bot._handle_message(message('/me'))
        key = bot._session_key(GROUP, ALICE, group=True, thread_id=12)
        mid = bot._panel[key]
        old = bot._brief_cleanups[(str(GROUP), mid)][0]
        original = bot._call

        async def call(method, payload=None, timeout=20):
            if method == 'editMessageText':
                bot._record_call_error('Bad Request: message is not modified')
                return None
            return await original(method, payload, timeout)

        bot._call = call
        bot.calls.clear()
        await bot._handle_callback(_cb('me', user=ALICE, mid=mid, thread=12))
        assert bot._brief_cleanups[(str(GROUP), mid)][0] is not old
        assert not any(m == 'sendMessage' for m, _ in bot.calls)
    asyncio.run(scenario())
