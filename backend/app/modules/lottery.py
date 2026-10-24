"""Telegram lottery: frozen configuration, once-only settlement and durable delivery.

No network call is made inside a write transaction. SQLite BEGIN IMMEDIATE owns
all admission, draw and reward transitions, including across workers/restarts.
"""
from __future__ import annotations

import copy
import json
import re
import secrets
import time
from datetime import datetime
from html import escape

from app.modules import lottery_identity
from app.modules.economy_rules import BEIJING, economy_write, encode, validate_bag_spec
from app.modules.inventory import InventoryService
from app.modules.registration import generate_code
from app.modules.report_delivery import CALL_DELIVERY

RULES = ('免费报名，每个 Telegram 每期仅一次、最多一奖；报名时须属于发布群且符合参与对象。'
         '截止后不能报名；管理员可提前手动开奖。人数不足如实少发。'
         '本人专属注册资格仅在开奖时仍未注册者中抽取，先抽该类奖，再抽其他奖，'
         '同类奖项随机排序，每个名额在剩余合资格报名者中等概率抽取。'
         '账户类奖励未绑定时保留，绑定后可领取；商城道具入背包后使用生效。'
         '注册资格绑定中奖 Telegram，不可转赠，进入原 Bot 注册流程自行设置密码，受原注册通道开关约束。')
AUDIENCES = {'all': '全部群友', 'member': '仅已注册', 'guest': '仅未注册'}
EMOJI = {'gift': ('5235826903490764103', '🎁'), 'star': ('5830101874467938532', '⭐️'),
         'notice': ('5197304993920616826', '📣'), 'check': ('5350486389806868244', '✅')}
# ColorfulFontEmoji indices 11/20/2/10/24 render LUCKY, but each sticker's
# Telegram `emoji` metadata is 🔡. ASCII letters are not valid alternatives.
LETTERS = [('5364273836122463188', '🔡'), ('5368486821082715433', '🔡'),
           ('5364012727880668255', '🔡'), ('5364258799441957434', '🔡'), ('5381971708427132282', '🔡')]


