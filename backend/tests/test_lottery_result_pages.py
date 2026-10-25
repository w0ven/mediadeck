"""Complete persisted results, parsed-text limits, ordered recovery without replay."""
import asyncio
import copy
import json
import re
import sqlite3
import time
from html import escape
from html.parser import HTMLParser

import pytest

from app.modules.economy_rules import economy_write
from app.modules.lottery import emoji, message_length, winner_mention
from tests.test_lottery import ADMIN, config, create, req
from tests.test_lottery import env as lottery_env  # noqa: F401
from tests.test_lottery_count import rows, saved, worker


@pytest.fixture
def e(request):
    return request.getfixturevalue('lottery_env')


def rendered(body):
    pieces = []
    parser = HTMLParser(convert_charrefs=True)
    parser.handle_data = pieces.append
    parser.feed(body)
    return ''.join(pieces)


def prepare(e, count=200, settle=True):
    lid = create(e, config(title='😀<&' * 20, prizes=[{'kind': 'manual', 'quantity': 200,
                           'name': '🎉<&' * 20, 'instructions': 'PRIVATE-NOTE'}]))
    # A real draw over explicit large-pool entries, without 200 unrelated HTTP requests.
    with economy_write(e.service.db) as conn:
        for i in range(count):
            conn.execute('INSERT INTO lottery_entries(lottery_id,tg_id,label,joined_at,registered) VALUES(?,?,?,?,0)',
                         (lid, str(880000000 + i), '😀<&' * 12 + str(i), int(time.time())))
    if settle:
        e.service.draw(lid)
    return lid


def result_rows(e, lid):
    return [r for r in rows(e, lid, 'lottery_notices') if r['kind'] == 'result' or r['kind'].startswith('result:')]


def assert_complete(e, lid):
    awards = rows(e, lid, 'lottery_awards')
    pages = result_rows(e, lid)
    links, shown_lines = [], []
    for i, page in enumerate(pages, start=1):
        payload = json.loads(page['payload_json'])
        plain = rendered(payload['text'])
        assert len(plain.encode('utf-16-le')) // 2 == message_length(payload['text']) <= 4096
        assert payload['reply_markup']['inline_keyboard'][0][0]['text'] == '查看结果'
        assert payload['message_thread_id'] == 9
        assert all(t not in plain for t in ('其余', '省略', 'PRIVATE-NOTE', '名额不足'))
        if len(pages) > 1:
            assert f'（{i}/{len(pages)}）' in plain.splitlines()[0]
        links += re.findall(r'tg://user\?id=([0-9]+)', payload['text'])
        shown_lines += payload['text'].split('\n\n', 1)[1].splitlines()
    assert links == [a['tg_id'] for a in awards]
    assert shown_lines == [f'{emoji("crown")} {winner_mention(a)} · {escape(json.loads(a["prize_json"])["name"])}' for a in awards]
    return pages


def test_33_short_winners_remain_one_message_not_limited_by_html(e):
    lid = create(e, config(), publish=False)
    winners = [{'tg_id': str(880000000 + i), 'label': 'Jason & <Q>', 'prize_json': json.dumps({'name': '本人专属注册资格'})} for i in range(33)]
    payloads = e.service.result_payloads(e.service.row(lid), winners)
    assert len(payloads) == 1 and payloads[0]['text'].count('tg://user?id=') == 33
    assert len(payloads[0]['text'].encode('utf-16-le')) // 2 > 4096
    assert message_length(payloads[0]['text']) < 4096


def test_large_draw_stages_every_winner_in_whole_lines_and_admin_pages(e):
    lid = prepare(e)
    pages = assert_complete(e, lid)
    assert len(pages) > 1 and len(rows(e, lid, 'lottery_awards')) == 200
    assert len(rows(e, lid, 'lottery_dms')) == 200
    admin = req(e, 'GET', '/' + lid).json()
    shown = [n for n in admin['notices'] if n['kind'] == 'result']
    assert [n['page'] for n in shown] == list(range(1, len(pages) + 1))
    assert {n['pages'] for n in shown} == {len(pages)}
    before = copy.deepcopy(pages)
    e.service.draw(lid)
    assert result_rows(e, lid) == before


