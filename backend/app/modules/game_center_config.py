"""Plugin declaration and validated, table-snapshotted game-center settings."""
from __future__ import annotations

import json
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.modules.economy_rules import day_bounds, encode
from app.modules.plugins import Field, Plugin, Spec

TIERS = ('practice', 'novice', 'normal', 'pro', 'master', 'supreme')
NAMES = ('练习场', '新手场', '普通场', '高手场', '大师场', '至尊场')
ZJH_TIERS = [{'id': key, 'name': name, 'ante': ante, 'cap': cap,
              'min_balance': cap or 2500, 'currency': 'beans' if key == 'practice' else 'points'}
             for key, name, ante, cap in zip(TIERS, NAMES, (10, 1, 5, 10, 20, 50), (500, 50, 250, 500, 1000, None))]
HOLDEM_TIERS = [{'id': key, 'name': name, 'small_blind': sb, 'big_blind': bb,
                 'buy_min': lo, 'buy_max': hi, 'min_balance': lo,
                 'currency': 'beans' if key == 'practice' else 'points'}
                for key, name, sb, bb, lo, hi in zip(TIERS, NAMES, (10, 1, 5, 10, 25, 50),
                    (20, 2, 10, 20, 50, 100), (2000, 40, 200, 400, 1000, 2000),
                    (2000, 200, 1000, 2000, 5000, None))]
BOARD_TIERS = [{'id': key, 'name': name, 'stake': stake, 'min_balance': stake,
                'currency': 'beans' if key == 'practice' else 'points'}
               for key, name, stake in zip(TIERS, NAMES, (100, 10, 50, 200, 500, 2000))]
DEFAULTS = {'xiangqi_enabled': False, 'gomoku_enabled': False,
            'xiangqi_tiers': encode(BOARD_TIERS), 'gomoku_tiers': encode(BOARD_TIERS),
            'xiangqi_total_seconds': 600, 'xiangqi_step_seconds': 60,
            'gomoku_total_seconds': 300, 'gomoku_step_seconds': 30,
            'zjh_enabled': False, 'holdem_enabled': False, 'zjh_tiers': encode(ZJH_TIERS),
            'holdem_tiers': encode(HOLDEM_TIERS), 'step_seconds': 30, 'zjh_auto_hands': 3,
            'holdem_auto_hands': 10, 'holdem_auto_seconds': 600, 'holdem_away_seconds': 600,
            'allow_235': False, 'daily_beans': 10000, 'broadcast_enabled': False,
            'broadcast_groups': ''}


def deep_link(base, param='games'):
    parts = urlsplit(str(base or ''))
    if parts.scheme != 'https' or not parts.netloc:
        return ''
    pairs = dict(parse_qsl(parts.query, keep_blank_values=True))
    pairs['startapp' if parts.hostname in ('t.me', 'telegram.me') else 'tgWebAppStartParam'] = param
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment))


def tiers(config, game):
    if game not in ('zjh', 'holdem', 'xiangqi', 'gomoku'):
        raise ValueError('游戏不存在')
    try:
        rows = json.loads(config[game + '_tiers'])
        if not isinstance(rows, list) or len(rows) != 6 or {r['id'] for r in rows} != set(TIERS):
            raise ValueError
        for r in rows:
            if not isinstance(r.get('name'), str) or not 1 <= len(r['name']) <= 20:
                raise ValueError
            if r['currency'] != ('beans' if r['id'] == 'practice' else 'points'):
                raise ValueError
            if game in ('xiangqi', 'gomoku'):
                if type(r.get('stake')) is not int or not 1 <= r['stake'] <= 10**12:
                    raise ValueError
                if r['id'] == 'practice' and r['stake'] != 100:
                    raise ValueError
                r['min_balance'] = r['stake']
                continue
            keys = ('ante', 'min_balance') if game == 'zjh' else ('small_blind', 'big_blind', 'buy_min')
            if any(type(r.get(k)) is not int or not 1 <= r[k] <= 10**12 for k in keys):
                raise ValueError
            maximum = r['cap' if game == 'zjh' else 'buy_max']
            low = r['ante' if game == 'zjh' else 'buy_min']
            if maximum is None:
                if r['id'] != 'supreme':
                    raise ValueError
            elif type(maximum) is not int or not low <= maximum <= 10**12:
                raise ValueError
            if game == 'zjh':
                r['min_balance'] = r['cap'] or r['min_balance']
                if r['min_balance'] < r['ante']:
                    raise ValueError
            else:
                if r['big_blind'] < r['small_blind'] or r['buy_min'] < r['big_blind']:
                    raise ValueError
                if r['id'] == 'practice' and (r['buy_min'] != 2000 or r['buy_max'] != 2000):
                    raise ValueError
                r['min_balance'] = r['buy_min']
        return sorted(rows, key=lambda r: TIERS.index(r['id']))
    except (ValueError, KeyError, TypeError):
        raise ValueError('场次 JSON 无效：须保留六场、币种及有效正整数范围') from None


