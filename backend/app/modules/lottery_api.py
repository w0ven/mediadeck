"""Only signed Telegram identities; no member-data bypass for lottery guests."""
import json
from contextlib import contextmanager

from fastapi import Body, Depends, HTTPException, Request

from app.core.config import settings
from app.modules.lottery_captcha import CaptchaError
from app.modules.lottery_identity import trusted_ip


def register(app, telegram_context):
    identity = Depends(telegram_context)
    payload = Body(...)
    join_payload = Body(default={})

    async def admin(ctx=identity):
        if not ctx['is_admin']:
            raise HTTPException(403, '仅管理员可操作抽奖管理')
        return ctx

    manager = Depends(admin)

    def service():
        return app.state.lottery

    @contextmanager
    def errors():
        try:
            yield
        except CaptchaError as exc:
            raise HTTPException(exc.status, str(exc),
                                headers={'Retry-After': str(exc.retry_after)} if exc.retry_after else None) from None
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'")) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get('/api/app/lottery/context')
    async def context(ctx=identity):
        return {'is_admin': ctx['is_admin'], 'registered': bool(ctx['member']),
                'name': str(ctx['tg'].get('first_name') or 'Telegram用户'),
                'bot_username': app.state.telegram._bot_username,
                'points_balance': max(0, service().points.balance(ctx['member']['emby_user_id'])) if ctx['member'] else None}

    @app.get('/api/app/lottery/ongoing')
    async def ongoing(ctx=identity):
        # Member home: signed initData only (registered or not); local data only.
        return service().ongoing(ctx['tg_id'])

    @app.get('/api/app/lottery/admin/options')
    async def options(ctx=manager):
        return {'chats': app.state.telegram._group_allowlist(),
                'groups': [{'id': g['id'], 'name': g['name']} for g in app.state.groups.list()],
                'items': service().shop.items(enabled_only=True),
                'ip_mode_configured': settings().mediadeck_lottery_ip_mode == 'cloudflare_protected'}

    @app.get('/api/app/lottery/admin/list')
    async def listing(offset: int = 0, ctx=manager):
        rows = service().db.query('SELECT l.*,(SELECT COUNT(*) FROM lottery_entries e WHERE e.lottery_id=l.id) AS count '
                                  'FROM lotteries l ORDER BY created_at DESC,id DESC LIMIT 51 OFFSET ?',
                                  (max(0, min(offset, 100000)),))
        items = []
        for r in rows[:50]:
            c = json.loads(r['config_json'])
            items.append({'id': r['id'], 'state': r['state'], 'title': c['title'], 'deadline': c['deadline'],
                          'draw_mode': c['draw_mode'], 'max_entries': c.get('max_entries', 0), 'count': r['count']})
        return {'more': len(rows) > 50, 'items': items}

    @app.post('/api/app/lottery/admin/drafts')
    async def create(raw: dict = payload, ctx=manager):
        with errors():
            lid = service().save(raw, ctx['tg_id'])
            return service().view(lid, ctx['tg_id'], True)

    @app.put('/api/app/lottery/{lid}')
    async def update(lid: str, raw: dict = payload, ctx=manager):
        with errors():
            service().save(raw, ctx['tg_id'], lid, raw.get('revision'))
            return service().view(lid, ctx['tg_id'], True)

    @app.get('/api/app/lottery/{lid}')
    async def detail(lid: str, ctx=identity):
        with errors():
            return service().view(lid, ctx['tg_id'], ctx['is_admin'])

    @app.get('/api/app/lottery/{lid}/records')
    async def records(lid: str, offset: int = 0, ctx=manager):
        with errors():
            return service().records(lid, max(0, min(offset, 1000000)))

    @app.post('/api/app/lottery/{lid}/captcha')
    async def captcha(lid: str, request: Request, ctx=identity):
        with errors():
            return service().captcha_challenge(lid, ctx['tg'],
                source_ip=trusted_ip(request, settings().mediadeck_lottery_ip_mode))

    @app.post('/api/app/lottery/{lid}/join')
    async def join(lid: str, request: Request, raw: dict = join_payload, ctx=identity):
        with errors():
            await service().join(lid, ctx['tg'],
                                 source_ip=trusted_ip(request, settings().mediadeck_lottery_ip_mode),
                                 browser_id=raw.get('browser_id'),
                                 consent_version=raw.get('consent_version'), device=raw.get('ctx'),
                                 captcha_token=raw.get('captcha_token'), captcha_challenge_id=raw.get('captcha_challenge_id'),
                                 **({'expected_entry_fee': raw['expected_entry_fee']} if 'expected_entry_fee' in raw else {}))
            return service().view(lid, ctx['tg_id'], ctx['is_admin'])

    @app.post('/api/app/lottery/{lid}/claim')
    async def claim(lid: str, ctx=identity):
        with errors():
            service().row(lid)
            row = service().db.one('SELECT id FROM lottery_awards WHERE lottery_id=? AND tg_id=?', (lid, ctx['tg_id']))
            if not row:
                raise HTTPException(404, '没有属于你的中奖记录')
            service().deliver_award(row['id'])
            return service().view(lid, ctx['tg_id'], ctx['is_admin'])

    @app.post('/api/app/lottery/{lid}/preview')
    async def preview(lid: str, ctx=manager):
        with errors():
            row = service().row(lid)
            # No network/send, no credential leak. Render native Telegram tags
            # as text/images only in the local preview, never send img HTML.
            return {'text': service().notice_payload(row, 'launch')['text'], 'rules': service().view(lid, ctx['tg_id'], True)['rules']}

    @app.post('/api/app/lottery/{lid}/publish')
    async def publish(lid: str, raw: dict = payload, ctx=manager):
        with errors():
            await service().publish(lid, raw.get('revision'), ctx['tg_id'])
            return service().view(lid, ctx['tg_id'], True)

    @app.post('/api/app/lottery/{lid}/draw')
    async def draw(lid: str, ctx=manager):
        with errors():
            service().draw(lid, ctx['tg_id'])
            service().deliver_awards(lid)
            await service().deliver_notices(lid)
            await service().deliver_dms(lid)
            await service().deliver_ops(lid)
            return service().view(lid, ctx['tg_id'], True)

    @app.post('/api/app/lottery/{lid}/copy')
    async def copy_draft(lid: str, ctx=manager):
        with errors():
            return service().view(service().copy(lid, ctx['tg_id']), ctx['tg_id'], True)

    @app.post('/api/app/lottery/{lid}/delete')
    async def delete_draft(lid: str, raw: dict = payload, ctx=manager):
        with errors():
            service().delete(lid, ctx['tg_id'], confirm=raw.get('confirm'))
            return {'deleted': True, 'id': lid}

    @app.post('/api/app/lottery/{lid}/cancel')
    async def cancel(lid: str, raw: dict = payload, ctx=manager):
        with errors():
            service().cancel(lid, ctx['tg_id'], confirm=raw.get('confirm'))
            await service().deliver_notices(lid)
            await service().deliver_ops(lid)
            return service().view(lid, ctx['tg_id'], True)

    @app.post('/api/app/lottery/{lid}/retry-awards')
    async def retry_awards(lid: str, ctx=manager):
        with errors():
            service().row(lid)
            service().deliver_awards(lid)
            return service().view(lid, ctx['tg_id'], True)

    @app.post('/api/app/lottery/{lid}/awards/{award_id}/confirm')
    async def confirm(lid: str, award_id: int, ctx=manager):
        with errors():
            service().confirm_manual(lid, award_id, ctx['tg_id'])
            return service().view(lid, ctx['tg_id'], True)

    @app.post('/api/app/lottery/{lid}/notices/{notice_id}/recover')
    async def recover(lid: str, notice_id: int, raw: dict = payload, ctx=manager):
        with errors():
            service().recover_notice(lid, notice_id, raw, ctx['tg_id'])
            await service().deliver_notices(lid)
            return service().view(lid, ctx['tg_id'], True)
