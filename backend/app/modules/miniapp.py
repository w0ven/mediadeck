"""Member Mini App: Telegram-signed identity over the services the Bot already uses.

Every call carries the WebApp ``initData``; nothing is trusted from the client
beyond what that signature covers. Money and quota actions go through the same
service methods, idempotency keys and confirmation intents as the Bot.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote

from fastapi import Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from app.modules.economy_rules import economy_write, encode
from app.modules.groups import WHITELIST_GROUP_ID
from app.modules.invite_tree import InviteTree, active_suspension
from app.modules.member_devices import device_cap
from app.modules.points_ranking import PointsRankingService
from app.modules.requests import RequestError, normalize_demand
from app.modules.tmdb import poster_url
from app.modules.usage import run_usage_io

INIT_MAX_AGE = 24 * 3600
DAY = 86400
STATIC = Path(__file__).resolve().parents[1] / 'static' / 'miniapp'
_POSTER_KEY = secrets.token_bytes(32)
_LIBRARY_TTL = 600.0


class InitDataError(ValueError):
    pass


def verify_init_data(raw: str, bot_token: str, *, now: float | None = None,
                     max_age: int = INIT_MAX_AGE) -> dict[str, Any]:
    """Validate Telegram WebApp initData and return the signed user."""
    if not raw or not bot_token:
        raise InitDataError('missing')
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise InitDataError('malformed') from None
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise InitDataError('duplicate field')
    received = fields.pop('hash', '')
    check = '\n'.join(f'{k}={v}' for k, v in sorted(fields.items()))
    secret = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not received or not hmac.compare_digest(expected, received):
        raise InitDataError('bad signature')
    try:
        signed_at = int(fields.get('auth_date', '0'))
        user = json.loads(fields.get('user', ''))
    except ValueError:
        raise InitDataError('malformed') from None
    moment = time.time() if now is None else now
    if signed_at <= 0 or moment - signed_at > max_age or signed_at - moment > 300:
        raise InitDataError('expired')
    if not isinstance(user, dict) or type(user.get('id')) is not int or user['id'] <= 0:
        raise InitDataError('no user')
    return user


def poster_sig(item_id: str) -> str:
    return hmac.new(_POSTER_KEY, str(item_id).encode(), hashlib.sha256).hexdigest()[:24]


def avatar_url(tg_id: Any) -> str:
    tg = str(tg_id or '').strip()
    if not tg.isascii() or not tg.isdigit() or int(tg) <= 0:
        return ''
    return f'/api/app/avatar/{tg}?s={poster_sig("avatar:" + tg)}'


def _thumbnail(raw: bytes) -> bytes | None:
    from io import BytesIO

    from PIL import Image, ImageOps
    try:
        with Image.open(BytesIO(raw)) as image:
            out = BytesIO()
            ImageOps.fit(image.convert('RGB'), (160, 160)).save(out, 'JPEG', quality=82)
            return out.getvalue()
    except Exception:  # noqa: BLE001 - a broken picture falls back to the initial
        return None


def _fail(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {'code': code, 'message': message})


def _display_name(tg: dict[str, Any], profile: dict[str, Any] | None = None) -> str:
    profile = profile or {}
    invisible = ' \u200b\u200c\u200d\ufeff\u3164\u2800\u115f\u1160'
    name = str(profile.get('display_name') or '').strip(invisible)
    if not name:
        name = ' '.join(str(tg.get(k) or '') for k in ('first_name', 'last_name')).strip(invisible)
    username = str(profile.get('username') or tg.get('username') or '').strip()
    return name or (f'@{username}' if username else '')


def _rank_label(row: dict[str, Any], profiles: dict[str, dict[str, str]]) -> str:
    """Public ranking name: TG nickname, then @username; never the Emby account."""
    invisible = ' \u200b\u200c\u200d\ufeff\u3164\u2800\u115f\u1160'
    profile = profiles.get(str(row.get('tg_user_id') or '')) or {}
    name = str(profile.get('display_name') or row.get('tg_display_name') or '').strip(invisible)
    if name:
        return name[:40]
    username = str(profile.get('username') or row.get('tg_username') or '').strip(invisible)
    if username:
        return '@' + username[:40]
    return 'Telegram用户' if row.get('tg_user_id') else '未绑定'


def _level_tag(member: dict[str, Any] | None) -> dict[str, Any] | None:
    if not member:
        return None
    if member.get('group_id') == WHITELIST_GROUP_ID:
        return {'whitelist': True}
    level = member.get('activity_level')
    if not level:
        return None
    return {'level': level['level'], 'name': level['name'], 'icon': level['icon']}


def _library_url(base: str, item_id: str) -> str:
    base = str(base or '').strip().rstrip('/')
    if not base.startswith(('https://', 'http://')) or not item_id:
        return ''
    return f"{base}/web/index.html#!/item?id={quote(str(item_id), safe='')}"


def register(app: Any) -> None:
    state = app.state
    library_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
    avatar_cache: dict[str, tuple[float, bytes | None]] = {}
    avatar_gate = asyncio.Semaphore(3)

    async def io(fn, *args, **kwargs):
        return await run_usage_io(fn, *args, **kwargs)

    def bot():
        return getattr(state, 'telegram', None)

    def tg_cfg() -> dict[str, Any]:
        b = bot()
        return b._cfg() if b is not None else {}

    async def telegram_context(request: Request) -> dict[str, Any]:
        b = bot()
        token = b._token() if b is not None else ''
        if not token or not str(tg_cfg().get('miniapp_url') or '').strip():
            raise _fail(503, 'disabled', '会员面板暂未开放')
        try:
            user = verify_init_data(request.headers.get('X-Telegram-Init-Data', ''), token)
        except InitDataError as exc:
            if str(exc) == 'expired':
                raise _fail(401, 'expired', '页面已打开太久，请关闭后从 Bot 重新打开') from None
            raise _fail(401, 'unsigned', '请从 Bot 里的「📱 会员面板」打开') from None
        tg_id = str(user['id'])
        member = await io(state.members.find_by_telegram, tg_id)
        return {'tg': user, 'tg_id': tg_id, 'member': member,
                'uid': str(member['emby_user_id']) if member else '',
                'is_admin': bool(b.is_admin(member))}

    async def member_context(request: Request) -> dict[str, Any]:
        ctx = await telegram_context(request)
        if not ctx['member']:
            raise _fail(403, 'unbound', '这个 Telegram 还没有绑定账号，请先在 Bot 里注册或绑定')
        return ctx

    Ctx = Depends(member_context)
    from app.modules.lottery_api import register as register_lottery
    register_lottery(app, telegram_context)
    from app.modules.game_center_api import register as register_games
    register_games(app, member_context)


    def refreshed(ctx):
        return state.members.get(ctx['uid']) or ctx['member']

    # -- pages ------------------------------------------------------------------

    @app.get('/app', include_in_schema=False)
    @app.get('/app/', include_in_schema=False)
    async def miniapp_page():
        html = (STATIC / 'index.html').read_text(encoding='utf-8')
        try:
            ver = str((await asyncio.to_thread(state.updater.version)).get('commit') or '')
        except Exception:  # noqa: BLE001 - version stamping must never break the page
            ver = ''
        if ver:
            for asset in ('app.css', 'app.js', 'lottery.css', 'lottery.js', 'ux.js', 'vendor/thumbmark.umd.js'):
                html = html.replace(f'/app/assets/{asset}', f'/app/assets/{asset}?v={quote(ver)}')
        return HTMLResponse(html, headers={'Cache-Control': 'no-cache'})

    app.mount('/app/assets', StaticFiles(directory=str(STATIC)), name='miniapp-assets')

    # -- me ---------------------------------------------------------------------

    def me_payload(ctx):
        member = refreshed(ctx)
        uid = ctx['uid']
        levels = state.levels
        info = levels.status(uid)
        level = info['level']
        config = levels.config()['config']
        watch = state.stats.watch_summary(uid) if state.stats is not None else {}
        b = bot()
        profile = b._tg_profiles.get(ctx['tg_id']) if b is not None else None
        expiry = info['expires_at']
        group = member.get('group') or {}
        blockers = []
        if not info['managed']:
            blockers.append('账号尚未开通等级续期')
        if info['lifecycle'] != 'ready':
            blockers.append('账号删除流程待确认，请联系管理员')
        if not info['window_open'] and expiry:
            blockers.append('到期前 72 小时内才能续期')
        if info['shortfall']:
            blockers.append(f"积分不足，还差 {info['shortfall']} 积分")
        return {
            'is_admin': ctx['is_admin'],
            'name': _display_name(ctx['tg'], profile) or str(member.get('username') or ''),
            'username': str(member.get('username') or ''),
            'state': str(member.get('state') or member.get('status') or ''),
            'whitelist': bool(info['exempt']),
            'managed': bool(info['managed']),
            'level': {k: level.get(k) for k in ('level', 'name', 'title', 'icon', 'growth', 'next_threshold',
                                                 'threshold')},
            'worn_title': info['worn_title'],
            'balance': info['balance'],
            'today': {'day': str(info['day']), 'points': int(info['today'].get('points') or 0),
                      'growth': int(info['today'].get('growth') or 0)},
            'expires_at': expiry,
            'delete_at': info['delete_at'],
            'renewal': {'price': info['renewal_price'], 'shortfall': info['shortfall'],
                        'window_open': info['window_open'], 'opens_at': expiry - 3 * DAY if expiry else None,
                        'auto_renew': info['auto_renew'], 'blockers': blockers,
                        'available': info['managed'] and not info['exempt']},
            'binding': levels.binding_revision(uid),
            'rights': {'bandwidth_kbps': member.get('bandwidth_limit_kbps'),
                       'max_streams': member.get('max_streams'),
                       'request_quota': group.get('request_quota'),
                       'download': bool(member.get('allow_download')),
                       'transcode': bool(member.get('allow_transcode')),
                       'max_devices': device_cap(member)},
            'watch': {'seconds_24h': watch.get('seconds_24h'), 'seconds_30d': watch.get('seconds_30d'),
                      'total': watch.get('recorded_seconds')},
            'levels': [{k: row.get(k) for k in ('level', 'name', 'title', 'icon', 'threshold', 'price',
                                                'bandwidth_limit_kbps', 'max_streams', 'request_quota',
                                                'max_devices')}
                       for row in config['levels']],
            'bot_username': str(getattr(b, '_bot_username', '') or ''),
            'features': {
                'points_ranking': bool(b and b._plugin_on('points_ranking')),
                'shop': state.shop is not None,
                'invites': getattr(state, 'registration', None) is not None,
            },
        }

    @app.get('/api/app/me')
    async def app_me(ctx=Ctx):
        payload = await io(me_payload, ctx)
        payload['avatar'] = avatar_url(ctx['tg_id'])
        try:
            payload['devices'] = len(await state.member_devices.for_user(ctx['uid']))
        except Exception:  # noqa: BLE001 - the count is optional on the home page
            payload['devices'] = None
        return payload

    # -- renewal ----------------------------------------------------------------

    def renew_preview(ctx):
        uid, levels = ctx['uid'], state.levels
        info = levels.status(uid)
        payload = me_payload(ctx)['renewal']
        now = int(time.time())
        expiry = info['expires_at']
        new_expiry = max(now, expiry) + 30 * DAY if expiry else None
        out = {'price': info['renewal_price'], 'balance': info['balance'],
               'balance_after': info['balance'] - info['renewal_price'],
               'expires_at': expiry, 'new_expires_at': new_expiry,
               'level': {k: info['level'].get(k) for k in ('level', 'name', 'icon')},
               'blockers': payload['blockers'] if payload['available'] else ['白名单账号永久有效，无需续期'
                                                                               if info['exempt'] else
                                                                               '账号尚未开通等级续期']}
        if out['blockers']:
            return out
        nonce = secrets.token_hex(12)
        snapshot = {'expiry': expiry, 'price': info['renewal_price'], 'level': info['level']['level'],
                    'revision': levels.config()['revision'], 'binding': levels.binding_revision(uid)}
        with economy_write(levels.db) as conn:
            conn.execute('INSERT INTO activity_renew_intents VALUES(?,?,?,?,?,?,NULL)',
                         (nonce, uid, ctx['tg_id'], ctx['tg_id'], encode(snapshot), now + 300))
        out['nonce'] = nonce
        return out

    @app.post('/api/app/renew/preview')
    async def app_renew_preview(ctx=Ctx):
        return await io(renew_preview, ctx)

    @app.post('/api/app/renew/confirm')
    async def app_renew_confirm(payload: dict = Body(...), ctx=Ctx):  # noqa: B008
        nonce = str(payload.get('nonce') or '')
        if not nonce or len(nonce) > 64:
            raise _fail(400, 'bad_request', '确认已失效，请重新打开续期')
        try:
            result = await io(state.levels.renew, ctx['uid'], 'bot-renew:' + nonce,
                              bot_intent=(nonce, ctx['tg_id'], ctx['tg_id']))
        except ValueError as exc:
            raise _fail(409, 'rejected', str(exc)) from None
        b = bot()
        warning = (await b._after_member_change(ctx['member'])).strip() if b is not None else ''
        return {'ok': True, 'cost': result.get('cost'), 'expires_at': result.get('expires_at'),
                'previous_expires_at': result.get('previous_expires_at'), 'warning': warning,
                'suspended': ctx['member'].get('state', ctx['member'].get('status')) == 'suspended'}

    @app.post('/api/app/auto-renew')
    async def app_auto_renew(payload: dict = Body(...), ctx=Ctx):  # noqa: B008
        enabled = payload.get('enabled')
        if type(enabled) is not bool:
            raise _fail(400, 'bad_request', '开关参数无效')
        try:
            return await io(state.levels.set_auto, ctx['uid'], enabled, tg_uid=ctx['tg_id'],
                            binding_revision=payload.get('binding'))
        except ValueError as exc:
            raise _fail(409, 'rejected', str(exc)) from None

    # -- playback lines -----------------------------------------------------------

    @app.get('/api/app/lines')
    async def app_lines(ctx=Ctx):
        cfg = tg_cfg()
        member = ctx['member']
        eligible = (member.get('group_id') == WHITELIST_GROUP_ID
                    and member.get('state', member.get('status')) == 'active')
        lines = [{'label': str(line.get('label') or ''), 'url': str(line.get('url') or ''),
                  'hint': str(line.get('hint') or '')}
                 for line in (cfg.get('playback_lines') or [])
                 if not line.get('whitelist_only') or eligible]
        if not lines and str(cfg.get('emby_public_url') or '').strip():
            lines = [{'label': '服务器', 'url': str(cfg['emby_public_url']).strip(), 'hint': ''}]
        b = bot()
        online = await b._online_plays() if b is not None else None
        return {'lines': lines, 'note': str(cfg.get('playback_lines_note') or '').strip(),
                'routing_rules': bool(str(cfg.get('playback_routing_rules') or '').strip()),
                'online': online}

    # -- rankings -----------------------------------------------------------------

    def rank_watch(ctx, period):
        b = bot()
        if period == 'today':
            board = b._today_board() if b is not None else {'unavailable': True}
            if board.get('unavailable'):
                return {'available': False}
            rows = [dict(r) for r in board['users']]
            stamp = board.get('stamp', '')
        else:
            days = 7 if period == '7d' else 1
            rows = state.stats.top_users(days=days, limit=5000, calendar=True)
            from app.modules.stats import ranking_stamp
            stamp = ranking_stamp(days)
        profiles = b._tg_profiles if b is not None else {}
        out, mine = [], None
        for index, row in enumerate(rows, 1):
            seconds = int(row.get('seconds') or (row.get('hours') or 0) * 3600)
            uid = str(row.get('user_id') or row.get('emby_user_id') or '')
            if uid == ctx['uid']:
                mine = {'rank': index, 'value': seconds}
            if index <= 50:
                out.append({'rank': index, 'name': _rank_label(row, profiles), 'value': seconds,
                            'avatar': avatar_url(row.get('tg_user_id')),
                            'tag': _level_tag(state.members.get(uid)) if uid else None})
        return {'available': True, 'stamp': stamp, 'rows': out, 'mine': mine, 'unit': 'seconds'}

    def rank_points(ctx):
        b = bot()
        if not (b and b._plugin_on('points_ranking')):
            return {'available': False, 'message': '积分榜暂未开放'}
        rows = PointsRankingService(state.members, state.points).rows()
        profiles = b._tg_profiles
        out, mine = [], None
        for index, row in enumerate(rows, 1):
            if row['_uid'] == ctx['uid']:
                mine = {'rank': index, 'value': row['points']}
            if index <= 50:
                member = state.members.get(row['_uid']) or {}
                out.append({'rank': index, 'name': _rank_label(member, profiles), 'value': row['points'],
                            'avatar': avatar_url(member.get('tg_user_id')), 'tag': _level_tag(member)})
        return {'available': True, 'rows': out, 'mine': mine, 'unit': 'points'}

    def rank_titles(period):
        b = bot()
        if period == 'today':
            board = b._today_board() if b is not None else {'unavailable': True}
            if board.get('unavailable'):
                return {'available': False}
            movies, shows, stamp = board['movies'], board['shows'], board.get('stamp', '')
        else:
            days = 7 if period == '7d' else 1
            movies, shows = state.stats.top_titles_split(days=days, limit=12, calendar=True)
            from app.modules.stats import ranking_stamp
            stamp = ranking_stamp(days)
        base = str(tg_cfg().get('emby_public_url') or '')

        def shape(rows):
            return [{'rank': i, 'title': str(r.get('title') or ''), 'plays': int(r.get('plays') or 0),
                     'seconds': int(r.get('seconds') or (r.get('hours') or 0) * 3600),
                     'poster': (f"/api/app/poster/{quote(r['item_id'], safe='')}?s={poster_sig(r['item_id'])}"
                                if r.get('item_id') else ''),
                     'url': _library_url(base, r.get('item_id') or '')}
                    for i, r in enumerate(rows[:12], 1)]
        return {'available': True, 'stamp': stamp, 'movies': shape(movies), 'shows': shape(shows)}

    @app.get('/api/app/rank')
    async def app_rank(kind: str = 'watch', period: str = 'today', ctx=Ctx):
        if period not in ('today', 'yesterday', '7d'):
            raise _fail(400, 'bad_request', '时间范围无效')
        try:
            if kind == 'watch':
                return await io(rank_watch, ctx, period)
            if kind == 'points':
                return await io(rank_points, ctx)
            if kind == 'titles':
                return await io(rank_titles, period)
        except Exception:  # noqa: BLE001 - unavailable is not an empty chart
            return {'available': False}
        raise _fail(400, 'bad_request', '榜单类型无效')

    @app.get('/api/app/poster/{item_id}')
    async def app_poster(item_id: str, s: str = ''):
        if not hmac.compare_digest(poster_sig(item_id), s or ''):
            raise HTTPException(404)
        fetch = getattr(state.emby, 'item_primary_image', None)
        blob = await fetch(item_id) if callable(fetch) else None
        if not blob:
            raise HTTPException(404)
        kind = 'image/png' if blob[:8] == b'\x89PNG\r\n\x1a\n' else 'image/jpeg'
        return Response(blob, media_type=kind, headers={'Cache-Control': 'private, max-age=86400'})

    @app.get('/api/app/avatar/{tg_id}')
    async def app_avatar(tg_id: str, s: str = ''):
        if not tg_id.isascii() or not tg_id.isdigit() or not hmac.compare_digest(poster_sig('avatar:' + tg_id), s or ''):
            raise HTTPException(404)
        hit = avatar_cache.get(tg_id)
        if not hit or hit[0] <= time.monotonic():
            async with avatar_gate:
                hit = avatar_cache.get(tg_id)
                if not hit or hit[0] <= time.monotonic():
                    b = bot()
                    try:
                        raw = await b._tg_avatar_bytes(tg_id) if b is not None else None
                    except Exception:  # noqa: BLE001 - avatar is optional
                        raw = None
                    blob = await asyncio.to_thread(_thumbnail, raw) if raw else None
                    if len(avatar_cache) > 1000:
                        avatar_cache.clear()
                    hit = (time.monotonic() + (6 * 3600 if blob else 1800), blob)
                    avatar_cache[tg_id] = hit
        if not hit[1]:
            raise HTTPException(404)
        return Response(hit[1], media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=21600'})

    # -- password -------------------------------------------------------------------

    @app.post('/api/app/password')
    async def app_password(payload: dict = Body(...), ctx=Ctx):  # noqa: B008
        from app.core.errors import ConfigError
        from app.modules.bot_passwords import validate_password
        # The client generates random passwords too, so it can always show what it sent.
        password = payload.get('password')
        if not isinstance(password, str) or len(password) > 128:
            raise _fail(400, 'bad_request', '密码无效')
        try:
            validate_password(password)
        except ConfigError as exc:
            raise _fail(400, 'bad_request', str(exc)) from None
        b = bot()
        if b is None or getattr(b, '_emby', None) is None:
            raise _fail(503, 'disabled', '暂时无法修改密码，请稍后再试')
        current = await io(state.members.find_by_telegram, ctx['tg_id'])
        if not current or str(current['emby_user_id']) != ctx['uid']:
            raise _fail(409, 'rejected', '账号绑定已变化，未修改密码')
        mode = 'random' if payload.get('mode') == 'random' else 'custom'
        ok, notices = await b._apply_password_reset(ctx['uid'], password, f"miniapp:{ctx['tg_id']}:{mode}")
        if not ok:
            raise _fail(502, 'upstream', '修改失败或结果未确认，请稍后重试；旧密码可能仍然有效')
        return {'ok': True, 'username': str(current.get('username') or ''), 'notices': notices}

    # -- devices --------------------------------------------------------------------

    def players():
        from app.modules.settings import PLAYER_PLATFORMS
        rows = tg_cfg().get('recommended_players') or []
        return [{'platform': p, 'players': [{k: r[k] for k in ('name', 'note', 'url')} for r in rows
                                            if r.get('platform') == p]}
                for p in PLAYER_PLATFORMS if any(r.get('platform') == p for r in rows)]

    @app.get('/api/app/devices')
    async def app_devices(ctx=Ctx):
        limit = device_cap(await io(refreshed, ctx))
        try:
            rows = await state.member_devices.for_user(ctx['uid'])
        except Exception:  # noqa: BLE001 - unavailable is not "no devices"
            return {'available': False, 'limit': limit, 'players': players()}
        return {'available': True, 'devices': rows, 'count': len(rows), 'limit': limit, 'players': players()}

    @app.post('/api/app/devices/remove')
    async def app_device_remove(payload: dict = Body(...), ctx=Ctx):  # noqa: B008
        device_id = str(payload.get('id') or '').strip()
        if not device_id or len(device_id) > 128:
            raise _fail(400, 'bad_request', '设备无效')
        try:
            removed = await state.member_devices.remove(ctx['uid'], device_id)
        except LookupError as exc:
            raise _fail(404, 'gone', str(exc)) from None
        except Exception:  # noqa: BLE001 - Emby errors never reach members verbatim
            raise _fail(502, 'upstream', '暂时无法移除，请稍后再试') from None
        await io(state.members.audit, f"miniapp:{ctx['tg_id']}", 'device.remove', ctx['uid'],
                 json.dumps({'name': removed['name'], 'app': removed['app']}, ensure_ascii=False))
        return {'ok': True, 'name': removed['name']}

    # -- requests -----------------------------------------------------------------

    async def presence(media_type, tmdb_id, uid, *, episodes):
        key = (media_type + (':ep' if episodes else ''), int(tmdb_id))
        cached = library_cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        lookup = getattr(state.emby, 'request_lookup', None)
        try:
            if lookup is None:
                raise RuntimeError('unconfigured')
            items = await asyncio.wait_for(
                lookup(media_type, int(tmdb_id), user_id=uid, include_episodes=episodes), 8)
            hint = {'available': True, 'items': items}
        except Exception:  # noqa: BLE001 - advisory only; submission stays possible
            return {'available': False, 'items': []}
        if len(library_cache) > 2000:
            library_cache.clear()
        library_cache[key] = (time.monotonic() + _LIBRARY_TTL, hint)
        return hint

    def quota_info(uid):
        member = state.members.get(uid)
        quota = state.requests.quota_for(member) if member else 0
        used = state.requests.used(uid)
        return {'quota': quota or None, 'used': used,
                'remaining': None if not quota else max(0, quota - used)}

    @app.get('/api/app/requests/search')
    async def app_request_search(q: str = "", type: str = "", page: int = 1, ctx=Ctx):
        query = q.strip()[:100]
        if not query:
            raise _fail(400, 'bad_request', '请输入片名')
        tmdb = getattr(state, 'tmdb', None)
        result = await tmdb.search(query, max(1, page), type if type in ('movie', 'tv') else '', None) \
            if tmdb is not None else {'available': False}
        if not result.get('available'):
            return {'available': False, 'message': '片名搜索暂不可用，请稍后再试'}
        rows = result.get('results') or []
        sem = asyncio.Semaphore(3)

        async def badge(row):
            async with sem:
                hint = await presence(row['media_type'], row['tmdb_id'], ctx['uid'], episodes=False)
            same = await io(state.requests.history, row['media_type'], row['tmdb_id'])
            open_ = any(r.get('status') == 'open' for r in same)
            accepted = any(r.get('status') == 'accepted' for r in same)
            return {'media_type': row['media_type'], 'tmdb_id': row['tmdb_id'],
                    'title': row.get('title') or '', 'year': row.get('year'),
                    'poster': poster_url(row.get('poster_path') or ''),
                    'in_library': bool(hint.get('items')),
                    'requested': 'open' if open_ else 'accepted' if accepted else ''}
        items = await asyncio.gather(*(badge(r) for r in rows[:20]))
        return {'available': True, 'results': items, 'page': result.get('page', page),
                'total_pages': result.get('total_pages', 1), **await io(quota_info, ctx['uid'])}

    @app.get('/api/app/requests/title')
    async def app_request_title(media_type: str, tmdb_id: int, ctx=Ctx):
        if media_type not in ('movie', 'tv') or tmdb_id <= 0:
            raise _fail(400, 'bad_request', '影片无效')
        tmdb = getattr(state, 'tmdb', None)
        lookup = getattr(tmdb, 'lookup_request', None) if tmdb is not None else None
        meta = (await lookup(media_type, tmdb_id) if lookup else None) or {}
        hint = await presence(media_type, tmdb_id, ctx['uid'], episodes=media_type == 'tv')
        base = str(tg_cfg().get('emby_public_url') or '')
        have: dict[int, int] = {}
        for item in hint.get('items') or []:
            for ep in item.get('episodes') or []:
                if isinstance(ep.get('season'), int):
                    have[ep['season']] = have.get(ep['season'], 0) + 1
        seasons = []
        for s in meta.get('seasons') or []:
            number = s.get('season_number')
            if not isinstance(number, int) or number <= 0:
                continue
            total = int(s.get('episode_count') or 0)
            got = have.get(number, 0)
            seasons.append({'season': number, 'episodes': total, 'have': got,
                            'state': 'full' if total and got >= total else 'partial' if got else 'missing'})
        history = await io(state.requests.history, media_type, tmdb_id)
        return {
            'media_type': media_type, 'tmdb_id': tmdb_id,
            'title': meta.get('title') or '', 'original_title': meta.get('original_title') or '',
            'year': meta.get('year'), 'overview': str(meta.get('overview') or '')[:600],
            'poster': poster_url(meta.get('poster_path') or ''),
            'library': {'available': hint.get('available', False),
                        'items': [{'name': i.get('name'), 'url': _library_url(base, i.get('id'))}
                                  for i in (hint.get('items') or [])[:3]]},
            'seasons': seasons,
            'history': [{'id': r.get('id'), 'status': r.get('status'), 'status_label': r.get('status_label'),
                         'demand_text': r.get('demand_text')} for r in history[:5]],
            **await io(quota_info, ctx['uid']),
        }

    def demand_from(payload):
        media_type = payload.get('media_type')
        seasons = payload.get('seasons')
        if media_type == 'tv' and seasons:
            raw = {'scope': 'season', 'seasons': [int(s) for s in seasons][:100]}
        else:
            raw = {}
        return normalize_demand(media_type, raw)

    @app.post('/api/app/requests/check')
    async def app_request_check(payload: dict = Body(...), ctx=Ctx):  # noqa: B008
        try:
            demand = demand_from(payload)
            same = await io(state.requests.same_demand, payload['media_type'], int(payload['tmdb_id']), demand)
        except (RequestError, KeyError, TypeError, ValueError) as exc:
            raise _fail(400, 'bad_request', str(exc) or '需求无效') from None
        return {'same': {'id': same['id'], 'status': same['status'],
                         'mine': str(same.get('emby_user_id')) == ctx['uid']} if same else None,
                **await io(quota_info, ctx['uid'])}

    @app.post('/api/app/requests')
    async def app_request_create(payload: dict = Body(...), ctx=Ctx):  # noqa: B008
        key = str(payload.get('create_key') or '')
        if not 16 <= len(key) <= 64:
            raise _fail(400, 'bad_request', '请重新确认后提交')
        try:
            demand = demand_from(payload)
            row = await state.requests.create(ctx['uid'], payload['media_type'], int(payload['tmdb_id']), '',
                                              demand=demand, confirmed_type=True,
                                              create_key='app:' + key)
        except (RequestError, KeyError, TypeError, ValueError) as exc:
            raise _fail(409, 'rejected', str(exc) or '提交失败') from None
        b = bot()
        if b is not None:
            with contextlib.suppress(Exception):
                await b.flush_request_notifications()
        return {'id': row['id'], 'created': bool(row.get('created')), 'followed': bool(row.get('followed')),
                'status': row.get('status'), 'status_label': row.get('status_label'),
                **await io(quota_info, ctx['uid'])}

    @app.get('/api/app/requests/mine')
    async def app_requests_mine(ctx=Ctx):
        def load():
            own = state.requests.list(user_id=ctx['uid'], limit=30)
            followed = state.requests.list(user_id=ctx['uid'], followed=True, limit=30)
            return own, followed

        own, followed = await io(load)

        def shape(r, follow=False):
            return {'id': r['id'], 'title': r.get('display_title') or r.get('title') or '',
                    'media_label': r.get('media_label'), 'demand_text': r.get('demand_text'),
                    'status': r.get('status'), 'status_label': r.get('status_label'),
                    'poster': poster_url(r.get('poster_path') or ''), 'created_at': r.get('created_at'),
                    'followed': follow}
        seen = {r['id'] for r in own}
        rows = [shape(r) for r in own] + [shape(r, True) for r in followed if r['id'] not in seen]
        rows.sort(key=lambda r: -int(r['id']))
        return {'rows': rows[:40], **await io(quota_info, ctx['uid'])}

    # -- bag ------------------------------------------------------------------------

    @app.get('/api/app/shop')
    async def app_shop(ctx=Ctx):
        if state.shop is None:
            return {'available': False}

        def load():
            items = state.shop.items(enabled_only=True)
            balance = state.points.balance(ctx['uid'])
            out = []
            for item in items:
                limit = int(item.get('per_user_limit') or 0)
                out.append({'id': item['id'], 'name': item.get('name'), 'description': item.get('description') or '',
                            'price': int(item.get('cost') or 0), 'kind': item.get('kind'),
                            'kind_label': item.get('kind_label'), 'duration_days': item.get('duration_days'),
                            'limit': limit or None,
                            'redeemed': state.shop.redeemed_count(ctx['uid'], item['id']) if limit else None,
                            'spec': item})
            return {'available': True, 'balance': balance, 'items': out}
        return await io(load)

    @app.post('/api/app/shop/{item_id}/redeem')
    async def app_shop_redeem(item_id: int, payload: dict = Body(...), ctx=Ctx):  # noqa: B008
        key = str(payload.get('request_id') or '')
        if state.shop is None or not 16 <= len(key) <= 64:
            raise _fail(400, 'bad_request', '请重新确认后兑换')
        try:
            result = await io(state.shop.redeem, ctx['uid'], item_id, 'miniapp',
                              request_id='app:' + key, expected_spec=payload.get('expected_spec'))
        except Exception as exc:  # noqa: BLE001 - the reason is for the member
            raise _fail(409, 'rejected', _public(exc)) from None
        item = result.get('item') or {}
        notice = ''
        b = bot()
        if item.get('kind') == 'custom' and b is not None:
            delivered = await b._send_custom_notice(ctx['member'], result['card_id'])
            if not delivered:
                notice = '使用说明未确认送达，内容已保存，可在「道具」里重新查看。'
        return {'ok': True, 'name': item.get('name'), 'granted': result.get('granted'),
                'cost': result.get('cost'), 'balance': result.get('balance'), 'notice': notice}

    @app.get('/api/app/items')
    async def app_items(ctx=Ctx):
        inventory = getattr(state, 'inventory', None)
        if inventory is None:
            return {'available': False}
        rows = await io(inventory.items, ctx['uid'])
        return {'available': True, 'rows': [
            {'id': r['id'], 'name': r['spec'].get('name') or '道具', 'kind': r['spec'].get('kind'),
             'duration_days': r['spec'].get('duration_days'), 'used_at': r.get('used_at'),
             'expires_at': r.get('expires_at'), 'active': bool(r.get('active')),
             'error': r.get('expiry_error') or ''}
            for r in rows]}

    @app.post('/api/app/items/{card_id}/use')
    async def app_item_use(card_id: int, payload: dict = Body(default={}), ctx=Ctx):  # noqa: B008
        inventory = getattr(state, 'inventory', None)
        if inventory is None:
            raise _fail(503, 'disabled', '背包暂不可用')
        title = str(payload.get('title') or '').strip()
        try:
            rows = await io(inventory.items, ctx['uid'])
            row = next((r for r in rows if r['id'] == card_id), None)
            if row is None:
                raise ValueError('道具不存在或不属于你')
            if (row.get('spec') or {}).get('kind') == 'title_card' and row.get('used_at') is None and not title:
                raise ValueError('请填写称号文字')
            result = await io(inventory.use, ctx['uid'], card_id, title=title)
        except Exception as exc:  # noqa: BLE001 - safely render fulfilment failures
            raise _fail(409, 'rejected', _public(exc)) from None
        b = bot()
        warning = (await b._after_member_change(ctx['member'])).strip() if b is not None else ''
        return {'ok': True, 'granted': result.get('granted'), 'warning': warning}

    @app.get('/api/app/titles')
    async def app_titles(ctx=Ctx):
        titles = getattr(state, 'titles', None)
        if titles is None:
            return {'available': False}

        def load():
            now = time.time()
            rows = [r for r in titles.titles(ctx['uid'])
                    if r['revoked_at'] is None and (r['expires_at'] is None or r['expires_at'] > now)]
            sync = [TITLE_SYNC.get(s['status'], s['status']) + (f" · {s['error']}" if s['error'] else '')
                    for s in titles.states(ctx['uid'])]
            return [{'id': r['id'], 'tag': r['tag'], 'worn': bool(r.get('worn')), 'expires_at': r['expires_at']}
                    for r in rows], sync
        rows, sync = await io(load)
        return {'available': True, 'rows': rows, 'sync': sync}

    @app.post('/api/app/titles/wear')
    async def app_title_wear(payload: dict = Body(...), ctx=Ctx):  # noqa: B008
        titles = getattr(state, 'titles', None)
        if titles is None:
            raise _fail(503, 'disabled', '称号服务暂不可用')
        raw = payload.get('id')
        try:
            await titles.wear(ctx['uid'], None if raw is None else int(raw))
        except Exception as exc:  # noqa: BLE001 - shown to the member
            raise _fail(409, 'rejected', _public(exc)) from None
        return {'ok': True}

    @app.get('/api/app/ledger')
    async def app_ledger(ctx=Ctx):
        rows = await io(state.points.ledger, ctx['uid'], 30)
        return {'balance': await io(state.points.balance, ctx['uid']),
                'rows': [{'delta': r['delta'], 'balance': r['balance_after'],
                          'reason': r.get('reason_label') or r['reason'], 'at': r['created_at']} for r in rows]}

    # -- invites --------------------------------------------------------------------

    def invites_payload(ctx):
        uid = ctx['uid']
        tree = InviteTree(state.members._db, state.members)
        nodes = tree.downstream(uid, 2)
        b = bot()
        profiles = b._tg_profiles if b is not None else {}

        def node(n):
            member = state.members.get(n['emby_user_id']) if n.get('status') != 'deleted' else None
            return {'label': n.get('label') or _rank_label(n, profiles), 'status': n.get('status'),
                    'avatar': avatar_url(n.get('tg_user_id')),
                    'active_7d': n.get('active_7d'), 'registered_at': n.get('registered_at'),
                    'expires_at': n.get('expires_at'), 'deleted_at': n.get('deleted_at'),
                    'tag': _level_tag(member), 'descendants': n.get('descendants', 0),
                    'children': [node(c) for c in n.get('children') or []]}
        registration = getattr(state, 'registration', None)
        quota, codes = None, []
        if registration is not None:
            quota = registration.invite_quota(uid)
            codes = [{'code': r['code'], 'uses_left': int(r.get('uses_left') or 0),
                      'expires_at': r.get('expires_at'), 'usable': bool(r.get('usable')),
                      'revoked': bool(r.get('revoked'))}
                     for r in registration.list_invites(uid, limit=10)]
        paused = active_suspension(tree.db, uid)
        shaped = [node(n) for n in nodes]
        return {'available': registration is not None, 'quota': quota, 'codes': codes,
                'suspended_until': paused['until'] if paused else None,
                'direct': len(shaped),
                'total': len(shaped) + sum(n['descendants'] for n in shaped),
                'nodes': shaped}

    @app.get('/api/app/invites')
    async def app_invites(ctx=Ctx):
        return await io(invites_payload, ctx)

    @app.post('/api/app/invites')
    async def app_invite_mint(ctx=Ctx):
        registration = getattr(state, 'registration', None)
        if registration is None:
            raise _fail(503, 'disabled', '邀请功能暂未开放')
        try:
            issued = await io(registration.spend_quota_for_invite, ctx['uid'])
        except Exception as exc:  # noqa: BLE001 - shown to the member
            raise _fail(409, 'rejected', _public(exc)) from None
        return {'ok': True, 'code': issued.get('code', '')}


TITLE_SYNC = {'synced': '已同步到群', 'pending': '等待同步', 'retry': '网络波动，正在重试',
              'protected': '管理员已手动设置，暂不覆盖', 'failed': '同步失败'}


def _public(exc: Exception) -> str:
    from app.modules.member_ops import redact
    from app.modules.shop import ShopError
    # Only domain refusals are member-facing; others may embed internals.
    if isinstance(exc, (ValueError, ShopError, RequestError)):
        return redact(str(exc)) or '操作失败，请稍后再试'
    return '操作失败，请稍后再试'