def emoji(key):
    eid, fallback = EMOJI[key]
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'


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

    def validate(self, raw, *, now=None):
        now = int(time.time()) if now is None else now
        if not isinstance(raw, dict):
            raise ValueError('活动配置无效')  # noqa: TRY004 - API validation contract
        title = text(raw.get('title'), '标题', 60)
        anti_multi = raw.get('anti_multi', True)
        if type(anti_multi) is not bool:
            raise ValueError('防小号开关必须为布尔值')
        audience = raw.get('audience')
        if audience not in AUDIENCES:
            raise ValueError('参与对象无效')
        chat = str(raw.get('chat_id') or '')
        if chat not in self.bot._group_allowlist():
            raise ValueError('请选择已配置白名单群')
        deadline = integer(raw.get('deadline'), '截止时间', now + 60, now + 366 * 86400)
        mode = raw.get('draw_mode')
        if mode not in ('manual', 'timed'):
            raise ValueError('开奖方式无效')
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
                'deadline': deadline, 'draw_mode': mode, 'prizes': clean, 'anti_multi': anti_multi}

    def save(self, raw, actor, lottery_id=None, revision=None):
        config = self.validate(raw)
        now = int(time.time())
        with economy_write(self.db) as conn:
            if lottery_id:
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

    @staticmethod
    def audit(conn, actor, action, subject, detail=''):
        conn.execute('INSERT INTO audit_log(ts,actor,action,subject,detail,ok) VALUES(?,?,?,?,?,1)',
                     (int(time.time()), str(actor), 'lottery.' + action, str(subject), detail))

    def row(self, lottery_id):
        row = self.db.one('SELECT * FROM lotteries WHERE id=?', (lottery_id,))
        if not row:
            raise KeyError('活动不存在')
        row['config'] = json.loads(row.pop('config_json'))
        return row

    def view(self, lottery_id, tg_id, admin=False):
        row = self.row(lottery_id)
        if row['state'] == 'draft' and not admin:
            raise KeyError('活动尚未发布')
        config = copy.deepcopy(row['config'])
        config['prizes'] = [public_prize(p) for p in config['prizes']]
        config.pop('chat_id', None)
        config.pop('thread_id', None)
        if not admin:
            config.pop('anti_multi', None)
        data = {k: row[k] for k in ('id', 'state', 'revision', 'published_at', 'settled_at')}
        mine = self.db.one('SELECT excluded,joined_at FROM lottery_entries WHERE lottery_id=? AND tg_id=?',
                           (lottery_id, tg_id))
        rules = RULES
        if admin:
            rules += lottery_identity.RULE if config['anti_multi'] else '本期未开启 IP/浏览器关联排除。'
        data.update(config=config, rules=rules, count=self.db.one(
            'SELECT COUNT(*) AS n FROM lottery_entries WHERE lottery_id=?', (lottery_id,))['n'],
            joined=bool(mine), winners=[], my_award=None)
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
                    data.setdefault('awards', []).append(private)
        if admin:
            data['qualification'] = {'excluded': bool(mine and mine['excluded'])}
            data['excluded_count'] = self.db.one('SELECT COUNT(*) AS n FROM lottery_entries WHERE lottery_id=? AND excluded=1', (lottery_id,))['n']
            data['candidate_count'] = data['count'] - data['excluded_count']
            data.update(config=row['config'], notices=self.db.query(
                'SELECT id,kind,state,message_id,attempts,error FROM lottery_notices WHERE lottery_id=? ORDER BY id',
                (lottery_id,)), last_error=row['last_error'])
        return data

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
                                    else 'not_selected' if row['state'] == 'settled' else 'eligible')
            return {**counts, 'state': row['state'], 'anti_multi': row['config']['anti_multi'],
                    'entries': entries[:50], 'more': len(entries) > 50,
                    'clusters': lottery_identity.clusters(self.db, lottery_id)}

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

    def notice_payload(self, row, kind, winners=None):
        c = row['config']
        header = ''.join(f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>' for eid, fallback in LETTERS)
        lines = [header, emoji('notice' if kind == 'result' else 'gift') + ' <b>' + escape(c['title']) + '</b>']
        if kind == 'launch':
            lines += [f'参与对象：{AUDIENCES[c["audience"]]}', '截止：' + datetime.fromtimestamp(c['deadline'], BEIJING).strftime('%Y-%m-%d %H:%M 北京时间'),
                      '定时开奖' if c['draw_mode'] == 'timed' else '截止后等待管理员开奖（可提前开奖）']
            lines += [f'{emoji("star")} {escape(p["name"])} × {p["quantity"]} 名' for p in c['prizes']]
            lines += ['每人一次 · 免费参与 · 最多一奖',
                      '本人专属注册资格只在开奖时未注册者中先行抽取；其他奖随后抽取。' if any(p['kind'] == 'registration' for p in c['prizes']) else '',
                      '完整规则和私密领奖信息请打开活动。']
        else:
            winners = winners or []
            lines += [f'已开奖 · {len(winners)} 位中奖者（名额不足不补抽）']
            # Keep the public message bounded even with 200 winners/long labels.
            lines += [f'{escape(w["label"])} · {escape(json.loads(w["prize_json"])["name"])}' for w in winners[:12]]
            if len(winners) > 12:
                lines += [f'其余 {len(winners) - 12} 位及完整公示请打开结果页。']
            lines += ['账户奖励待绑定者可在绑定后领取；人工奖品由管理员确认发放。']
        payload = {'chat_id': c['chat_id'], 'text': '\n'.join(x for x in lines if x), 'parse_mode': 'HTML',
                   'reply_markup': {'inline_keyboard': [[{'text': '查看结果 / 领取奖励' if kind == 'result' else '参加抽奖',
                       'icon_custom_emoji_id': EMOJI['check' if kind == 'result' else 'gift'][0],
                       'url': f'https://t.me/{row["bot_username"]}?startapp=lottery_{row["id"]}'}]]}}
        if c['thread_id']:
            payload['message_thread_id'] = c['thread_id']
        return payload

    def stage_notice(self, conn, row, kind, winners=None):
        conn.execute('INSERT OR IGNORE INTO lottery_notices(lottery_id,kind,payload_json,updated_at) VALUES(?,?,?,?)',
                     (row['id'], kind, encode(self.notice_payload(row, kind, winners)), int(time.time())))

    async def publish(self, lottery_id, revision, actor):
        row = self.row(lottery_id)
        if row['state'] != 'draft':
            return  # duplicate request must not create a second launch
        if type(revision) is not int or revision != row['revision']:
            raise ValueError('草稿版本已变化，请重新预览')
        self.validate(row['config'])
        username, chat_id = await self.ready(row['config'])
        with economy_write(self.db) as conn:
            if int(time.time()) >= row['config']['deadline']:
                raise ValueError('报名截止时间已过，请修改草稿后发布')
            row['config']['chat_id'] = chat_id
            changed = conn.execute('UPDATE lotteries SET state=\'published\',published_at=?,bot_username=?,config_json=? '
                                   'WHERE id=? AND state=\'draft\' AND revision=?',
                                   (int(time.time()), username, encode(row['config']), lottery_id, revision)).rowcount
            if not changed:
                raise ValueError('草稿版本已变化，请重新预览')
            row['bot_username'] = username
            self.stage_notice(conn, row, 'launch')
            self.audit(conn, actor, 'publish', lottery_id)
        await self.deliver_notices(lottery_id)

    async def join(self, lottery_id, tg, *, source_ip=None, browser_id=None):
        if not self.bot._cfg().get('enabled'):
            raise ValueError('Bot 暂未启用，无法确认群成员身份')
        row = self.row(lottery_id)
        if row['state'] != 'published' or int(time.time()) >= row['config']['deadline']:
            raise ValueError('报名已截止或活动尚未发布')
        # No unavailable-check fail-open, including Telegram's restricted status.
        member = await self.bot._call('getChatMember', {'chat_id': row['config']['chat_id'], 'user_id': tg['id']})
        if not isinstance(member, dict):
            raise ValueError('无法确认群成员身份，请稍后重试（尚未报名）')  # noqa: TRY004
        if not (member.get('status') in ('member', 'administrator', 'creator') or
                (member.get('status') == 'restricted' and member.get('is_member') is True)):
            raise ValueError('仅发布群的群成员可报名')
        tg_id = str(tg['id'])
        with economy_write(self.db) as conn:
            current = conn.execute('SELECT * FROM lotteries WHERE id=?', (lottery_id,)).fetchone()
            c = json.loads(current['config_json'])
            now = int(time.time())
            if current['state'] != 'published' or now >= c['deadline']:
                raise ValueError('报名已截止')
            if conn.execute('SELECT 1 FROM lottery_entries WHERE lottery_id=? AND tg_id=?', (lottery_id, tg_id)).fetchone():
                return
            registered = bool(conn.execute('SELECT 1 FROM members WHERE tg_user_id=?', (tg_id,)).fetchone())
            if c['audience'] != 'all' and (c['audience'] == 'member') != registered:
                raise ValueError('你的注册状态不符合本期参与对象')
            label = ' '.join(str(tg.get(k) or '') for k in ('first_name', 'last_name')).strip()
            label = label or ('@' + tg['username'] if tg.get('username') else 'Telegram用户')
            conn.execute('INSERT INTO lottery_entries(lottery_id,tg_id,label,joined_at,registered) VALUES(?,?,?,?,?)',
                         (lottery_id, tg_id, label[:40], now, int(registered)))
            if c['anti_multi']:
                lottery_identity.record(conn, lottery_id, tg_id, source_ip, browser_id, now)

    def draw(self, lottery_id, actor='system', *, timed=False, now=None):
        now = int(time.time()) if now is None else now
        with economy_write(self.db) as conn:
            row = conn.execute('SELECT * FROM lotteries WHERE id=?', (lottery_id,)).fetchone()
            if not row:
                raise KeyError('活动不存在')
            if row['state'] == 'settled':
                return
            if row['state'] != 'published':
                raise ValueError('活动尚未发布')
            c = json.loads(row['config_json'])
            if timed and (c['draw_mode'] != 'timed' or now < c['deadline']):
                return
            remaining = [dict(r) for r in conn.execute('SELECT * FROM lottery_entries WHERE lottery_id=? AND excluded=0 ORDER BY tg_id', (lottery_id,))]
            registered = {str(r[0]) for r in conn.execute("SELECT tg_user_id FROM members WHERE tg_user_id<>''")}
            rng = secrets.SystemRandom()
            special = [p for p in c['prizes'] if p['kind'] == 'registration']
            normal = [p for p in c['prizes'] if p['kind'] != 'registration']
            rng.shuffle(special)
            rng.shuffle(normal)
            for p in special + normal:
                eligible = [r for r in remaining if p['kind'] != 'registration' or r['tg_id'] not in registered]
                winners = rng.sample(eligible, min(p['quantity'], len(eligible)))
                for w in winners:
                    conn.execute('INSERT INTO lottery_awards(lottery_id,tg_id,label,prize_json) VALUES(?,?,?,?)',
                                 (lottery_id, w['tg_id'], w['label'], encode(p)))
                    remaining.remove(w)
            conn.execute('UPDATE lotteries SET state=\'settled\',settled_at=?,last_error=\'\' WHERE id=?', (now, lottery_id))
            winners = [dict(r) for r in conn.execute('SELECT * FROM lottery_awards WHERE lottery_id=? ORDER BY id', (lottery_id,))]
            self.stage_notice(conn, {**dict(row), 'config': c}, 'result', winners)
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

    async def deliver_notices(self, lottery_id=None):
        now = int(time.time())
        # A crashed sender might have reached Telegram. Never replay an expired lease.
        self.db.execute("UPDATE lottery_notices SET state='unknown',error='发送进程中断，需人工核验' WHERE state='sending' AND updated_at<?", (now - 180,))
        if not self.bot._cfg().get('enabled'):
            self.db.execute("UPDATE lottery_notices SET error='Bot 暂未启用，公告保留等待恢复' WHERE state='pending'")
            return
        rows = self.db.query("SELECT * FROM lottery_notices WHERE state='pending' AND next_at<=?" +
                             (' AND lottery_id=?' if lottery_id else '') + ' ORDER BY id LIMIT 20',
                             (now, lottery_id) if lottery_id else (now,))
        for notice in rows:
            launch_deadline = None
            with economy_write(self.db) as conn:
                if notice['kind'] == 'launch' and not notice['message_id']:
                    activity = conn.execute('SELECT state,config_json FROM lotteries WHERE id=?',
                                            (notice['lottery_id'],)).fetchone()
                    launch_deadline = json.loads(activity['config_json'])['deadline']
                    if activity['state'] != 'published' or launch_deadline <= int(time.time()):
                        conn.execute("UPDATE lottery_notices SET state='failed',error=?,next_at=0 WHERE id=? AND state='pending'",
                                     ('发起公告报名期已结束，未补发；请管理员人工处理', notice['id']))
                        continue
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
            result = await self._send(method, payload)
            failure = CALL_DELIVERY.get() or {}
            from app.modules.telegram import _CALL_ERROR
            error = str(_CALL_ERROR.get() or '').lower()
            _, allow_fallback = notice_rejection(error, failure)
            has_custom = '<tg-emoji' in payload['text'] or any(
                'icon_custom_emoji_id' in button for buttons in payload['reply_markup']['inline_keyboard'] for button in buttons)
            expired = launch_deadline is not None and launch_deadline <= int(time.time())
            downgraded = result is None and allow_fallback and has_custom and not expired
            if downgraded:
                # Only our known generated tags; no general HTML interpretation.
                for eid, fallback in LETTERS + list(EMOJI.values()):
                    payload['text'] = payload['text'].replace(f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>', fallback)
                for buttons in payload['reply_markup']['inline_keyboard']:
                    for button in buttons:
                        button.pop('icon_custom_emoji_id', None)
                # Persist before the retry, so a later known connection failure /
                # rate limit or manual recovery cannot reintroduce the icons.
                saved = json.loads(notice['payload_json'])
                saved.update(text=payload['text'], reply_markup=payload['reply_markup'])
                self.db.execute('UPDATE lottery_notices SET payload_json=? WHERE id=?', (encode(saved), notice['id']))
                result = await self._send(method, payload)
                failure = CALL_DELIVERY.get() or {}
                error = str(_CALL_ERROR.get() or '').lower()
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
                  and failure.get('reason') in ('连接失败，稍后重试', 'Telegram 限流')):
                state = 'pending' if notice['attempts'] < 3 else 'failed'
                reason = failure['reason']
                next_at = now + max(60 * 2 ** notice['attempts'], failure.get('retry_after', 0))
            # 5xx/timeout or unknown result: even shared report delivery may label
            # it retryable; lottery public announcements intentionally do not.
            self.db.execute('UPDATE lottery_notices SET state=?,message_id=?,error=?,updated_at=?,next_at=? WHERE id=?',
                            (state, mid, reason, int(time.time()), next_at, notice['id']))

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

    async def tick(self):
        for row in self.db.query("SELECT id,config_json FROM lotteries WHERE state='published'"):
            c = json.loads(row['config_json'])
            if c['draw_mode'] == 'timed' and int(time.time()) >= c['deadline']:
                try:
                    self.draw(row['id'], timed=True)
                except Exception:  # noqa: BLE001 - persisted operator-visible failure
                    self.db.execute('UPDATE lotteries SET last_error=? WHERE id=?', ('定时开奖失败，待重试；未改变已确定结果', row['id']))
        self.deliver_awards()
        await self.deliver_notices()
