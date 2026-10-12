"""Configured opening windows are snapshots, never edits to active rounds."""
import asyncio
import json

import pytest
from test_dice import create, message, svc
from test_dice_banker import bw_env, economy_env, interaction_env  # noqa: F401
from test_dice_banker import env as banker_env  # noqa: F401
from test_dice_size_group import send

from app.modules import dice_beacon as beacon
from app.modules.dice import DiceService
from app.modules.dice_config import DEFAULT_WINDOW, MAX_WINDOW, MIN_WINDOW, window_seconds
from app.modules.dice_group_view import banker_help, group_card
from app.modules.play_money import PlayError


@pytest.fixture
def env(request):
    return request.getfixturevalue('banker_env')


def test_missing_saved_field_defaults_to_90_and_backend_exposes_setting(env):
    registry = env.services.registry
    field = next(f for f in registry.get('dice').spec.fields if f.key == 'window_seconds')
    assert (field.kind, field.default, field.min, field.max) == ('int', 90, 30, 600)
    assert '后续新局' in field.help and '30' in field.label and '600' in field.label
    assert registry.config('dice')['window_seconds'] == DEFAULT_WINDOW == 90
    row = asyncio.run(create(env))
    assert row['deadline']-row['created'] == 90
    assert json.loads(row['rules'])['window'] == 90
    assert row['timeout']-row['deadline'] == 600
    assert row['round_number'] == beacon.future_round(row['deadline'])
    assert registry.get('dice').readonly_status()['封盘秒数'] == 90
    assert '90秒封盘' in banker_help()


@pytest.mark.parametrize('seconds', [MIN_WINDOW, 121, MAX_WINDOW])
def test_saved_setting_reaches_bot_service_and_never_changes_existing_round(env, seconds):
    first = asyncio.run(create(env))
    svc(env).group_bet(message(env, '/大 10', index=1, mid=8100), '大', 10)
    service = svc(env)  # callable injection must not cache the previous setting
    env.services.registry.save('dice', config={'window_seconds': str(seconds)})
    frozen = service.get(first['nonce'])
    for key in ('created', 'deadline', 'timeout', 'round_number', 'rules'):
        assert frozen[key] == first[key]
    assert service.close(first['nonce'], now=first['deadline']-0.01)['state'] == 'open'
    second = service.create(message(env, '/猜大小 100', index=2, mid=9000, thread=22))
    assert second['deadline']-second['created'] == seconds
    assert json.loads(second['rules'])['window'] == seconds
    assert second['timeout'] == second['deadline']+600
    assert second['round_number'] == beacon.future_round(second['deadline'])
    plugin = env.services.registry.get('dice')
    assert plugin.readonly_status()['封盘秒数'] == seconds
    assert plugin.readonly_status()['开奖最长等待秒数'] == 600
    # Plugin.run uses the Bot factory and cannot revert timing on existing records.
    asyncio.run(plugin.run(env.services.registry.config('dice')))
    assert service.get(first['nonce'])['deadline'] == first['deadline']
    assert service.get(second['nonce'])['deadline'] == second['deadline']


@pytest.mark.parametrize('value', [0, 29, 601, -1, 90.5, True, 'bad', '90.5'])
def test_invalid_backend_values_rejected_without_saving(env, value):
    with pytest.raises(ValueError):
        env.services.registry.save('dice', config={'window_seconds': value})
    assert env.services.registry.config('dice')['window_seconds'] == 90
    assert not env.db.query('SELECT * FROM dice_rounds')


def test_invalid_injected_config_never_reserves_collateral(env):
    original = svc(env)
    service = DiceService(env.db, original.members, original.points, lambda: True,
                          lambda c: True, '123', config=lambda: {'window_seconds': 601})
    with pytest.raises(PlayError, match='封盘'):
        service.create(message(env))
    assert not env.db.query('SELECT * FROM dice_rounds')
    assert not env.db.query('SELECT * FROM play_escrows')
    assert env.services.points.balance(env.uids[0]) == 1000
    assert window_seconds() == 90


def test_help_and_play_hub_use_current_config_legacy_card_uses_frozen_window(env):
    async def run():
        env.services.registry.save('dice', config={'window_seconds': 150})
        await send(env, message(env, '/猜大小帮助', private=True))
        text = next(p['text'] for method, p in reversed(env.tg.calls) if method == 'sendMessage')
        assert '150秒封盘' in text and '180秒' not in text and '10分钟' in text
        panel = {'nonce': 'menu', 'chat_id': str(env.actors[0]['id']), 'tg_id': str(env.actors[0]['id'])}
        body, _ = env.bot._play_hub_view(panel, {'view': 'gamehelp', 'which': 'dice'})
        assert '150秒封盘' in body
        body, _ = env.bot._play_hub_view(panel, {'view': 'playhub'})
        assert '150秒封盘' in body
        row = await create(env)
        # Simulate a persisted pre-upgrade no-banker round: display is per-round.
        row.update(rules=json.dumps({'version': 'dice-size-group-v1', 'window': 180}), group_view='rules')
        snapshot = {'totals': [], 'count': 0, 'latest': [], 'ledger': [], 'page': 0, 'pages': 1}
        body, _ = group_card(row, snapshot)
        assert '180秒封盘' in body and '150秒封盘' not in body
        row['group_view'] = 'details'
        body, _ = group_card(row, snapshot)
        assert '180秒' in body
    asyncio.run(run())


def test_preexisting_180_second_banker_keeps_original_beacon_and_caption(env):
    from app.modules.dice_caption_view import card
    env.services.registry.save('dice', config={'window_seconds': 180})
    row = asyncio.run(create(env))
    svc(env).group_bet(message(env, '/小 10', index=1, mid=8100), '小', 10)
    env.services.registry.save('dice', config={'window_seconds': 90})
    current, snapshot = svc(env).group_snapshot(row['nonce'])
    assert current['deadline'] == row['created']+180
    assert current['round_number'] == beacon.future_round(row['deadline'])
    assert json.loads(current['rules'])['window'] == 180
    body, _ = card(current, snapshot, now=row['created']+91)
    assert '剩余 01:29' in body
    assert svc(env).close(row['nonce'], now=row['created']+91)['state'] == 'open'
