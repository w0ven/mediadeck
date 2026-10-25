"""Telegram lottery: frozen configuration, once-only settlement and durable delivery.

No network call is made inside a write transaction. SQLite BEGIN IMMEDIATE owns
all admission, draw and reward transitions, including across workers/restarts.
"""
from __future__ import annotations

import copy
import json
import logging
import re
import secrets
import time
from datetime import datetime
from html import escape, unescape

from app.modules import lottery_captcha, lottery_device, lottery_identity, telegram_private, viewing
from app.modules.economy_rules import BEIJING, economy_write, encode, validate_bag_spec
from app.modules.inventory import InventoryService
from app.modules.registration import generate_code
from app.modules.report_delivery import CALL_DELIVERY

ENTRY_FEE_UNSET = object()
RULES = ('每个 Telegram 每期仅一次、最多一奖；报名时须属于发布群且符合参与对象。'
         '截止后不能报名；定时开奖到点自动开奖，手动开奖由管理员开奖，未设截止时开奖前均可报名。人数不足如实少发。'
         '本人专属注册资格仅在开奖时仍未注册者中抽取，先抽该类奖，再抽其他奖，'
         '同类奖项随机排序，每个名额在剩余合资格报名者中等概率抽取。'
         '账户类奖励未绑定时保留，绑定后可领取；商城道具入背包后使用生效。'
         '注册资格绑定中奖 Telegram，不可转赠，进入原 Bot 注册流程自行设置密码，受原注册通道开关约束。')
# Preserve the exact old public wording for configurations without a version.
# Only server-created new/copy drafts get v2; edits/publish keep the stored policy.
FAIRNESS = '发现多账号等违规参与，将取消中奖资格，报名费不退。'
LEGACY_RULES = RULES + FAIRNESS
PUBLIC_RULES_VERSION = 2
ELIGIBILITY_VERSION = 2
ELIGIBILITY_RULES = ('每个 Telegram 每期仅一次、最多一奖；报名时须属于发布群且符合参与对象。'
                     '定时开奖截止后不能报名；手动开奖不设截止时间，开奖前均可报名。人数不足如实少发。'
                     '按开奖时的注册状态抽取：本人注册资格仅未注册者，积分、邀请名额及商城奖品仅已注册者，人工奖品通用。'
                     '先抽注册资格，再抽其他奖；同类奖项随机排序，每个名额在剩余合资格报名者中等概率抽取。'
                     '已中奖的账户奖励解绑后保留，绑定后可领取；商城道具入背包后使用生效。'
                     '注册资格绑定中奖 Telegram，不可转赠，进入原 Bot 注册流程自行设置密码，受原注册通道开关约束。')
AUDIENCES = {'all': '全部群友', 'member': '仅已注册', 'guest': '仅未注册'}
# How a stored configuration WITHOUT a newer field is read: off / unlimited.
# New drafts get explicit values from validate(); publication freezes them.
LEGACY_DEFAULTS = {'min_level': 0, 'min_points': 0, 'min_watch_hours': 0, 'reminder': False, 'pin': False,
                   'winner_dm': False, 'live_count': False}
INTERNAL = ('chat_id', 'thread_id', 'anti_multi', 'reminder', 'pin', 'winner_dm', 'live_count', 'public_rules_version')
LEVEL_MAX = 8
# Reminder: ~1h before the deadline; not when published < 90 min before it;
# skipped once fewer than 10 min remain (missed maintenance window).
REMINDER_LEAD, REMINDER_NOTICE, REMINDER_CUTOFF = 3600, 5400, 600
LIVE_INTERVAL = 30
LEASE = 180
MAX_ATTEMPTS = 4
FINISHED_WINDOW = 7 * 86400
# Fixed transport reasons (report_delivery.failure) for private chats the Bot
# cannot reach: never started the Bot, blocked it, or deactivated account.
UNREACHABLE = ('用户已屏蔽机器人', '无法向该用户发送消息', 'Telegram 账号已注销', '会话不存在或用户尚未启动机器人')
RETRYABLE = ('连接失败，稍后重试', 'Telegram 限流')
# Alternatives are each sticker's exact Telegram `emoji` metadata (incl. variation selectors).
EMOJI = {'gift': ('5235826903490764103', '🎁'), 'star': ('5830101874467938532', '⭐️'),
         'notice': ('5197304993920616826', '📣'), 'check': ('5350486389806868244', '✅'),
         'present': ('5193085063998224234', '🎁'), 'party': ('5193209274452425995', '🎉'),
         'reaper': ('5861500009547045904', '⏰'), 'clock': ('5267421370114914946', '⏱'),
         'shield': ('5352888345972187597', '🛡'), 'crown': ('5449601904147440135', '👑'),
         'live': ('5260750418320836046', '🚨'), 'redpack': ('5868538327073822996', '🧧'),
         'warn': ('5188463524568926712', '⚠️'), 'done': ('5861526823027873795', '✅'),
         'prize0': ('5427225953463972959', '🎁'), 'prize1': ('5429263077927300012', '🎁'),
         'prize2': ('5427315847129478207', '🎁')}
# ColorfulFontEmoji 26–35: colourful digits 0–9.
DIGITS = [('5381817669425058178', '0️⃣'), ('5381828389663431280', '1️⃣'), ('5382051178207007027', '2️⃣'),
          ('5379910025340802255', '3️⃣'), ('5388624247696412237', '4️⃣'), ('5390859675094766802', '5️⃣'),
          ('5388691197646625260', '6️⃣'), ('5391035158868547187', '7️⃣'), ('5388710159927236114', '8️⃣'),
          ('5391071623140889606', '9️⃣')]
# ColorfulFontEmoji indices 11/20/2/10/24 render LUCKY, but each sticker's
# Telegram `emoji` metadata is 🔡. ASCII letters are not valid alternatives.
LETTERS = [('5364273836122463188', '🔡'), ('5368486821082715433', '🔡'),
           ('5364012727880668255', '🔡'), ('5364258799441957434', '🔡'), ('5381971708427132282', '🔡')]


def emoji(key):
    eid, fallback = EMOJI[key]
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'


def number(value):
    return ''.join(f'<tg-emoji emoji-id="{DIGITS[int(d)][0]}">{DIGITS[int(d)][1]}</tg-emoji>' for d in str(value))


