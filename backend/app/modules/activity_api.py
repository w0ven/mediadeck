"""Activity API registration: admin configuration versus member-owned actions."""
from fastapi import Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.modules import activity_web
from app.modules.level_lifecycle import LevelLifecycle
from app.modules.level_migration import LevelMigration
from app.modules.usage import run_usage_io


def register(app, auth, static_dir, sync_effects):
    async def member_auth(request: Request):
        uid = activity_web.resolve(app.state.db, request.cookies.get(activity_web.COOKIE))
        if not uid:
            raise HTTPException(401, '请使用Bot私聊的一次性登录码')
        return uid

    async def call(fn, *args, **kwargs):
        try:
            return await run_usage_io(fn, *args, **kwargs)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get('/activity', response_class=HTMLResponse)
    async def page():
        return HTMLResponse((static_dir/'activity.html').read_text(), headers={'Cache-Control': 'no-store'})

    @app.post('/api/activity/login')
    async def login(request: Request, payload: dict = Body(...)):  # noqa: B008
        token = await call(activity_web.exchange, app.state.db, str(payload.get('code') or ''))
        response = JSONResponse({'ok': True})
        response.set_cookie(activity_web.COOKIE, token, httponly=True, samesite='strict',
                            secure=request.url.scheme == 'https', max_age=3600, path='/api/activity')
        return response

    @app.get('/api/activity/me')
    async def me(uid: str = Depends(member_auth)):
        status = await call(app.state.levels.status, uid)
        member = app.state.members.get(uid)
        return dict(status, username=member['username'], effective=member['effective'])

    @app.post('/api/activity/checkin')
    async def checkin(payload: dict = Body(...), uid: str = Depends(member_auth)):  # noqa: B008
        if not app.state.plugins.enabled('checkin'):
            raise HTTPException(409, '签到未启用')
        return await call(app.state.plugins.get('checkin').checkin, uid, mode=payload.get('mode', 'normal'))

    @app.post('/api/activity/renew')
    async def renew(payload: dict = Body(...), uid: str = Depends(member_auth)):  # noqa: B008
        before = app.state.members.get(uid)
        result = await call(app.state.levels.renew, uid, payload.get('request_id'))
        try:
            effects = await sync_effects(uid, before.get('bandwidth_limit_kbps'))
        except Exception:  # noqa: BLE001 - committed renewal survives remote uncertainty
            effects = {'remote_ok': False, 'error': '本地续费已成功，远端同步未确认'}
        return dict(result, remote_ok=effects.get('remote_ok'), remote_error=effects.get('error', ''))

    @app.post('/api/activity/auto-renew')
    async def auto(payload: dict = Body(...), uid: str = Depends(member_auth)):  # noqa: B008
        return await call(app.state.levels.set_auto, uid, payload.get('enabled'))

    @app.get('/api/activity/config', dependencies=[Depends(auth)])
    async def config():
        return app.state.levels.config()

    @app.put('/api/activity/config', dependencies=[Depends(auth)])
    async def save(payload: dict = Body(...)):  # noqa: B008
        return await call(app.state.levels.save_config, payload.get('config', {}), payload.get('revision'))

    @app.get('/api/activity/migration', dependencies=[Depends(auth)])
    async def migration():
        return await call(LevelMigration(app.state.levels).preview)

    @app.post('/api/activity/migration', dependencies=[Depends(auth)])
    async def migrate(payload: dict = Body(...)):  # noqa: B008
        if payload.get('confirm') is not True:
            raise HTTPException(409, '须明确确认迁移预览')
        return await call(LevelMigration(app.state.levels).apply, payload.get('request_id'),
                          payload.get('hash'), payload.get('as_of'))

    @app.get('/api/activity/deletion-preview', dependencies=[Depends(auth)])
    async def deletions():
        return await call(LevelLifecycle(app.state.levels, app.state.emby).preview)

    @app.post('/api/activity/members/{uid}/recover-deletion', dependencies=[Depends(auth)])
    async def recover(uid: str, payload: dict = Body(...)):  # noqa: B008
        if payload.get('confirm') is not True:
            raise HTTPException(409, '须明确确认恢复删除互斥')
        try:
            return await LevelLifecycle(app.state.levels, app.state.emby).recover(uid)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post('/api/activity/members/{uid}/grant-lv8', dependencies=[Depends(auth)])
    async def grant(uid: str, payload: dict = Body(...), actor: str = Depends(auth)):  # noqa: B008
        if payload.get('confirm') is not True:
            raise HTTPException(409, '须明确确认授予Lv8')
        return await call(app.state.levels.grant_special, uid, actor)
