"""Administrator-only activity configuration, migration and recovery endpoints.

Members interact exclusively through Telegram; no member HTTP session or action API.
"""
from fastapi import Body, Depends, HTTPException

from app.modules.level_lifecycle import LevelLifecycle
from app.modules.level_migration import LevelMigration
from app.modules.usage import run_usage_io


def register(app, auth):
    async def call(fn, *args, **kwargs):
        try:
            return await run_usage_io(fn, *args, **kwargs)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

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