class GameCenterPlugin(Plugin):
    spec = Spec(id='game_center', name='游戏中心', icon='🎮', category='points',
                description='Mini App 炸金花、德州、象棋、五子棋真人积分桌与机器人欢乐豆练习。默认关闭；不抽水。',
                fields=[
                    Field('zjh_enabled', '炸金花', 'bool', False),
                    Field('holdem_enabled', '德州扑克', 'bool', False),
                    Field('xiangqi_enabled', '象棋', 'bool', False),
                    Field('gomoku_enabled', '五子棋', 'bool', False),
                    Field('xiangqi_tiers', '象棋六场参数 JSON', 'text', DEFAULTS['xiangqi_tiers']),
                    Field('gomoku_tiers', '五子棋六场参数 JSON', 'text', DEFAULTS['gomoku_tiers']),
                    Field('xiangqi_total_seconds', '象棋每方总秒数', 'int', 600, min=10, max=7200),
                    Field('xiangqi_step_seconds', '象棋每步秒数', 'int', 60, min=5, max=120),
                    Field('gomoku_total_seconds', '五子棋每方总秒数', 'int', 300, min=10, max=7200),
                    Field('gomoku_step_seconds', '五子棋每步秒数', 'int', 30, min=5, max=120),
                    Field('zjh_tiers', '炸金花六场参数 JSON', 'text', DEFAULTS['zjh_tiers']),
                    Field('holdem_tiers', '德州六场参数 JSON', 'text', DEFAULTS['holdem_tiers']),
                    Field('step_seconds', '每步操作秒数', 'int', 30, min=5, max=120),
                    Field('zjh_auto_hands', '炸金花连续托管手数上限', 'int', 3, min=1, max=20),
                    Field('holdem_auto_hands', '德州连续托管手数上限', 'int', 10, min=1, max=100),
                    Field('holdem_auto_seconds', '德州连续托管秒数上限', 'int', 600, min=30, max=3600),
                    Field('holdem_away_seconds', '德州暂离离桌秒数', 'int', 600, min=30, max=3600),
                    Field('allow_235', '杂色235吃豹子', 'bool', False),
                    Field('daily_beans', '每日欢乐豆补足', 'int', 10000, min=1, max=10**9),
                    Field('broadcast_enabled', '群拉人卡播报', 'bool', False),
                    Field('broadcast_groups', '播报群（每行一个负数群ID）', 'text', '',
                          help='必须同时位于 Bot 群互动授权列表；只发公开昵称，不含余额。'),
                ])

    def normalize_config(self, config):
        cfg = {**DEFAULTS, **config}
        for game in ('zjh', 'holdem', 'xiangqi', 'gomoku'):
            cfg[game + '_tiers'] = encode(tiers(cfg, game))
        for game in ('xiangqi', 'gomoku'):
            for key, low, high in (('total_seconds', 10, 7200), ('step_seconds', 5, 120)):
                value = cfg[game + '_' + key]
                if type(value) is not int or not low <= value <= high:
                    raise ValueError('棋钟参数须为有效范围内的整数')
        groups = str(cfg['broadcast_groups']).split()
        if any(not re.fullmatch(r'-[1-9][0-9]{0,19}', group) for group in groups) or len(groups) > 20:
            raise ValueError('播报群须为负数ID，最多20个')
        cfg['broadcast_groups'] = '\n'.join(dict.fromkeys(groups))
        return cfg

    def validate_config(self, config):
        self.normalize_config(config)

    def readonly_status(self):
        db = self.ctx.db
        _, start, end = day_bounds(time.time())
        holdem_input = sum(sum(p['committed'] for p in json.loads(row['state_json'])['engine']['seats'])
                           for row in db.query("SELECT h.state_json FROM gc_hands h JOIN gc_tables t ON t.id=h.table_id "
                                               "WHERE t.game='holdem' AND t.currency='points' AND h.settled_at>=? AND h.settled_at<?", (start, end)))
        return {'在桌人数': db.one('SELECT COUNT(DISTINCT user_id) n FROM gc_seats')['n'],
                '今日局数': db.one('SELECT COUNT(*) n FROM gc_hands WHERE settled_at>=? AND settled_at<?', (start, end))['n'],
                '今日积分投入流水': holdem_input + db.one("SELECT COALESCE(SUM(amount),0) n FROM play_cash_events WHERE scope='gc_zjh' AND operation='consume' AND created_at>=? AND created_at<?", (start, end))['n'],
                '今日德州冻结转移': db.one("SELECT COALESCE(SUM(amount),0) n FROM play_cash_events WHERE scope='gc_holdem' AND operation='transfer_out' AND created_at>=? AND created_at<?", (start, end))['n'],
                '播报结果不明': db.one("SELECT COUNT(*) n FROM gc_outbox WHERE state='unknown'")['n'],
                '资金明细': '/api/game-center/cash?table_id=牌桌ID 或 user_id=会员ID'}

    async def run(self, config):
        return self.readonly_status()