def normalize_notice_payload(payload):
    """Repair only exact legacy tags emitted by this feature, including saved jobs.

    No HTML parser, arbitrary ID substitution, unescaping or regeneration from
    current activity data: frozen text, recipients, links and results stay intact.
    A failed/unknown job is NOT queued by this function or by an upgrade.
    """
    result = copy.deepcopy(payload)
    body = result['text']
    for (eid, fallback), old in zip(LETTERS, 'LUCKY'):
        body = body.replace(f'<tg-emoji emoji-id="{eid}">{old}</tg-emoji>',
                            f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>')
    # Match the exact original star metadata, including its variation selector.
    eid, fallback = EMOJI['star']
    body = body.replace(f'<tg-emoji emoji-id="{eid}">⭐</tg-emoji>',
                        f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>')
    result['text'] = body
    return result


def notice_rejection(error, failure):
    """Lottery-only fixed diagnostics; never retain external description fragments.

    The shared transport classifies delivery certainty; this only refines a
    definite rejection. Unknown/5xx/timeout must not acquire replay permission.
    The boolean permits ONE emoji-less attempt, never a generic content retry.
    """
    if failure.get('state') != 'failed':
        return '未取得确认，需管理员人工核验', False
    detail = str(error or '').lower()
    # Confirmed by a controlled Telegram comparison: ASCII letter alternatives
    # fail with ENTITY_TEXT_INVALID; the same IDs/buttons succeed with 🔡.
    # A format rejection is diagnostic, not permission to strip native artwork.
    if 'entity_text_invalid' in detail:
        return 'Telegram 拒绝实体替代文本（ENTITY_TEXT_INVALID）；请检查自定义表情的有效 emoji 替代值', False
    if 'button_icon_document_invalid' in detail:
        return 'Telegram 拒绝按钮自定义图标（BUTTON_ICON_DOCUMENT_INVALID）', False
    if ('custom emoji' in detail or 'custom_emoji' in detail or 'tg-emoji' in detail) and any(
            word in detail for word in ('alternative', 'valid emoji', 'entity', 'entities')):
        return 'Telegram 拒绝自定义表情替代文本或实体格式（400）', False
    if 'emoji' in detail and any(word in detail for word in (
            'invalid', 'not allowed', 'not_allowed', 'forbidden', 'premium',
            'unsupported', 'not supported', 'not_supported')):
        return 'Telegram 拒绝自定义表情（ID 无效、不支持或权限不足）', True
    if any(word in detail for word in ("can't parse entities", "can't find end", 'unsupported start tag',
                                       'entity_bounds_invalid', 'entity length', 'entity offset')):
        return 'Telegram 拒绝 HTML 实体格式（400）', False
    if any(word in detail for word in ('button_url_invalid', 'url_invalid', 'wrong http url', 'url host is empty',
                                       'unsupported url protocol', 'web_app_invalid', 'start_param_invalid')):
        return 'Telegram 拒绝按钮链接或 Mini App 参数（400）', False
    if any(word in detail for word in ('message is too long', 'message_too_long', 'message text is too long', 'text_too_long')):
        return 'Telegram 拒绝公告：正文过长（400）', False
    if any(word in detail for word in ('reply markup', 'reply_markup', 'inline keyboard', 'button_type_invalid', 'button_data_invalid')):
        return 'Telegram 拒绝按钮结构或参数（400）', False
    # No substring from error is persisted, even if transport redaction missed a
    # quoted title, secret prize instruction, URL or newly shaped credential.
    if failure.get('content_error'):
        return 'Telegram 拒绝公告参数（未识别的 400）；请管理员核验，未自动重发', False
    if failure.get('reason') == '机器人凭据不可用':
        return '机器人凭据不可用', False
    return 'Telegram 明确拒绝公告，请检查 Bot / 群权限或消息参数', False


def integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{label}须为 {low}–{high} 的整数')
    return value


def text(value, label, maximum, required=True):
    if not isinstance(value, str) or len(value.strip()) > maximum or (required and not value.strip()):
        raise ValueError(f'{label}须为{maximum}字以内的文字')
    return value.strip()


def winner_mention(winner):
    """Use the stored Telegram identity, never an account name or supplied URL."""
    label = str(winner.get('label') or '').strip().lstrip('@')
    display = '@' + label if label and label != 'Telegram用户' else 'Telegram用户'
    display = escape(display)
    tg_id = str(winner.get('tg_id') or '')
    if not re.fullmatch(r'[1-9][0-9]{0,15}', tg_id) or int(tg_id) > 2**52 - 1:
        return display
    return f'<a href="tg://user?id={tg_id}">{display}</a>'


def public_rules_version(config):
    """Read a server-stored policy; absence (or an unknown value) keeps old text."""
    value = config.get('public_rules_version')
    return PUBLIC_RULES_VERSION if type(value) is int and value == PUBLIC_RULES_VERSION else 1


def eligibility_version(config):
    value = config.get('eligibility_version')
    return ELIGIBILITY_VERSION if type(value) is int and value == ELIGIBILITY_VERSION else 1


def prize_audience(prize):
    return 'guest' if prize['kind'] == 'registration' else 'all' if prize['kind'] == 'manual' else 'member'


def eligible_for_prize(config, prize, registered):
    if eligibility_version(config) == 1:
        return prize['kind'] != 'registration' or not registered
    audience = prize_audience(prize)
    return audience == 'all' or (audience == 'member') == registered


def prize_eligibility(config, registered):
    """Public prize-type eligibility only; never consult an entry or risk signals."""
    if eligibility_version(config) == 1 or any(eligible_for_prize(config, p, registered) for p in config['prizes']):
        return True, ''
    return False, ('本期奖品仅限未注册者，你当前已注册' if registered else '本期奖品仅限已注册者，请先注册并绑定账户')


def entry_terms(config):
    fee = config.get('entry_fee', 0)
    if fee:
        return f'报名扣 {fee} 积分，成功报名后不退（未中奖不退），活动取消时全额退还；须绑定已注册积分账户。'
    return '免费参与。'


def requirement_parts(config):
    parts = []
    if config.get('min_level'):
        parts.append(f'Lv{config["min_level"]} 及以上')
    if config.get('min_points'):
        parts.append(f'积分余额不少于 {config["min_points"]}')
    if config.get('min_watch_hours'):
        parts.append(f'近 30 天观看满 {config["min_watch_hours"]} 小时')
    return parts


def requirement_terms(config):
    """Public participation conditions; never any risk-control wording."""
    parts = requirement_parts(config)
    if not parts:
        return ''
    note = '（积分只校验不扣）' if config.get('min_points') else ''
    return '参与门槛：' + '、'.join(parts) + note + '；须绑定已注册账户，报名时按当时数据判定。'


def gated(config):
    return bool(config.get('min_level') or config.get('min_points') or config.get('min_watch_hours'))


def accepting(config, now):
    """Entries are open until the deadline; a manual draw without one stays open until drawn."""
    return not config.get('deadline') or now < config['deadline']


def count_reached(conn, lottery_id, config):
    """The public entry total, including excluded entries; call under the writer lock."""
    return config['draw_mode'] == 'count' and conn.execute(
        'SELECT COUNT(*) FROM lottery_entries WHERE lottery_id=?', (lottery_id,)).fetchone()[0] >= config['max_entries']


def watch_seconds(db, uid, now):
    """The member-facing recorded-viewing window: StatsService.watch_summary()['seconds_30d']."""
    since = now - 30 * 86400
    return viewing.window(viewing.records(db, uid, since, now), since, now)['seconds']


def deep_link(row):
    return f'https://t.me/{row["bot_username"]}?startapp=lottery_{row["id"]}'


def live_line(key, config):
    phase, count = key.split(':')
    if phase == 'open':
        return f'已报名 {count} 人'
    if phase == 'closed':
        return f'报名已截止 · {count} 人'
    if phase == 'settled':
        return f'已开奖 · {count} 人参与'
    return '活动已取消' + (' · 报名积分已全额退还' if config.get('entry_fee') and count != '0' else '')


def live_status(key, config):
    phase, count = key.split(':')
    if phase == 'open':
        return f'{emoji("live")} {number(count)} 人已报名'
    if phase == 'closed':
        return f'{emoji("clock")} 报名截止 · {number(count)} 人'
    if phase == 'settled':
        return f'{emoji("party")} 已开奖 · {number(count)} 人参与'
    return emoji('warn') + ' 已取消' + (' · 积分已退还' if config.get('entry_fee') and count != '0' else '')


def with_live(text, key, config):
    """The status joins the title line, which is all a pinned-message bar shows."""
    head, sep, rest = text.partition('\n')
    return f'{head}  {live_status(key, config)}{sep}{rest}'


def message_length(body):
    """UTF-16 units after parsing our generated HTML, not serialized tag length.

    All caller-supplied labels/titles were escaped before generating these tags.
    """
    return len(unescape(re.sub(r'<[^>]*>', '', body)).encode('utf-16-le')) // 2


def has_custom(payload):
    return '<tg-emoji' in payload['text'] or any(
        'icon_custom_emoji_id' in button for buttons in (payload.get('reply_markup') or {}).get('inline_keyboard', [])
        for button in buttons)


def strip_custom(payload):
    """Only our known generated tags/icons; no general HTML interpretation."""
    for eid, fallback in LETTERS + list(EMOJI.values()) + DIGITS:
        payload['text'] = payload['text'].replace(f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>', fallback)
    for buttons in (payload.get('reply_markup') or {}).get('inline_keyboard', []):
        for button in buttons:
            button.pop('icon_custom_emoji_id', None)


def outcome(result, failure, error):
    """Shared certainty classification for every new lottery Telegram write.

    'retry' only for a definite connection failure (not a timeout) or a rate
    limit; timeouts, 5xx and unconfirmed replies are 'unknown' and never resent.
    """
    if result is not None and result is not False:
        return 'sent'
    if failure.get('not_modified'):
        return 'sent'
    if failure.get('state') == 'failed':
        return 'failed'
    if failure.get('state') == 'retry' and 'timeout' not in error and failure.get('reason') in RETRYABLE:
        return 'retry'
    return 'unknown'


def backoff(attempts, failure, now):
    return now + max(60 * 2 ** max(0, attempts - 1), int(failure.get('retry_after') or 0))


def rejection(error, failure, action):
    """Fixed pin/unpin/edit diagnostics; never echo the external description."""
    if failure.get('state') != 'failed':
        return '未取得确认，未自动重试'
    if any(word in error for word in ('rights', 'admin_required', 'administrator', 'not enough')):
        return f'Bot 没有{action}权限；未重试'
    return f'Telegram 拒绝{action}请求；未重试'


def public_prize(prize):
    result = {k: v for k, v in prize.items() if k not in ('spec', 'instructions')}
    if 'spec' in prize:
        # Public terms, never a custom product's private purchase notice.
        result['item_terms'] = {k: prize['spec'].get(k) for k in ('kind', 'amount', 'duration_days', 'retention_days')}
    return result


class LotteryService:
    def __init__(self, db, members, points, shop, registration, groups, bot):
        self.db, self.members, self.points, self.shop = db, members, points, shop
        self.registration, self.groups, self.bot = registration, groups, bot

    def levels_enabled(self):
        levels = getattr(self.members, '_levels', None)
        return bool(levels is not None and levels.config()['config']['enabled'])

    def validate(self, raw, *, now=None, incomplete=False, legacy=False):
        """Server-side draft validation. ``incomplete`` is only for copy(): the
        deadline (and a no-longer-allowlisted group) must be re-entered before
        the draft can be saved again or published. ``legacy`` is ONLY selected
        by publish from server-stored config, never from client fields."""
        now = int(time.time()) if now is None else now
        if not isinstance(raw, dict):
            raise ValueError('活动配置无效')  # noqa: TRY004 - API validation contract
        title = text(raw.get('title'), '标题', 60)
        anti_multi = raw.get('anti_multi', True)
        if type(anti_multi) is not bool:
            raise ValueError('防小号开关必须为布尔值')
        switches = {}
        for key, label in (('reminder', '截止前提醒'), ('pin', '静默置顶')):
            switches[key] = raw.get(key, True)  # new drafts: on by default
            if type(switches[key]) is not bool:
                raise ValueError(f'{label}开关必须为布尔值')
        audience = raw.get('audience')
        if audience not in AUDIENCES:
            raise ValueError('参与对象无效')
        clear_member_terms = not legacy and audience != 'member'
        entry_fee = 0 if clear_member_terms else integer(raw.get('entry_fee', 0), '报名积分', 0, 100000)
        if entry_fee and audience == 'guest':
            raise ValueError('收费活动需已注册积分账户，不能选择仅未注册参与')
        limits = {}
        for key, label, high in (('min_level', '最低等级', LEVEL_MAX), ('min_points', '最低积分余额', 1000000),
                                 ('min_watch_hours', '近 30 天观看时长（小时）', 720)):
            value = raw.get(key)
            limits[key] = 0 if clear_member_terms else integer(0 if value is None else value, label, 0, high)
        if gated(limits) and audience == 'guest':
            raise ValueError('设置参与门槛的活动需已注册账户，不能选择仅未注册参与')
        if limits['min_level'] and not self.levels_enabled():
            raise ValueError('等级体系未启用，不能设置最低等级门槛')
        chat = str(raw.get('chat_id') or '')
        if chat not in self.bot._group_allowlist() and not (incomplete and not chat):
            raise ValueError('请选择已配置白名单群')
        mode = raw.get('draw_mode')
        if mode not in ('manual', 'timed', 'count'):
            raise ValueError('开奖方式无效')
        max_entries = integer(raw.get('max_entries'), '开奖人数', 1, 100000) if mode == 'count' else 0
        if mode == 'count' or (not legacy and mode == 'manual'):
            deadline = None
            switches['reminder'] = False
        elif raw.get('deadline') is None:
            # Legacy manual drafts may still retain their original deadline.
            if not incomplete and mode == 'timed':
                raise ValueError('定时开奖须填写截止时间')
            deadline = None
        else:
            deadline = integer(raw.get('deadline'), '截止时间', now + 60, now + 366 * 86400)
        thread = raw.get('thread_id')
        if thread is not None:
            integer(thread, '话题 ID', 1, 2147483647)
        prizes = raw.get('prizes')
        if not isinstance(prizes, list) or not 1 <= len(prizes) <= 20:
            raise ValueError('请配置 1–20 个奖项')
        clean = []
        for p in prizes:
            if not isinstance(p, dict):
                raise ValueError('奖项无效')  # noqa: TRY004 - API validation contract
            kind = p.get('kind')
            if not legacy and audience == 'guest' and kind in ('points', 'invite', 'shop'):
                raise ValueError('仅未注册活动不能配置积分、邀请名额或商城奖品；请选注册资格或人工奖品')
            if not legacy and audience == 'member' and kind == 'registration':
                raise ValueError('仅已注册活动不能配置注册资格')
            item = {'kind': kind, 'name': text(p.get('name'), '奖项名称', 60),
                    'quantity': integer(p.get('quantity'), '获奖人数', 1, 200)}
            if kind in ('points', 'invite'):
                item['amount'] = integer(p.get('amount'), '每人奖励数量', 1, 100000 if kind == 'points' else 100)
            elif kind == 'shop':
                item_id = integer(p.get('item_id'), '商品', 1, 2147483647)
                spec = self.shop.get(item_id, include_private=True)
                if not spec or not spec['enabled']:
                    raise ValueError('商城商品不存在或已下架')
                validate_bag_spec(spec)
                item.update(item_id=item_id, spec=spec, name=spec['name'])
            elif kind == 'registration':
                if entry_fee:
                    raise ValueError('收费活动不能配置本人专属注册资格奖；请改为免费活动')
                if gated(limits):
                    raise ValueError('设置参与门槛的活动不能配置本人专属注册资格奖')
                group = str(p.get('group_id') or '')
                found = self.groups.get(group)
                if not found:
                    raise ValueError('注册资格必须选择现有账号分组')
                if audience == 'member':
                    raise ValueError('仅已注册活动不能配置注册资格')
                item.update(group_id=group, group_name=found['name'],
                            days=integer(p.get('days'), '注册有效天数', 1, 3650))
            elif kind == 'manual':
                item['instructions'] = text(p.get('instructions'), '私密领取说明', 2000)
            else:
                raise ValueError('奖品类型无效')
            clean.append(item)
        if sum(p['quantity'] for p in clean) > 200:
            raise ValueError('每期最多 200 个获奖名额')
        return {'title': title, 'audience': audience, 'chat_id': chat, 'thread_id': thread,
                'deadline': deadline, 'draw_mode': mode, 'max_entries': max_entries,
                'prizes': clean, 'anti_multi': anti_multi, 'entry_fee': entry_fee, 'require_private_start': True,
                'require_captcha': True,
                **limits, **switches, 'device_check': lottery_device.flag(raw),
                'public_rules_version': PUBLIC_RULES_VERSION, 'eligibility_version': 1 if legacy else ELIGIBILITY_VERSION}

    def save(self, raw, actor, lottery_id=None, revision=None):
        config = self.validate(raw)
        now = int(time.time())
        with economy_write(self.db) as conn:
            if lottery_id:
                stored = conn.execute("SELECT config_json FROM lotteries WHERE id=? AND state='draft' AND revision=?",
                                      (lottery_id, revision)).fetchone()
                if not stored:
                    raise ValueError('草稿已变更或已发布，请刷新；发布后配置冻结')
                # Preserve server-stored policies under the edit's write lock
                # and revision, never a client's claimed version/legacy marker.
                stored = json.loads(stored['config_json'])
                config['device_check'] = lottery_device.flag(raw, stored=stored)
                config['public_rules_version'] = public_rules_version(stored)
                changed = conn.execute('UPDATE lotteries SET config_json=?,revision=revision+1 '
                                       'WHERE id=? AND state=\'draft\' AND revision=?',
                                       (encode(config), lottery_id, revision)).rowcount
                if not changed:
                    raise ValueError('草稿已变更或已发布，请刷新；发布后配置冻结')
            else:
                lottery_id = secrets.token_hex(12)
                conn.execute('INSERT INTO lotteries(id,config_json,created_by,created_at) VALUES(?,?,?,?)',
                             (lottery_id, encode(config), actor, now))
            self.audit(conn, actor, 'save', lottery_id)
        return lottery_id

    def copy(self, lottery_id, actor):
        """New draft from any activity's configuration; the deadline must be re-entered.

        Prize pool, audience, fee, thresholds and switches are copied as the
        source's effective values (a legacy source's missing switches = off).
        Retired environment checks and old public wording are never copied:
        this is a NEW activity.
        """
        source = self.row(lottery_id)['config']
        raw = {k: source.get(k) for k in ('title', 'audience', 'chat_id', 'thread_id', 'draw_mode', 'max_entries', 'anti_multi',
                                          'entry_fee', 'min_level', 'min_points', 'min_watch_hours', 'reminder', 'pin')}
        # Publication may have normalised an @handle to the numeric chat id;
        # a group that is no longer allowlisted must be chosen again.
        if raw['chat_id'] not in self.bot._group_allowlist():
            raw['chat_id'] = ''
        # Shop prizes are re-read and re-validated as current products, not
        # the old frozen snapshot; other prize fields are validated again too.
        raw['prizes'] = [{k: v for k, v in p.items() if k not in ('spec', 'group_name')} for p in source['prizes']]
        raw['deadline'] = None
        config = self.validate(raw, incomplete=True)
        new_id = secrets.token_hex(12)
        with economy_write(self.db) as conn:
            conn.execute('INSERT INTO lotteries(id,config_json,created_by,created_at) VALUES(?,?,?,?)',
                         (new_id, encode(config), actor, int(time.time())))
            self.audit(conn, actor, 'copy', new_id, f'from={lottery_id}')
        return new_id

    def delete(self, lottery_id, actor, confirm=False):
        if confirm is not True:
            raise ValueError('请确认删除草稿（confirm 须为 true）')
        with economy_write(self.db) as conn:
            row = conn.execute('SELECT state,config_json FROM lotteries WHERE id=?', (lottery_id,)).fetchone()
            if not row:
                raise KeyError('活动不存在')
            if row['state'] != 'draft':
                raise ValueError('只有草稿可以删除；已发布且未开奖的活动请使用取消')
            conn.execute("DELETE FROM lotteries WHERE id=? AND state='draft'", (lottery_id,))
            self.audit(conn, actor, 'delete', lottery_id, json.loads(row['config_json'])['title'][:60])

    def cancel(self, lottery_id, actor, confirm=False):
        """Published, unsettled only. Same write lock as entry and draw: exactly one wins.

        Paid entries are refunded the amount actually debited, once, keyed by
        activity + TG. A repeated or concurrent cancel is a no-op.
        """
        if confirm is not True:
            raise ValueError('请确认取消：取消后不再报名和开奖，收费活动退还报名积分（confirm 须为 true）')
        now = int(time.time())
        with economy_write(self.db) as conn:
            row = conn.execute('SELECT * FROM lotteries WHERE id=?', (lottery_id,)).fetchone()
            if not row:
                raise KeyError('活动不存在')
            if row['state'] == 'cancelled':
                return False  # repeated / concurrent cancel: nothing refunded twice
            if row['state'] == 'settled':
                raise ValueError('已开奖的活动不能取消')
            if row['state'] != 'published':
                raise ValueError('草稿尚未发布，请直接删除草稿')
            conn.execute("UPDATE lotteries SET state='cancelled',cancelled_at=? WHERE id=? AND state='published'",
                         (now, lottery_id))
            entrants = {r['tg_id'] for r in conn.execute('SELECT tg_id FROM lottery_entries WHERE lottery_id=?',
                                                         (lottery_id,)).fetchall()}
            prefix = f'lottery.entry:{lottery_id}:'
            refunded = {r['ref'] for r in conn.execute(
                "SELECT ref FROM points_ledger WHERE reason='lottery.refund' AND ref LIKE ?",
                (f'lottery.refund:{lottery_id}:%',)).fetchall()}
            count = total = 0
            for paid in conn.execute("SELECT emby_user_id,delta,ref FROM points_ledger WHERE reason='lottery.entry' "
                                     'AND ref LIKE ? ORDER BY id', (prefix + '%',)).fetchall():
                tg_id = paid['ref'][len(prefix):]
                ref = f'lottery.refund:{lottery_id}:{tg_id}'
                if tg_id not in entrants or paid['delta'] >= 0 or ref in refunded:
                    continue
                self.points._apply(conn, paid['emby_user_id'], -paid['delta'], 'lottery.refund', ref, str(actor), now)
                refunded.add(ref)
                count, total = count + 1, total - paid['delta']
            launch = conn.execute("SELECT state FROM lottery_notices WHERE lottery_id=? AND kind='launch'",
                                  (lottery_id,)).fetchone()
            if launch and launch['state'] in ('sent', 'unknown', 'sending'):
                # Only when the group may have seen the launch; reply target is
                # resolved at send time from the then-known launch message.
                current = {**dict(row), 'config': json.loads(row['config_json'])}
                self.stage_notice(conn, current, 'cancel')
            self.audit(conn, actor, 'cancel', lottery_id, f'refunds={count} points={total}')
        return True

    @staticmethod
    def audit(conn, actor, action, subject, detail=''):
        conn.execute('INSERT INTO audit_log(ts,actor,action,subject,detail,ok) VALUES(?,?,?,?,?,1)',
                     (int(time.time()), str(actor), 'lottery.' + action, str(subject), detail))

    def row(self, lottery_id):
        row = self.db.one('SELECT * FROM lotteries WHERE id=?', (lottery_id,))
        if not row:
            raise KeyError('活动不存在')
        row['config'] = json.loads(row.pop('config_json'))
        row['config'].setdefault('entry_fee', 0)
        row['config'].setdefault('max_entries', 0)
        row['config'].setdefault('require_private_start', False)
        row['config'].setdefault('require_captcha', False)
        row['config']['eligibility_version'] = eligibility_version(row['config'])
        for key, value in LEGACY_DEFAULTS.items():
            row['config'].setdefault(key, value)
        return row

    def view(self, lottery_id, tg_id, admin=False):
        row = self.row(lottery_id)
        if row['state'] == 'draft' and not admin:
            raise KeyError('活动尚未发布')
        config = copy.deepcopy(row['config'])
        config['prizes'] = [public_prize(p) for p in config['prizes']]
        if eligibility_version(config) == ELIGIBILITY_VERSION:
            for p in config['prizes']:
                p['audience'] = prize_audience(p)
        for key in INTERNAL if not admin else ('chat_id', 'thread_id'):
            config.pop(key, None)
        data = {k: row[k] for k in ('id', 'state', 'revision', 'published_at', 'settled_at', 'cancelled_at')}
        mine = self.db.one('SELECT excluded,joined_at FROM lottery_entries WHERE lottery_id=? AND tg_id=?',
                           (lottery_id, tg_id))
        # Identical public rules for everybody, administrators included: the
        # association explanation is a separate administrator-only field.
        wording = ELIGIBILITY_RULES if eligibility_version(config) == ELIGIBILITY_VERSION else RULES
        if config['draw_mode'] == 'count':
            wording = (f'满{config["max_entries"]}人自动开奖，管理员也可提前开奖。' + wording.replace(
                '定时开奖截止后不能报名；手动开奖不设截止时间，开奖前均可报名。', '满员或开奖后停止报名。'))
        if public_rules_version(row['config']) == 1:
            wording += FAIRNESS
        if config.get('require_private_start'):
            wording += '报名须先私聊启动 Bot。'
        rules = entry_terms(config) + requirement_terms(config) + wording
        registered = bool(self.db.one('SELECT 1 FROM members WHERE tg_user_id=?', (str(tg_id),)))
        data['prize_eligible'], data['prize_eligibility_reason'] = prize_eligibility(row['config'], registered)
        private_ready = telegram_private.ready(self.db, telegram_private.identity(self.bot), str(tg_id))
        private_required = bool(config.get('require_private_start') and not mine)
        username = self.bot._bot_username or row['bot_username']
        data.update(bot_private_ready=private_ready, bot_private_required=private_required,
                    bot_private_reason=telegram_private.REASON if private_required and not private_ready else '',
                    bot_start_url=f'https://t.me/{username}?start=lottery_{lottery_id}'
                    if re.fullmatch(r'[A-Za-z0-9_]{5,32}', username) else '')
        if admin:
            data['admin_rules'] = lottery_identity.RULE if config['anti_multi'] else lottery_identity.RULE_OFF
        data.update(config=config, rules=rules, count=self.db.one(
            'SELECT COUNT(*) AS n FROM lottery_entries WHERE lottery_id=?', (lottery_id,))['n'],
            joined=bool(mine), winners=[], my_award=None)
        data['captcha'] = lottery_captcha.public(self.bot, config.get('require_captcha') and not mine
            and row['state'] == 'published' and accepting(config, int(time.time()))
            and not (config['draw_mode'] == 'count' and data['count'] >= config.get('max_entries', 0)))
        for award in self.db.query('SELECT * FROM lottery_awards WHERE lottery_id=? ORDER BY id', (lottery_id,)):
            prize = json.loads(award['prize_json'])
            data['winners'].append({'label': award['label'], 'prize': public_prize(prize)})
            if admin or award['tg_id'] == tg_id:
                private = {k: award[k] for k in ('id', 'label', 'state', 'error', 'delivered_at')}
                private['prize'] = public_prize(prize)
                if prize['kind'] == 'manual':
                    private['instructions'] = prize['instructions']
                private['receipt'] = json.loads(award['receipt_json'])
                if prize['kind'] == 'registration':
                    admission = self.db.one('SELECT code,used_at FROM lottery_admissions WHERE award_id=?', (award['id'],))
                    if admission and not admission['used_at']:
                        private['registration_url'] = f'https://t.me/{row["bot_username"]}?start={admission["code"]}'
                    private['consumed'] = bool(admission and admission['used_at'])
                if award['tg_id'] == tg_id:
                    data['my_award'] = private
                if admin:
                    # Administrator-only private message receipt (null: old activity / not staged).
                    private = {**private, 'dm': self.db.one(
                        'SELECT state,error,updated_at FROM lottery_dms WHERE award_id=?', (award['id'],))}
                    data.setdefault('awards', []).append(private)
        if admin:
            data['qualification'] = {'excluded': bool(mine and mine['excluded'])}
            data['excluded_count'] = self.db.one('SELECT COUNT(*) AS n FROM lottery_entries WHERE lottery_id=? AND excluded=1', (lottery_id,))['n']
            data['candidate_count'] = data['count'] - data['excluded_count']
            notices = self.db.query(
                'SELECT id,kind,state,message_id,attempts,error FROM lottery_notices WHERE lottery_id=? ORDER BY id',
                (lottery_id,))
            result_parts = [n for n in notices if n['kind'] == 'result' or n['kind'].startswith('result:')]
            if len(result_parts) > 1:
                for index, notice in enumerate(result_parts, start=1):
                    notice.update(kind='result', page=index, pages=len(result_parts))
            admin_config = row['config']
            if row['state'] == 'draft':
                # Frozen ON only at publication (see publish); not draft settings.
                admin_config = {k: v for k, v in admin_config.items() if k not in ('winner_dm', 'live_count')}
            data.update(config=admin_config, notices=notices, last_error=row['last_error'],
                        operations=self.operations(row, notices))
            if eligibility_version(admin_config) == ELIGIBILITY_VERSION:
                data['config'] = {**admin_config, 'prizes': [{**p, 'audience': prize_audience(p)} for p in admin_config['prizes']]}
            if row['state'] == 'cancelled':
                data['refunds'] = self.db.one("SELECT COUNT(*) AS count,COALESCE(SUM(delta),0) AS points FROM points_ledger "
                                              "WHERE reason='lottery.refund' AND ref LIKE ?",
                                              (f'lottery.refund:{lottery_id}:%',))
        return lottery_device.present(self.db, row, data, tg_id, admin)

    def ongoing(self, tg_id, now=None):
        """Member-home summary of activities open for entry right now.

        Group scoping uses local evidence only, never live Telegram lookups:
        groups where this Telegram account was already admitted (membership
        verified at entry) to an earlier lottery. Without such evidence every
        open activity is listed; joining still verifies membership. No chat
        IDs, thresholds of others or association data are returned.
        """
        now = int(time.time()) if now is None else now
        tg_id = str(tg_id)
        joined, known = set(), set()
        for r in self.db.query("SELECT e.lottery_id,l.config_json FROM lottery_entries e JOIN lotteries l "
                               "ON l.id=e.lottery_id WHERE e.tg_id=? AND l.state<>'draft'", (tg_id,)):
            joined.add(r['lottery_id'])
            known.add(str(json.loads(r['config_json']).get('chat_id') or ''))
        items = []
        for r in self.db.query("SELECT id,config_json,(SELECT COUNT(*) FROM lottery_entries e WHERE e.lottery_id=l.id) AS count "
                               "FROM lotteries l WHERE state='published' ORDER BY id"):
            c = json.loads(r['config_json'])
            if not accepting(c, now) or (known and str(c.get('chat_id') or '') not in known and r['id'] not in joined):
                continue
            if c['draw_mode'] == 'count' and r['count'] >= c['max_entries']:
                continue
            prizes = [{'name': p['name'], 'kind': p['kind'], 'quantity': p['quantity']} for p in c['prizes']]
            summary = '、'.join(f'{p["name"]} ×{p["quantity"]}' for p in prizes[:3])
            items.append({'id': r['id'], 'title': c['title'], 'deadline': c['deadline'], 'audience': c['audience'],
                          'entry_fee': c.get('entry_fee', 0), 'joined': r['id'] in joined, 'prizes': prizes,
                          'draw_mode': c['draw_mode'], 'max_entries': c.get('max_entries', 0), 'count': r['count'],
                          'prize_summary': summary + (f' 等 {len(prizes)} 项' if len(prizes) > 3 else ''),
                          'slots': sum(p['quantity'] for p in prizes)})
        items.sort(key=lambda item: (item['deadline'] is None, item['deadline'] or 0, item['id']))
        return {'items': items[:20]}

    def operations(self, row, notices):
        """Administrator-only status of reminder / pin / unpin / live count / winner DMs.

        'off' = disabled for this activity (legacy activities: always off),
        'waiting' = not yet due/staged, otherwise the persisted job state.
        """
        c, now, draft = row['config'], int(time.time()), row['state'] == 'draft'
        ops = {r['kind']: r for r in self.db.query(
            'SELECT kind,state,error,updated_at,shown FROM lottery_ops WHERE lottery_id=?', (row['id'],))}

        def job(kind, enabled):
            if not enabled:
                return {'state': 'off', 'error': '', 'updated_at': None}
            found = ops.get(kind)
            if not found:
                return {'state': 'waiting', 'error': '', 'updated_at': None}
            return {'state': found['state'], 'error': found['error'], 'updated_at': found['updated_at'] or None}

        reminder = next(({'state': n['state'], 'error': n['error']} for n in notices if n['kind'] == 'reminder'), None)
        deadline = c.get('deadline')
        if not c['reminder']:
            reminder = {'state': 'off', 'error': ''}
        elif not deadline:
            reminder = {'state': 'off', 'error': '未设截止时间，不发送提醒'}
        elif reminder is None and draft:
            reminder = {'state': 'waiting', 'error': '发布时距截止须不少于 90 分钟才会提醒'}
        elif reminder is None and deadline - (row['published_at'] or 0) < REMINDER_NOTICE:
            reminder = {'state': 'skipped', 'error': '发布时距截止不足 90 分钟，不发送提醒'}
        elif reminder is None and (row['state'] != 'published' or now >= deadline - REMINDER_CUTOFF):
            reminder = {'state': 'skipped', 'error': '未在提醒窗口内发送（活动已结束、发起公告未确认或错过窗口）'}
        elif reminder is None:
            reminder = {'state': 'waiting', 'error': ''}
        if reminder['state'] not in ('off', 'skipped') and deadline:
            reminder['at'] = deadline - REMINDER_LEAD
        # Drafts: live count and winner DMs are frozen ON at publication.
        live_on, dm_on = draft or c['live_count'], draft or c['winner_dm']
        live = job('live', live_on)
        if live_on and ops.get('live'):
            live['shown'] = live_line(ops['live']['shown'], c) if ops['live']['shown'] != 'open:0' else ''
        dms = {r['state']: r['n'] for r in self.db.query(
            'SELECT state,COUNT(*) AS n FROM lottery_dms WHERE lottery_id=? GROUP BY state', (row['id'],))}
        return {'reminder': reminder, 'pin': job('pin', c['pin']), 'unpin': job('unpin', c['pin']),
                'live': live, 'winner_dm': {'enabled': bool(dm_on), 'summary': dms}}

    def records(self, lottery_id, offset=0):
        # One read snapshot for the roster and association explanation.
        with self.db.snapshot():
            row = self.row(lottery_id)
            counts = self.db.one('SELECT COUNT(*) AS total,COALESCE(SUM(excluded),0) AS excluded '
                                 'FROM lottery_entries WHERE lottery_id=?', (lottery_id,))
            entries = self.db.query('SELECT e.tg_id,e.label,e.joined_at,e.registered,e.excluded,'
                                    'a.id AS award_id,a.state AS award_state FROM lottery_entries e '
                                    'LEFT JOIN lottery_awards a ON a.lottery_id=e.lottery_id AND a.tg_id=e.tg_id '
                                    'WHERE e.lottery_id=? ORDER BY e.joined_at,e.tg_id LIMIT 51 OFFSET ?',
                                    (lottery_id, max(0, offset)))
            for entry in entries:
                entry['outcome'] = ('excluded' if entry['excluded'] else 'winner' if entry['award_id']
                                    else 'not_selected' if row['state'] == 'settled'
                                    else 'cancelled' if row['state'] == 'cancelled' else 'eligible')
            return lottery_device.annotate_records(self.db, row, {
                **counts, 'state': row['state'], 'anti_multi': row['config']['anti_multi'],
                'entries': entries[:50], 'more': len(entries) > 50,
                'clusters': lottery_identity.clusters(self.db, lottery_id)})

    async def ready(self, config):
        if not self.bot._cfg().get('enabled'):
            raise ValueError('请先启用 Bot，再发布抽奖')
        me = await self.bot._call('getMe')
        if not isinstance(me, dict) or not re.fullmatch(r'[A-Za-z0-9_]{5,32}', str(me.get('username') or '')):
            raise ValueError('无法确认 Bot 身份，请稍后重试')
        if not me.get('has_main_web_app'):
            raise ValueError('请先在 BotFather 配置 Main Mini App，再发布群深链')
        chat = await self.bot._call('getChat', {'chat_id': config['chat_id']})
        if not isinstance(chat, dict) or chat.get('type') not in ('group', 'supergroup'):
            raise ValueError('无法确认目标群，请检查 Bot 群权限')
        if not self.bot._group_chat_allowed(chat):
            raise ValueError('目标群已不在白名单')
        if config['thread_id'] and not chat.get('is_forum'):
            raise ValueError('该群不是话题群，不能指定话题 ID')
        member = await self.bot._call('getChatMember', {'chat_id': chat['id'], 'user_id': me['id']})
        if not member or member.get('status') not in ('administrator', 'creator'):
            raise ValueError('请将 Bot 设为群管理员，以可靠查询报名者群成员身份')
        return me['username'], str(chat['id'])

    def notice_payload(self, row, kind, winners=None, count=0):
        c = row['config']
        title = '<b>' + escape(c['title']) + '</b>'
        when = datetime.fromtimestamp(c['deadline'], BEIJING).strftime('%m-%d %H:%M') if c.get('deadline') else ''
        # Sections are separated by one blank line; the launch title line stays
        # first and alone because the live entry count is appended to it.
        if kind == 'launch':
            timing = (f'{when} 截止 · ' + ('到点自动开奖' if c['draw_mode'] == 'timed' else '管理员开奖')
                      if when else '管理员开奖 · 开奖前均可报名')
            if c['draw_mode'] == 'count':
                timing = f'满{c["max_entries"]}人自动开奖'
            who = [AUDIENCES[c['audience']] + '可参与', *requirement_parts(c)]
            if c.get('min_points'):
                who.append('积分只校验不扣')
            if (gated(c) or c.get('entry_fee')) and c['audience'] != 'member':
                who.append('须已注册')
            terms = [f'{emoji("clock")} {timing}', f'{emoji("shield")} ' + ' · '.join(who)]
            if c.get('entry_fee'):
                terms.append(f'{emoji("redpack")} 报名 {c["entry_fee"]} 积分 · 成功报名不退，取消活动全额退还')
            notes = []
            if c.get('require_private_start'):
                notes.append('<i>报名须先私聊启动 Bot</i>')
            if c.get('entry_fee') and public_rules_version(c) == 1:
                notes.append(f'<i>{emoji("warn")} {FAIRNESS}</i>')
            if eligibility_version(c) == ELIGIBILITY_VERSION:
                notes.append('<i>奖品资格以开奖时的注册状态为准</i>')
            elif any(p['kind'] == 'registration' for p in c['prizes']):
                notes.append('<i>注册资格奖仅限开奖时未注册者本人领取</i>')
            sections = [[f'{emoji("present")} {title}'],
                        [f'{emoji("prize" + str(i % 3))} {escape(p["name"])} × {p["quantity"]}'
                         + (f' · {AUDIENCES[prize_audience(p)] if p["kind"] != "manual" else "通用"}'
                            if eligibility_version(c) == ELIGIBILITY_VERSION else '')
                         for i, p in enumerate(c['prizes'])], terms, notes]
        elif kind == 'reminder':
            sections = [[f'{emoji("reaper")} {title} 即将截止'],
                        [f'{emoji("clock")} {when} 截止 · 已有 {number(count)} 人报名', '还没报名的抓紧上车']]
        elif kind == 'cancel':
            sections = [[f'{emoji("warn")} {title} 已取消'],
                        ['本期不再报名和开奖' + ('，报名积分已全额退还' if c.get('entry_fee') else '')]]
        else:
            head = f'{emoji("party")} {title} 开奖啦'
            lines = [f'{emoji("crown")} {winner_mention(w)} · {escape(json.loads(w["prize_json"])["name"])}'
                     for w in winners or []]
            sections = [[head], lines or ['本期暂无中奖者']]
        text = '\n\n'.join('\n'.join(section) for section in sections if section)
        payload = {'chat_id': c['chat_id'], 'text': text, 'parse_mode': 'HTML'}
        if kind != 'cancel':
            payload['reply_markup'] = {'inline_keyboard': [[{'text': '查看结果' if kind == 'result' else '立即参与',
                                       'icon_custom_emoji_id': EMOJI['check' if kind == 'result' else 'gift'][0],
                                       'url': f'https://t.me/{row["bot_username"]}?startapp=lottery_{row["id"]}'}]]}
        if c['thread_id']:
            payload['message_thread_id'] = c['thread_id']
        return payload

    def result_payloads(self, row, winners):
        """Full winner coverage in whole lines; each page is a durable notice."""
        full = self.notice_payload(row, 'result', winners)
        if message_length(full['text']) <= 4096:
            return [full]
        # At most one page per winner. Reserve the widest page label before
        # packing, and add real page numbers only after all pages are known.
        reserve = len(f'（{len(winners)}/{len(winners)}）')
        groups, page = [], []
        for winner in winners:
            candidate = self.notice_payload(row, 'result', [*page, winner])
            if page and message_length(candidate['text']) + reserve > 4096:
                groups.append(page)
                page = []
            page.append(winner)
        groups.append(page)
        result = []
        for index, group in enumerate(groups, start=1):
            payload = self.notice_payload(row, 'result', group)
            head, sep, rest = payload['text'].partition('\n')
            payload['text'] = f'{head}（{index}/{len(groups)}）{sep}{rest}'
            if message_length(payload['text']) > 4096:
                raise ValueError('单条中奖信息过长，无法生成完整公告')
            result.append(payload)
        return result

    @staticmethod
    def dm_payload(row, award):
        """Short winner DM: activity, prize, original deep link. Never private claim text."""
        prize = json.loads(award['prize_json'])
        tg_id = str(award['tg_id'])
        text = '\n\n'.join([f'{emoji("party")} <b>恭喜中奖！</b>',
                            f'{emoji("present")} {escape(prize["name"])}\n来自「{escape(row["config"]["title"])}」',
                            '点下方按钮查看并领取'])
        return {'chat_id': int(tg_id) if tg_id.isdigit() else tg_id, 'text': text, 'parse_mode': 'HTML',
                'reply_markup': {'inline_keyboard': [[{'text': '查看并领取', 'icon_custom_emoji_id': EMOJI['check'][0],
                                                       'url': deep_link(row)}]]}}

    def stage_notice(self, conn, row, kind, winners=None):
        if conn.execute('SELECT 1 FROM lottery_notices WHERE lottery_id=? AND kind=?', (row['id'], kind)).fetchone():
            return  # Never regenerate or extend an old frozen notice.
        count = conn.execute('SELECT COUNT(*) FROM lottery_entries WHERE lottery_id=?', (row['id'],)).fetchone()[0]
        payloads = (self.result_payloads(row, winners or []) if kind == 'result'
                    else [self.notice_payload(row, kind, winners, count)])
        for index, payload in enumerate(payloads, start=1):
            part = kind if index == 1 else f'{kind}:{index}'
            conn.execute('INSERT OR IGNORE INTO lottery_notices(lottery_id,kind,payload_json,updated_at) VALUES(?,?,?,?)',
                         (row['id'], part, encode(payload), int(time.time())))

    async def publish(self, lottery_id, revision, actor):
        row = self.row(lottery_id)
        if row['state'] != 'draft':
            return  # duplicate request must not create a second launch
        if type(revision) is not int or revision != row['revision']:
            raise ValueError('草稿版本已变化，请重新预览')
        # Validate, but publish the stored configuration: the new-draft default
        # must not replace a legacy draft's stored device/public/eligibility policies.
        legacy = eligibility_version(row['config']) == 1
        checked = self.validate(row['config'], legacy=legacy)
        if not legacy and any(row['config'].get(k) != checked[k] for k in
                              ('deadline', 'reminder', 'entry_fee', 'min_level', 'min_points', 'min_watch_hours', 'max_entries')):
            raise ValueError('草稿参与条件已变化，请保存草稿后重新发布')
        username, chat_id = await self.ready(row['config'])
        with economy_write(self.db) as conn:
            if not accepting(row['config'], int(time.time())):
                raise ValueError('报名截止时间已过，请修改草稿后发布')
            stored = conn.execute('SELECT config_json FROM lotteries WHERE id=?', (lottery_id,)).fetchone()
            if stored:
                for key in ('max_entries', 'require_private_start'):
                    if key not in json.loads(stored['config_json']):
                        row['config'].pop(key, None)  # do not backfill an unedited old draft
            row['config']['chat_id'] = chat_id
            # Every future publication is protected, including an unedited old
            # draft; other historical participation policies remain frozen.
            row['config']['require_captcha'] = True
            # Only activities published from now on get winner DMs and the live
            # entry count; legacy published rows keep their stored (absent = off) config.
            row['config'].update(winner_dm=True, live_count=True)
            changed = conn.execute('UPDATE lotteries SET state=\'published\',published_at=?,bot_username=?,config_json=? '
                                   'WHERE id=? AND state=\'draft\' AND revision=?',
                                   (int(time.time()), username, encode(row['config']), lottery_id, revision)).rowcount
            if not changed:
                raise ValueError('草稿版本已变化，请重新预览')
            row['bot_username'] = username
            self.stage_notice(conn, row, 'launch')
            self.audit(conn, actor, 'publish', lottery_id)
        await self.deliver_notices(lottery_id)
        await self.deliver_ops(lottery_id)

    def threshold(self, config, tg_id, now=None):
        """Read-only participation-threshold verdict, computed before the write lock.

        Returns (uid, error). Level = current Lv number of the existing level
        system; watch time = the member-facing recorded 30-day window.
        """
        if not gated(config):
            return None, None
        member = self.members.find_by_telegram(tg_id)
        if not member:
            return None, '本期设有参与门槛，须绑定已注册账户后报名'
        uid = str(member['emby_user_id'])
        need = config.get('min_level')
        if need:
            level = (member.get('activity_level') or {}).get('level')
            if not level:
                return uid, f'需要 Lv{need} 及以上（当前无法确认等级）'
            if level < need:
                return uid, f'需要 Lv{need} 及以上（当前 Lv{level}）'
        hours = config.get('min_watch_hours')
        if hours:
            seconds = watch_seconds(self.db, uid, int(time.time()) if now is None else now)
            if seconds < hours * 3600:
                return uid, f'需要近 30 天观看满 {hours} 小时（当前约 {int(seconds // 360) / 10:g} 小时）'
        return uid, None

    def captcha_precheck(self, row, tg_id):
        """Read-only public eligibility; never expose risk/exclusion verdicts."""
        c = row['config']
        if not self.bot._cfg().get('enabled'):
            raise ValueError('Bot 暂未启用，无法确认群成员身份')
        if row['state'] != 'published' or not accepting(c, int(time.time())):
            raise ValueError('报名已截止或活动尚未发布')
        if (c['draw_mode'] == 'count' and self.db.one('SELECT COUNT(*) AS n FROM lottery_entries WHERE lottery_id=?',
                                                    (row['id'],))['n'] >= c.get('max_entries', 0)):
            raise ValueError('报名人数已满，等待开奖')
        if c.get('require_private_start') and not telegram_private.ready(self.db, telegram_private.identity(self.bot), tg_id):
            raise ValueError(telegram_private.REASON)
        account = self.db.one('SELECT emby_user_id FROM members WHERE tg_user_id=?', (tg_id,))
        if c['audience'] != 'all' and (c['audience'] == 'member') != bool(account):
            raise ValueError('你的注册状态不符合本期参与对象')
        allowed, reason = prize_eligibility(c, bool(account))
        if not allowed:
            raise ValueError(reason)
        _, reason = self.threshold(c, tg_id)
        if reason:
            raise ValueError(reason)
        if account:
            balance = self.points.balance(account['emby_user_id'])
            if c.get('min_points', 0) > balance:
                raise ValueError(f'需要积分余额不少于 {c["min_points"]}（当前 {balance}，只校验不扣）')
            if c.get('entry_fee', 0) > balance:
                raise ValueError('积分不足')
        elif c.get('entry_fee'):
            raise ValueError('收费活动须绑定已注册积分账户后报名')

    def captcha_challenge(self, lottery_id, tg, *, source_ip=None):
        row, tg_id = self.row(lottery_id), str(tg['id'])
        if (not row['config'].get('require_captcha') or self.db.one(
                'SELECT 1 FROM lottery_entries WHERE lottery_id=? AND tg_id=?', (lottery_id, tg_id))):
            raise ValueError('本次报名无需验证，请刷新活动')
        lottery_captcha.throttle(self.db, 'challenge', lottery_id, tg_id, source_ip)
        self.captcha_precheck(row, tg_id)
        return lottery_captcha.issue(self.db, self.bot, lottery_id, tg_id, source_ip)

    async def join(self, lottery_id, tg, *, source_ip=None, browser_id=None, expected_entry_fee=ENTRY_FEE_UNSET,
                   consent_version=None, device=None, captcha_token=None, captcha_challenge_id=None):
        row = self.row(lottery_id)
        tg_id = str(tg['id'])
        if (row['state'] in ('published', 'settled') and self.db.one(
                'SELECT 1 FROM lottery_entries WHERE lottery_id=? AND tg_id=?', (lottery_id, tg_id))):
            return  # receipt survives expired tokens/deadlines, settlement and configuration outages
        if not self.bot._cfg().get('enabled'):
            raise ValueError('Bot 暂未启用，无法确认群成员身份')
        if row['state'] == 'cancelled':
            raise ValueError('活动已取消，不能报名')
        if row['state'] != 'published' or not accepting(row['config'], int(time.time())):
            raise ValueError('报名已截止或活动尚未发布')
        if row['config'].get('require_captcha'):
            lottery_captcha.throttle(self.db, 'join', lottery_id, tg_id, source_ip)
        # No unavailable-check fail-open, including Telegram's restricted status.
        member = await self.bot._call('getChatMember', {'chat_id': row['config']['chat_id'], 'user_id': tg['id']})
        if not isinstance(member, dict):
            raise ValueError('无法确认群成员身份，请稍后重试（尚未报名）')  # noqa: TRY004
        if not (member.get('status') in ('member', 'administrator', 'creator') or
                (member.get('status') == 'restricted' and member.get('is_member') is True)):
            raise ValueError('仅发布群的群成员可报名')
        gate_uid, gate_error = self.threshold(row['config'], tg_id)
        proof = None
        if row['config'].get('require_captcha'):
            try:
                proof = await lottery_captcha.verify(self.db, self.bot, lottery_id, tg_id,
                                                      captcha_challenge_id, captcha_token, source_ip)
            except lottery_captcha.CaptchaError:
                # Another legitimate request may have completed while verification awaited I/O.
                if self.row(lottery_id)['state'] in ('published', 'settled') and self.db.one(
                        'SELECT 1 FROM lottery_entries WHERE lottery_id=? AND tg_id=?', (lottery_id, tg_id)):
                    return
                raise
        bot_id = telegram_private.identity(self.bot)
        with economy_write(self.db) as conn:
            current = conn.execute('SELECT * FROM lotteries WHERE id=?', (lottery_id,)).fetchone()
            c = json.loads(current['config_json'])
            now = int(time.time())
            if current['state'] == 'cancelled':
                raise ValueError('活动已取消，不能报名')
            joined = conn.execute('SELECT 1 FROM lottery_entries WHERE lottery_id=? AND tg_id=?', (lottery_id, tg_id)).fetchone()
            if current['state'] in ('published', 'settled') and joined:
                return
            if current['state'] != 'published' or not accepting(c, now):
                raise ValueError('报名已截止')
            if joined:
                return
            if count_reached(conn, lottery_id, c):
                raise ValueError('报名人数已满，等待开奖')
            if c.get('require_private_start') and not telegram_private.ready(conn, bot_id, tg_id):
                raise ValueError(telegram_private.REASON)
            # Consent + usable device data before any debit; never decoded without consent.
            consented = lottery_device.admit(conn, c, consent_version, device)
            account = conn.execute('SELECT emby_user_id FROM members WHERE tg_user_id=?', (tg_id,)).fetchone()
            registered = bool(account)
            if c['audience'] != 'all' and (c['audience'] == 'member') != registered:
                raise ValueError('你的注册状态不符合本期参与对象')
            allowed, reason = prize_eligibility(c, registered)
            if not allowed:
                raise ValueError(reason)
            fee = integer(c.get('entry_fee', 0), '报名积分', 0, 100000)
            if ((expected_entry_fee is not ENTRY_FEE_UNSET or fee) and
                    (type(expected_entry_fee) is not int or expected_entry_fee != fee)):
                raise ValueError('报名积分费用未确认或已变化，请刷新活动后确认报名')
            if gated(c):
                if gate_error:
                    raise ValueError(gate_error)
                if not account or str(account['emby_user_id']) != gate_uid:
                    raise ValueError('账号绑定状态已变化，请刷新后重试')
                if c.get('min_points'):
                    # Checked only, never debited; same lock as every points spender.
                    balance = conn.execute('SELECT COALESCE(SUM(delta),0) FROM points_ledger WHERE emby_user_id=?',
                                           (gate_uid,)).fetchone()[0]
                    if balance < c['min_points']:
                        raise ValueError(f'需要积分余额不少于 {c["min_points"]}（当前 {balance}，只校验不扣）')
            if c.get('require_captcha'):
                # No network here. Consumption, debit, entry, risk association and
                # count-mode settlement commit or roll back as one transaction.
                lottery_captcha.consume(conn, self.bot, proof, lottery_id, tg_id, now)
            if fee:
                if not account:
                    raise ValueError('收费活动须绑定已注册积分账户后报名')
                # This write uses the same BEGIN IMMEDIATE boundary as entry /
                # association insertion and all other points spenders. The entry
                # PK is the receipt: retries return above, even after re-binding.
                self.points._apply(conn, account['emby_user_id'], -fee, 'lottery.entry',
                                   f'lottery.entry:{lottery_id}:{tg_id}', tg_id, now)
            label = ' '.join(str(tg.get(k) or '') for k in ('first_name', 'last_name')).strip()
            label = label or ('@' + tg['username'] if tg.get('username') else 'Telegram用户')
            conn.execute('INSERT INTO lottery_entries(lottery_id,tg_id,label,joined_at,registered) VALUES(?,?,?,?,?)',
                         (lottery_id, tg_id, label[:40], now, int(registered)))
            if c['anti_multi']:
                lottery_identity.record(conn, lottery_id, tg_id, source_ip,
                                        lottery_device.browser(consented, browser_id), now)
            lottery_device.record(conn, lottery_id, tg_id, consented, now)
            if count_reached(conn, lottery_id, c):
                # Last entry, debit, risk records, winners and durable jobs all
                # commit together. No nested transaction or external request.
                self._settle(conn, current, c, 'system', now)

    def draw(self, lottery_id, actor='system', *, timed=False, counted=False, now=None):
        now = int(time.time()) if now is None else now
        with economy_write(self.db) as conn:
            row = conn.execute('SELECT * FROM lotteries WHERE id=?', (lottery_id,)).fetchone()
            if not row:
                raise KeyError('活动不存在')
            if row['state'] == 'settled':
                return
            if row['state'] == 'cancelled':
                raise ValueError('活动已取消，不能开奖')
            if row['state'] != 'published':
                raise ValueError('活动尚未发布')
            c = json.loads(row['config_json'])
            if timed and (c['draw_mode'] != 'timed' or now < c['deadline']):
                return
            if counted and not count_reached(conn, lottery_id, c):
                return
            self._settle(conn, row, c, actor, now)

    def _settle(self, conn, row, c, actor, now):
        """Settle a published row already checked under this same writer lock.

        Shared by explicit/maintenance draws and the last count-mode entry.
        Only database writes: rewards and Telegram jobs run after commit.
        """
        lottery_id = row['id']
        lottery_device.settle(conn, lottery_id, c, now)  # merged into excluded before candidates are read
        remaining = [dict(r) for r in conn.execute('SELECT * FROM lottery_entries WHERE lottery_id=? AND excluded=0 ORDER BY tg_id', (lottery_id,))]
        registered = {str(r[0]) for r in conn.execute("SELECT tg_user_id FROM members WHERE tg_user_id<>''")}
        rng = secrets.SystemRandom()
        special = [p for p in c['prizes'] if p['kind'] == 'registration']
        normal = [p for p in c['prizes'] if p['kind'] != 'registration']
        rng.shuffle(special)
        rng.shuffle(normal)
        for p in special + normal:
            eligible = [r for r in remaining if eligible_for_prize(c, p, r['tg_id'] in registered)]
            winners = rng.sample(eligible, min(p['quantity'], len(eligible)))
            for w in winners:
                conn.execute('INSERT INTO lottery_awards(lottery_id,tg_id,label,prize_json) VALUES(?,?,?,?)',
                             (lottery_id, w['tg_id'], w['label'], encode(p)))
                remaining.remove(w)
        conn.execute('UPDATE lotteries SET state=\'settled\',settled_at=?,last_error=\'\' WHERE id=?', (now, lottery_id))
        winners = [dict(r) for r in conn.execute('SELECT * FROM lottery_awards WHERE lottery_id=? ORDER BY id', (lottery_id,))]
        current = {**dict(row), 'config': c}
        self.stage_notice(conn, current, 'result', winners)
        if c.get('winner_dm'):
            # Durable DM jobs in the same transaction; delivery is separate
            # and can never change winners or award delivery.
            for w in winners:
                conn.execute('INSERT OR IGNORE INTO lottery_dms(lottery_id,award_id,tg_id,payload_json,updated_at) '
                             'VALUES(?,?,?,?,?)', (lottery_id, w['id'], w['tg_id'],
                                                    encode(self.dm_payload(current, w)), now))
        self.audit(conn, actor, 'draw', lottery_id, f'winners={len(winners)}')

    def deliver_award(self, award_id):
        now = int(time.time())
        with economy_write(self.db) as conn:
            row = conn.execute('SELECT * FROM lottery_awards WHERE id=?', (award_id,)).fetchone()
            if not row or row['state'] == 'delivered':
                return
            p = json.loads(row['prize_json'])
            member = conn.execute('SELECT emby_user_id FROM members WHERE tg_user_id=?', (row['tg_id'],)).fetchone()
            receipt = {}
            if p['kind'] == 'manual':
                conn.execute('UPDATE lottery_awards SET state=\'manual\',error=\'\' WHERE id=?', (award_id,))
                return
            if p['kind'] == 'registration':
                if member:
                    conn.execute('UPDATE lottery_awards SET state=\'blocked\',error=? WHERE id=?',
                                 ('开奖后已通过其他通道注册；资格不可转赠，请联系管理员', award_id))
                    return
                if not self.registration.channel_enabled('admin_grant'):
                    raise ValueError('管理员授权注册通道已关闭；资格保留待恢复')
                if not self.groups.get(p['group_id']):
                    raise ValueError('获奖分组已不存在，请联系管理员恢复分组')
                # Independent rows preserve all older unused admin/gift qualifications.
                if not conn.execute('SELECT 1 FROM lottery_admissions WHERE award_id=?', (award_id,)).fetchone():
                    for _ in range(20):
                        inserted = conn.execute(
                            'INSERT INTO lottery_admissions(code,award_id,tg_id,group_id,days,created_at) '
                            'VALUES(?,?,?,?,?,?) ON CONFLICT(code) DO NOTHING',
                            ('LOT' + generate_code(9), award_id, row['tg_id'], p['group_id'], p['days'], now)).rowcount
                        if inserted:
                            break
                    else:
                        raise ValueError('生成本人注册资格失败，请重试')
            else:
                if not member:
                    conn.execute('UPDATE lottery_awards SET state=\'binding\',error=\'\',next_at=? WHERE id=?', (now + 300, award_id))
                    return
                uid = member['emby_user_id']
                source = f'lottery:{award_id}'
                if p['kind'] == 'points':
                    receipt['balance'] = self.points._apply(conn, uid, p['amount'], 'lottery.award', source, 'system', now)
                elif p['kind'] == 'invite':
                    conn.execute('UPDATE members SET invite_quota=COALESCE(invite_quota,0)+?,updated_at=? WHERE emby_user_id=?',
                                 (p['amount'], now, uid))
                    receipt['invite_quota_added'] = p['amount']
                elif p['kind'] == 'shop':
                    receipt['card_id'] = InventoryService.add(conn, uid, p['spec'], source, now)
                else:
                    raise ValueError('未知奖励类型')
            conn.execute('UPDATE lottery_awards SET state=\'delivered\',user_id=?,delivered_at=?,receipt_json=?,error=\'\' WHERE id=?',
                         (member['emby_user_id'] if member else '', now, encode(receipt), award_id))
            self.audit(conn, 'system', 'award', award_id)

    def deliver_awards(self, lottery_id=None):
        sql = "SELECT id FROM lottery_awards WHERE state IN ('pending','binding','failed')"
        rows = self.db.query(sql + (' AND lottery_id=?' if lottery_id else ' AND next_at<=?') + ' ORDER BY next_at,id LIMIT 200',
                             (lottery_id,) if lottery_id else (int(time.time()),))
        for row in rows:
            try:
                self.deliver_award(row['id'])
            except Exception:  # noqa: BLE001 - durable failure; never lose/re-draw winners
                self.db.execute("UPDATE lottery_awards SET state='failed',error=?,next_at=? WHERE id=? AND state<>'delivered'",
                                ('奖励暂未发放，请检查注册通道/分组或服务状态后重试', int(time.time()) + 300, row['id']))

    def confirm_manual(self, lottery_id, award_id, actor):
        with economy_write(self.db) as conn:
            row = conn.execute('SELECT * FROM lottery_awards WHERE id=? AND lottery_id=?', (award_id, lottery_id)).fetchone()
            if not row or json.loads(row['prize_json'])['kind'] != 'manual':
                raise ValueError('人工奖品不存在')
            if row['state'] != 'delivered':
                conn.execute("UPDATE lottery_awards SET state='delivered',delivered_at=?,error='' WHERE id=?", (int(time.time()), award_id))
                self.audit(conn, actor, 'manual_confirm', award_id)

    async def _send(self, method, payload):
        from app.modules.telegram import _CALL_ERROR
        CALL_DELIVERY.set(None)
        _CALL_ERROR.set('')
        try:
            return await self.bot._call(method, payload)
        except Exception:  # noqa: BLE001 - transport outcome unknown, no replay
            CALL_DELIVERY.set({'state': 'unknown'})
            return None

    @staticmethod
    def _last_failure():
        from app.modules.telegram import _CALL_ERROR
        return CALL_DELIVERY.get() or {}, str(_CALL_ERROR.get() or '').lower()

    async def deliver_notices(self, lottery_id=None):
        now = int(time.time())
        # A crashed sender might have reached Telegram. Never replay an expired lease.
        self.db.execute("UPDATE lottery_notices SET state='unknown',error='发送进程中断，需人工核验' WHERE state='sending' AND updated_at<?", (now - 180,))
        if not self.bot._cfg().get('enabled'):
            self.db.execute("UPDATE lottery_notices SET error='Bot 暂未启用，公告保留等待恢复' WHERE state='pending'")
            return
        # Blocked continuations must not consume the batch and starve other
        # activities. Due pending predecessors may still finish in this batch;
        # the same check is repeated under the claim lock below.
        rows = self.db.query("SELECT n.* FROM lottery_notices n WHERE n.state='pending' AND n.next_at<=? "
                             "AND (n.kind NOT LIKE 'result:%' OR NOT EXISTS (SELECT 1 FROM lottery_notices p "
                             "WHERE p.lottery_id=n.lottery_id AND p.id<n.id AND (p.kind='result' OR p.kind LIKE 'result:%') "
                             "AND (p.state NOT IN ('sent','pending') OR (p.state='pending' AND p.next_at>?))))" +
                             (' AND n.lottery_id=?' if lottery_id else '') + ' ORDER BY n.id LIMIT 20',
                             (now, now, lottery_id) if lottery_id else (now, now))
        for notice in rows:
            launch_deadline = reply_to = None
            with economy_write(self.db) as conn:
                if notice['kind'].startswith('result:') and conn.execute(
                        "SELECT 1 FROM lottery_notices WHERE lottery_id=? AND id<? "
                        "AND (kind='result' OR kind LIKE 'result:%') AND state<>'sent' LIMIT 1",
                        (notice['lottery_id'], notice['id'])).fetchone():
                    continue  # finish/recover an earlier part first; never replay a sent part
                if notice['kind'] == 'launch' and not notice['message_id']:
                    activity = conn.execute('SELECT state,config_json FROM lotteries WHERE id=?',
                                            (notice['lottery_id'],)).fetchone()
                    launch_deadline = json.loads(activity['config_json'])['deadline']
                    if activity['state'] != 'published' or not accepting({'deadline': launch_deadline}, int(time.time())):
                        conn.execute("UPDATE lottery_notices SET state='failed',error=?,next_at=0 WHERE id=? AND state='pending'",
                                     ('活动已取消，发起公告未补发' if activity['state'] == 'cancelled'
                                      else '发起公告报名期已结束，未补发；请管理员人工处理', notice['id']))
                        continue
                if notice['kind'] == 'reminder' and not notice['message_id']:
                    activity = conn.execute('SELECT state,config_json FROM lotteries WHERE id=?',
                                            (notice['lottery_id'],)).fetchone()
                    deadline = json.loads(activity['config_json'])['deadline']
                    # A delayed (Bot paused / rate-limited) reminder is never sent
                    # late, after cancellation, the deadline or the draw.
                    if activity['state'] != 'published' or not deadline or int(time.time()) >= deadline - REMINDER_CUTOFF:
                        conn.execute("UPDATE lottery_notices SET state='skipped',error=?,next_at=0 WHERE id=? AND state='pending'",
                                     ('已错过提醒窗口或活动已结束，未发送', notice['id']))
                        continue
                if notice['kind'] in ('reminder', 'cancel') and not notice['message_id']:
                    # Reply target is the then-known launch message (resolved at send time).
                    launch = conn.execute("SELECT message_id FROM lottery_notices WHERE lottery_id=? AND kind='launch'",
                                          (notice['lottery_id'],)).fetchone()
                    reply_to = launch['message_id'] if launch else None
                changed = conn.execute("UPDATE lottery_notices SET state='sending',attempts=attempts+1,updated_at=? WHERE id=? AND state='pending'",
                                       (now, notice['id'])).rowcount
            if not changed:
                continue
            payload = normalize_notice_payload(json.loads(notice['payload_json']))
            # Repair only the claimed job. Failed/unknown rows remain untouched
            # until an administrator explicitly authorizes recovery.
            self.db.execute('UPDATE lottery_notices SET payload_json=? WHERE id=?', (encode(payload), notice['id']))
            method = 'sendMessage'
            if notice['message_id']:
                payload['message_id'] = notice['message_id']
                payload.pop('message_thread_id', None)
                method = 'editMessageText'
            elif reply_to:
                payload['reply_parameters'] = {'message_id': reply_to, 'allow_sending_without_reply': True}
            result = await self._send(method, payload)
            failure, error = self._last_failure()
            _, allow_fallback = notice_rejection(error, failure)
            expired = launch_deadline is not None and launch_deadline <= int(time.time())
            downgraded = result is None and allow_fallback and has_custom(payload) and not expired
            if downgraded:
                strip_custom(payload)
                # Persist before the retry, so a later known connection failure /
                # rate limit or manual recovery cannot reintroduce the icons.
                saved = json.loads(notice['payload_json'])
                saved['text'] = payload['text']
                if 'reply_markup' in payload:
                    saved['reply_markup'] = payload['reply_markup']
                self.db.execute('UPDATE lottery_notices SET payload_json=? WHERE id=?', (encode(saved), notice['id']))
                result = await self._send(method, payload)
                failure, error = self._last_failure()
            mid = result.get('message_id') if isinstance(result, dict) else notice['message_id']
            state, reason, next_at = 'unknown', '未取得确认，需管理员人工核验', 0
            if (result is not None and mid) or (notice['message_id'] and failure.get('not_modified')):
                state, reason = 'sent', ''
            elif failure.get('state') == 'failed':
                state, reason = 'failed', notice_rejection(error, failure)[0]
                if downgraded:
                    reason = '已移除自定义图标重试一次；' + reason
                elif expired:
                    reason += '；报名期已结束，未补发'
            elif (failure.get('state') == 'retry' and 'timeout' not in error
                  and failure.get('reason') in RETRYABLE):
                state = 'pending' if notice['attempts'] < 3 else 'failed'
                reason = failure['reason']
                next_at = now + max(60 * 2 ** notice['attempts'], failure.get('retry_after', 0))
            # 5xx/timeout or unknown result: even shared report delivery may label
            # it retryable; lottery public announcements intentionally do not.
            self.db.execute('UPDATE lottery_notices SET state=?,message_id=?,error=?,updated_at=?,next_at=? WHERE id=?',
                            (state, mid, reason, int(time.time()), next_at, notice['id']))
            if notice['kind'] == 'launch' and state == 'sent':
                # The (re)confirmed launch text carries no entry-count line yet.
                self.db.execute("UPDATE lottery_ops SET state='idle',shown='open:0',target='',error='',attempts=0,"
                                "next_at=0,updated_at=0,message_id=? WHERE lottery_id=? AND kind='live'",
                                (mid, notice['lottery_id']))

    def recover_notice(self, lottery_id, notice_id, raw, actor):
        with economy_write(self.db) as conn:
            row = conn.execute('SELECT * FROM lottery_notices WHERE id=? AND lottery_id=?', (notice_id, lottery_id)).fetchone()
            if not row or row['state'] not in ('failed', 'unknown'):
                raise ValueError('该公告当前不可恢复，请刷新状态')
            mid = raw.get('message_id') or row['message_id']
            if mid is not None:
                integer(mid, '已核验消息 ID', 1, 2147483647)
            if row['state'] == 'unknown' and not mid and raw.get('confirmed_absent') is not True:
                raise ValueError('发送结果不明：请先核实群中不存在该公告，或提供原消息 ID 以编辑')
            conn.execute("UPDATE lottery_notices SET state='pending',message_id=?,next_at=0,attempts=0,error='' WHERE id=?", (mid, notice_id))
            self.audit(conn, actor, 'notice_recover', notice_id, 'edit' if mid else 'confirmed absent / rejected')

    def stage_reminders(self, now=None):
        """At most one reminder per activity, ~1h before the deadline (UNIQUE kind)."""
        now = int(time.time()) if now is None else now
        rows = self.db.query("SELECT l.id,l.published_at,l.config_json FROM lotteries l JOIN lottery_notices n "
                             "ON n.lottery_id=l.id AND n.kind='launch' WHERE l.state='published' AND n.state='sent' "
                             "AND n.message_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM lottery_notices r "
                             "WHERE r.lottery_id=l.id AND r.kind='reminder')")
        for row in rows:
            c = json.loads(row['config_json'])
            deadline = c.get('deadline')
            if (not c.get('reminder') or not deadline or deadline - (row['published_at'] or 0) < REMINDER_NOTICE
                    or not deadline - REMINDER_LEAD <= now < deadline - REMINDER_CUTOFF):
                continue
            with economy_write(self.db) as conn:
                current = conn.execute('SELECT * FROM lotteries WHERE id=?', (row['id'],)).fetchone()
                if current['state'] == 'published':
                    self.stage_notice(conn, {**dict(current), 'config': json.loads(current['config_json'])}, 'reminder')

    async def deliver_dms(self, lottery_id=None):
        """Winner DMs: unreachable recipients are recorded, never retried; an
        unconfirmed result is never resent. Independent of results and awards."""
        now = int(time.time())
        self.db.execute("UPDATE lottery_dms SET state='unknown',error='发送进程中断，未自动重发' "
                        "WHERE state='sending' AND updated_at<?", (now - LEASE,))
        if not self.bot._cfg().get('enabled'):
            self.db.execute("UPDATE lottery_dms SET error='Bot 暂未启用，私信保留等待恢复' WHERE state='pending'")
            return
        rows = self.db.query("SELECT * FROM lottery_dms WHERE state='pending' AND next_at<=?" +
                             (' AND lottery_id=?' if lottery_id else '') + ' ORDER BY id LIMIT 30',
                             (now, lottery_id) if lottery_id else (now,))
        for dm in rows:
            if not self.db.execute("UPDATE lottery_dms SET state='sending',attempts=attempts+1,updated_at=? "
                                   "WHERE id=? AND state='pending'", (now, dm['id'])):
                continue
            attempts = dm['attempts'] + 1
            payload = json.loads(dm['payload_json'])
            bot_id = telegram_private.identity(self.bot)
            private_marker = telegram_private.marker(self.db, bot_id, dm['tg_id'])
            result = await self._send('sendMessage', payload)
            failure, error = self._last_failure()
            downgraded = result is None and notice_rejection(error, failure)[1] and has_custom(payload)
            if downgraded:
                strip_custom(payload)
                self.db.execute('UPDATE lottery_dms SET payload_json=? WHERE id=?', (encode(payload), dm['id']))
                result = await self._send('sendMessage', payload)
                failure, error = self._last_failure()
            state = outcome(result if isinstance(result, dict) else None, failure, error)
            reason, next_at = '', 0
            if state == 'failed' and failure.get('reason') in UNREACHABLE:
                state, reason = 'unreachable', '无法私信：对方未私聊过 Bot、已屏蔽或已注销；未重试'
                telegram_private.delivery_blocked(self.db, bot_id, dm['tg_id'], private_marker, now)
            elif state == 'failed':
                reason = ('已移除自定义图标重试一次；' if downgraded else '') + notice_rejection(error, failure)[0]
            elif state == 'retry' and attempts >= MAX_ATTEMPTS:
                state, reason = 'failed', failure['reason'] + '；已达自动重试上限'
            elif state == 'retry':
                state, reason, next_at = 'pending', failure['reason'], backoff(attempts, failure, now)
            elif state == 'unknown':
                reason = '未取得确认，未自动重发'
            self.db.execute('UPDATE lottery_dms SET state=?,message_id=?,error=?,updated_at=?,next_at=? WHERE id=?',
                            (state, result.get('message_id') if isinstance(result, dict) else None, reason,
                             int(time.time()), next_at, dm['id']))

    def pinned_by_lottery(self, chat_id, message_id):
        """Whether a group message is a launch notice this feature pinned (service-message cleanup)."""
        return bool(self.db.one("SELECT 1 FROM lottery_ops WHERE kind='pin' AND chat_id=? AND message_id=?",
                                (str(chat_id), message_id)))

    async def deliver_ops(self, lottery_id=None):
        """Silent pin, unpin and live entry-count edits of a confirmed launch notice.

        Only launch notices with a confirmed message_id. Failures are recorded
        for administrators; uncertain outcomes are not repeated; nothing here
        can change entries, results or awards.
        """
        now = int(time.time())
        self.db.execute("UPDATE lottery_ops SET state='unknown',error='处理进程中断，未自动重试' "
                        "WHERE state='sending' AND updated_at<?", (now - LEASE,))
        if not self.bot._cfg().get('enabled'):
            return
        sql = ("SELECT l.id FROM lotteries l JOIN lottery_notices n ON n.lottery_id=l.id AND n.kind='launch' "
               "WHERE n.state='sent' AND n.message_id IS NOT NULL AND (l.state='published' OR "
               "(l.state IN ('settled','cancelled') AND COALESCE(l.settled_at,l.cancelled_at,0)>=?))")
        params = (now - FINISHED_WINDOW,)
        if lottery_id:
            sql, params = sql + ' AND l.id=?', (*params, lottery_id)
        for row in self.db.query(sql + ' ORDER BY l.published_at LIMIT 100', params):
            for kind in ('pin', 'unpin', 'live'):
                job = self._claim_op(row['id'], kind)
                if job:
                    await self._run_op(row['id'], kind, *job)

    def _claim_op(self, lottery_id, kind):
        """Decide and claim one due operation inside the write lock: (method, payload, attempts) or None."""
        now = int(time.time())
        with economy_write(self.db) as conn:
            row = conn.execute('SELECT * FROM lotteries WHERE id=?', (lottery_id,)).fetchone()
            launch = conn.execute("SELECT message_id,payload_json FROM lottery_notices WHERE lottery_id=? AND kind='launch' "
                                  "AND state='sent'", (lottery_id,)).fetchone()
            if not row or not launch or not launch['message_id']:
                return None
            c = {**LEGACY_DEFAULTS, **json.loads(row['config_json'])}
            ops = {r['kind']: dict(r) for r in conn.execute('SELECT * FROM lottery_ops WHERE lottery_id=?', (lottery_id,))}
            op = ops.get(kind)

            def claim(target='', attempts=None):
                attempts = op['attempts'] + 1 if attempts is None else attempts
                conn.execute("UPDATE lottery_ops SET state='sending',attempts=?,target=?,updated_at=?,error='' "
                             'WHERE lottery_id=? AND kind=?', (attempts, target, now, lottery_id, kind))
                return attempts

            def insert(state, chat, mid, shown='', updated=None):
                conn.execute('INSERT INTO lottery_ops(lottery_id,kind,state,chat_id,message_id,shown,updated_at) '
                             'VALUES(?,?,?,?,?,?,?)', (lottery_id, kind, state, chat, mid, shown,
                                                       now if updated is None else updated))
                return dict(conn.execute('SELECT * FROM lottery_ops WHERE lottery_id=? AND kind=?',
                                         (lottery_id, kind)).fetchone())

            if kind == 'pin':
                if not c['pin'] or (op is None and row['state'] != 'published'):
                    return None
                op = op or insert('pending', str(c['chat_id']), launch['message_id'])
                if op['state'] != 'pending' or op['next_at'] > now:
                    return None
                if row['state'] != 'published':
                    conn.execute("UPDATE lottery_ops SET state='skipped',error='活动已结束，未置顶',updated_at=? "
                                 "WHERE lottery_id=? AND kind='pin'", (now, lottery_id))
                    return None
                attempts = claim()
                return 'pinChatMessage', {'chat_id': op['chat_id'], 'message_id': op['message_id'],
                                          'disable_notification': True}, attempts
            if kind == 'unpin':
                pin = ops.get('pin')
                if not c['pin'] or row['state'] not in ('settled', 'cancelled') or not pin or pin['state'] not in ('sent', 'unknown'):
                    return None
                op = op or insert('pending', pin['chat_id'], pin['message_id'])
                if op['state'] != 'pending' or op['next_at'] > now:
                    return None
                attempts = claim()
                return 'unpinChatMessage', {'chat_id': op['chat_id'], 'message_id': op['message_id']}, attempts
            # Live entry count: the public total, same as the ordinary view.
            if not c['live_count']:
                return None
            op = op or insert('idle', str(c['chat_id']), launch['message_id'], 'open:0', 0)
            if op['state'] in ('failed', 'sending', 'skipped') or (op['state'] == 'pending' and op['next_at'] > now):
                return None
            count = conn.execute('SELECT COUNT(*) FROM lottery_entries WHERE lottery_id=?', (lottery_id,)).fetchone()[0]
            phase = ('cancelled' if row['state'] == 'cancelled' else 'settled' if row['state'] == 'settled'
                     else 'open' if accepting(c, now) else 'closed')
            key = f'{phase}:{count}'
            if key == op['shown'] or (op['state'] == 'unknown' and key == op['target']):
                return None  # unchanged, or the identical unconfirmed edit: never repeated
            if phase == 'open' and op['state'] != 'pending' and now - op['updated_at'] < LIVE_INTERVAL:
                return None
            attempts = claim(key, op['attempts'] + 1 if op['state'] == 'pending' and key == op['target'] else 1)
            stored = normalize_notice_payload(json.loads(launch['payload_json']))
            payload = {'chat_id': stored['chat_id'], 'message_id': launch['message_id'], 'parse_mode': 'HTML',
                       'text': with_live(stored['text'], key, c)}
            if stored.get('reply_markup'):
                payload['reply_markup'] = stored['reply_markup']
            return 'editMessageText', payload, attempts

    async def _run_op(self, lottery_id, kind, method, payload, attempts):
        now = int(time.time())
        result = await self._send(method, payload)
        failure, error = self._last_failure()
        state = outcome(result, failure, error)
        action = {'pin': '置顶', 'unpin': '取消置顶', 'live': '编辑发起公告'}[kind]
        reason, next_at = '', 0
        if state == 'failed':
            reason = rejection(error, failure, action)
        elif state == 'retry' and attempts >= MAX_ATTEMPTS:
            state, reason = 'failed', failure['reason'] + '；已达自动重试上限'
        elif state == 'retry':
            state, reason, next_at = 'pending', failure['reason'], backoff(attempts, failure, now)
        elif state == 'unknown':
            reason = rejection(error, failure, action)
        shown = ',shown=target' if state == 'sent' and kind == 'live' else ''
        self.db.execute(f'UPDATE lottery_ops SET state=?,error=?,updated_at=?,next_at=?{shown} '
                        "WHERE lottery_id=? AND kind=? AND state='sending'",
                        (state, reason, int(time.time()), next_at, lottery_id, kind))

    async def tick(self):
        lottery_captcha.prune(self.db)
        for row in self.db.query("SELECT id,config_json FROM lotteries WHERE state='published'"):
            c = json.loads(row['config_json'])
            counted = c['draw_mode'] == 'count'
            if counted or (c['draw_mode'] == 'timed' and int(time.time()) >= c['deadline']):
                try:
                    self.draw(row['id'], timed=not counted, counted=counted)
                except Exception:  # noqa: BLE001 - persisted operator-visible failure
                    reason = ('满员' if counted else '定时') + '开奖失败，待重试；未改变已确定结果'
                    self.db.execute("UPDATE lotteries SET last_error=? WHERE id=? AND state='published'", (reason, row['id']))
        self.deliver_awards()
        try:
            self.stage_reminders()
        except Exception:  # noqa: BLE001 - optional reminder must never block delivery
            logging.getLogger(__name__).warning('Lottery reminder staging failed; retried next tick')
        await self.deliver_notices()
        # Optional group/private operations run last and independently.
        for step in (self.deliver_dms, self.deliver_ops):
            try:
                await step()
            except Exception:  # noqa: BLE001 - durable rows remain for the next tick
                logging.getLogger(__name__).warning('Lottery %s interrupted; rows retained', step.__name__)