@pytest.mark.parametrize('failure,state', [
    ({'error_code': 400, 'description': "Bad Request: can't parse entities"}, 'failed'),
    ({'error_code': 500, 'description': 'server failure'}, 'unknown'),
    ({'error_code': 429, 'description': 'too many', 'parameters': {'retry_after': 120}}, 'pending'),
])
def test_partial_failure_recovers_only_failed_page_and_never_replays_sent_part(e, monkeypatch, failure, state):
    lid = prepare(e)
    pages = assert_complete(e, lid)
    original = e.service.bot._call
    attempted = []

    async def fail_second(method, payload=None, **kw):
        if method in ('sendMessage', 'editMessageText') and '开奖啦' in (payload or {}).get('text', ''):
            attempted.append((method, copy.deepcopy(payload)))
            if len(attempted) == 2:
                e.replies.append(failure)
        return await original(method, payload, **kw)
    monkeypatch.setattr(e.service.bot, '_call', fail_second)
    asyncio.run(e.service.deliver_notices(lid))
    changed = result_rows(e, lid)
    assert changed[0]['state'] == 'sent' and changed[1]['state'] == state
    assert all(r['state'] == 'pending' and r['attempts'] == 0 for r in changed[2:])
    first = copy.deepcopy(changed[0])
    awards = rows(e, lid, 'lottery_awards')
    asyncio.run(e.service.deliver_notices(lid))
    assert len(attempted) == 2 and result_rows(e, lid)[0] == first
    if state == 'pending':
        e.service.db.execute('UPDATE lottery_notices SET next_at=0 WHERE id=?', (pages[1]['id'],))
    else:
        if state == 'unknown':
            with pytest.raises(ValueError, match='发送结果不明'):
                e.service.recover_notice(lid, pages[1]['id'], {}, str(ADMIN))
        # Known Telegram message edits, not new sends, when the operator provides a verified ID.
        e.service.recover_notice(lid, pages[1]['id'], {'message_id': 777}, str(ADMIN))
    with worker(e) as restarted:
        asyncio.run(restarted.deliver_notices(lid))
    assert all(r['state'] == 'sent' for r in result_rows(e, lid))
    assert result_rows(e, lid)[0] == first and rows(e, lid, 'lottery_awards') == awards
    assert attempted[2][0] == ('sendMessage' if state == 'pending' else 'editMessageText')
    assert len(attempted) == len(pages) + 1
    assert_complete(e, lid)


def test_crashed_sending_page_becomes_unknown_and_holds_following_pages(e):
    lid = prepare(e)
    pages = result_rows(e, lid)
    e.service.db.execute("UPDATE lottery_notices SET state='sending',updated_at=? WHERE id=?",
                         (int(time.time()) - 500, pages[0]['id']))
    calls = len(e.calls)
    asyncio.run(e.service.deliver_notices(lid))
    assert len(e.calls) == calls
    assert result_rows(e, lid)[0]['state'] == 'unknown'
    assert all(r['attempts'] == 0 for r in result_rows(e, lid)[1:])
    e.service.recover_notice(lid, pages[0]['id'], {'confirmed_absent': True}, str(ADMIN))
    asyncio.run(e.service.deliver_notices(lid))
    assert all(r['state'] == 'sent' for r in result_rows(e, lid))


def test_cross_connection_senders_cannot_duplicate_pages(e, monkeypatch):
    lid = prepare(e)
    original = e.service.bot._call

    async def interleave(*args, **kw):
        await asyncio.sleep(0)
        return await original(*args, **kw)
    monkeypatch.setattr(e.service.bot, '_call', interleave)
    calls = len([m for m, _ in e.calls if m == 'sendMessage'])
    with worker(e) as other:
        async def run():
            await asyncio.gather(e.service.deliver_notices(lid), other.deliver_notices(lid))
            await other.deliver_notices(lid)
        asyncio.run(run())
    pages = result_rows(e, lid)
    assert all(r['state'] == 'sent' and r['attempts'] == 1 for r in pages)
    assert len([m for m, _ in e.calls if m == 'sendMessage']) - calls == len(pages)


def test_failure_staging_later_page_rolls_back_draw_and_all_earlier_pages(e):
    lid = prepare(e, settle=False)
    e.service.db.execute("CREATE TRIGGER reject_page BEFORE INSERT ON lottery_notices WHEN NEW.kind='result:2' "
                         "BEGIN SELECT RAISE(ABORT, 'isolated page storage failure'); END")
    with pytest.raises(sqlite3.DatabaseError):
        e.service.draw(lid)
    assert e.service.row(lid)['state'] == 'published'
    assert not result_rows(e, lid) and not rows(e, lid, 'lottery_awards') and not rows(e, lid, 'lottery_dms')
    e.service.db.execute('DROP TRIGGER reject_page')
    e.service.draw(lid)
    assert_complete(e, lid)


def test_old_frozen_truncated_result_is_never_regenerated_or_extended(e):
    lid = create(e)
    e.service.draw(lid)
    notice = result_rows(e, lid)[0]
    payload = json.loads(notice['payload_json'])
    payload['text'] = '历史已冻结公告\n其余 21 位见结果页'
    e.service.db.execute("UPDATE lottery_notices SET payload_json=?,state='failed' WHERE id=?", (json.dumps(payload), notice['id']))
    before, activity = rows(e, lid, 'lottery_notices'), saved(e, lid)
    with economy_write(e.service.db) as conn:
        e.service.stage_notice(conn, e.service.row(lid), 'result', [{'tg_id': '1', 'label': 'New',
                              'prize_json': json.dumps({'name': 'New'})}])
    e.service.draw(lid)
    asyncio.run(e.service.deliver_notices(lid))
    assert rows(e, lid, 'lottery_notices') == before and saved(e, lid) == activity


def test_blocked_continuations_do_not_starve_other_activities(e):
    blocked = []
    for _ in range(3):
        lid = prepare(e)
        first = result_rows(e, lid)[0]
        e.service.db.execute("UPDATE lottery_notices SET state='unknown' WHERE id=?", (first['id'],))
        blocked.extend(result_rows(e, lid)[1:])
    assert len(blocked) > 20  # enough to fill the old global pending batch
    live = prepare(e, count=1)
    asyncio.run(e.service.deliver_notices())
    assert result_rows(e, live)[0]['state'] == 'sent'
    assert all(e.service.db.one('SELECT state,attempts FROM lottery_notices WHERE id=?', (n['id'],)) ==
               {'state': 'pending', 'attempts': 0} for n in blocked)
