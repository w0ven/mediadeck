"""Only signed Telegram identities; no member-data bypass for lottery guests."""
import json
from contextlib import contextmanager

from fastapi import Body, Depends, HTTPException, Request

from app.core.config import settings
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
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'")) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get('/api/app/lottery/context')
    async def context(ctx=identity):
        return {'is_admin': ctx['is_admin'], 'registered': bool(ctx['member']),
                'name': str(ctx['tg'].get('first_name') or 'Telegram用户'),
                'bot_username': app.state.telegram._bot_username}

    @app.get('/api/app/lottery/admin/options')
    async def options(ctx=manager):
        return {'chats': app.state.telegram._group_allowlist(),
                'groups': [{'id': g['id'], 'name': g['name']} for g in app.state.groups.list()],
                'items': service().shop.items(enabled_only=True),
                'ip_mode_configured': settings().mediadeck_lottery_ip_mode == 'cloudflare_protected'}

    @app.get('/api/app/lottery/admin/list')
    async def listing(offset: int = 0, ctx=manager):
        rows = service().db.query('SELECT * FROM lotteries ORDER BY created_at DESC,id DESC LIMIT 51 OFFSET ?',
                                  (max(0, min(offset, 100000)),))
        return {'more': len(rows) > 50, 'items': [
            {'id': r['id'], 'state': r['state'], 'title': json.loads(r['config_json'])['title'],
             'deadline': json.loads(r['config_json'])['deadline']} for r in rows[:50]]}

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

    @app.post('/api/app/lottery/{lid}/join')
    async def join(lid: str, request: Request, raw: dict = join_payload, ctx=identity):
        with errors():
            await service().join(lid, ctx['tg'],
                                 source_ip=trusted_ip(request, settings().mediadeck_lottery_ip_mode),
                                 browser_id=raw.get('browser_id'))
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
