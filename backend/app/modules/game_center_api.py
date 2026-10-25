"""Member-authenticated HTTP and short-lived, viewer-bound SSE snapshots."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections import Counter

from fastapi import Body, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.modules.economy_rules import encode
from app.modules.game_center import GameError
from app.modules.miniapp import _display_name, _fail
from app.modules.usage import run_usage_io

PREFIX = '/api/app/games'
TOKEN_TTL = 120


class StreamAccessFilter(logging.Filter):
    """Query bearer tokens must not appear in the application's access log."""
    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(re.sub(r'([?&]token=)[^&\s]*', r'\1[redacted]', value)
                                if isinstance(value, str) and value.startswith(PREFIX + '/') else value
                                for value in record.args)
        return True


class StreamTokens:
    def __init__(self, key=None):
        self.key = key or secrets.token_bytes(32)

    def issue(self, uid, tg, table, now=None):
        expires = int(time.time() if now is None else now) + TOKEN_TTL
        data = base64.urlsafe_b64encode(encode({'uid': uid, 'tg': tg, 'table': table, 'exp': expires}).encode()).decode().rstrip('=')
        sig = hmac.new(self.key, data.encode(), hashlib.sha256).hexdigest()
        return data + '.' + sig, expires

    def verify(self, token, table, now=None):
        try:
            if not isinstance(token, str) or len(token) > 2048:
                raise ValueError
            data, sig = token.split('.')
            wanted = hmac.new(self.key, data.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, wanted):
                raise ValueError
            payload = json.loads(base64.b64decode(data + '=' * (-len(data) % 4), altchars=b'-_', validate=True))
            clock = time.time() if now is None else now
            if (payload['table'] != table or type(payload['exp']) is not int
                    or not clock < payload['exp'] <= clock + TOKEN_TTL
                    or not isinstance(payload['uid'], str) or not isinstance(payload['tg'], str)):
                raise ValueError
            return payload
        except (ValueError, KeyError, TypeError):
            raise _fail(401, 'invalid_token', '实时连接凭证失效，请重新连接') from None


async def call(fn, *args, **kwargs):
    try:
        return await run_usage_io(fn, *args, **kwargs)
    except GameError as exc:
        raise _fail(exc.status, exc.code, str(exc)) from None
    except ValueError as exc:
        raise _fail(400, 'invalid', str(exc)) from None


async def stream(service, table_id, uid, valid, expires, disconnected, keepalive=15):
    subscriber = service.subscribe(table_id)
    _, changed = subscriber
    revision = None
    try:
        while time.time() < expires and not await disconnected():
            if not await run_usage_io(valid):
                break
            changed.clear()
            try:
                snapshot = await call(service.snapshot, table_id, uid)
            except HTTPException:
                break
            current = snapshot['table']['revision']
            if current != revision:
                revision = current
                yield f'id: {current}\nevent: snapshot\ndata: {encode(snapshot)}\n\n'
            remaining = min(keepalive, expires - time.time())
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except TimeoutError:
                if time.time() < expires:
                    yield ': keepalive\n\n'
    finally:
        service.unsubscribe(table_id, subscriber)


def register(app, member_context):
    logger = logging.getLogger('uvicorn.access')
    if not any(isinstance(f, StreamAccessFilter) for f in logger.filters):
        logger.addFilter(StreamAccessFilter())

    async def private_context(request: Request, response: Response):
        response.headers['Cache-Control'] = 'private, no-store'
        return await member_context(request)

    ctx_dep = Depends(private_context)
    json_body = Body(...)
    connections = Counter()

    def service():
        return app.state.game_center

    def tokens():
        svc = service()
        if not hasattr(svc, 'stream_tokens'):
            svc.stream_tokens = StreamTokens()
        return svc.stream_tokens

    @app.get(PREFIX + '/lobby')
    async def lobby(ctx=ctx_dep):
        return await call(service().lobby, ctx['uid'])

    @app.get(PREFIX + '/me')
    async def me(ctx=ctx_dep):
        return await call(service().me, ctx['uid'])

    @app.get(PREFIX + '/wallet')
    async def wallet(ctx=ctx_dep):
        return await call(service().wallet, ctx['uid'])

    @app.get(PREFIX + '/{game}/tiers')
    async def game_tiers(game: str, ctx=ctx_dep):
        return await call(service().list_tiers, game)

    async def mutate(ctx, op, body, table_id=None):
        bot = getattr(app.state, 'telegram', None)
        profile = bot._tg_profiles.get(ctx['tg_id']) if bot else None
        name = _display_name(ctx['tg'], profile) or '会员'
        return await call(service().mutate, ctx['uid'], name, op, body, table_id)

    @app.post(PREFIX + '/tables')
    async def create(body: dict = json_body, ctx=ctx_dep):
        return await mutate(ctx, 'create', body)

    @app.post(PREFIX + '/quick-start')
    async def quick(body: dict = json_body, ctx=ctx_dep):
        return await mutate(ctx, 'quick', body)

    @app.get(PREFIX + '/tables/{table_id}')
    async def table(table_id: str, ctx=ctx_dep):
        return await call(service().snapshot, table_id, ctx['uid'])

    @app.get(PREFIX + '/tables/{table_id}/hands/{hand_no}/verify')
    async def verify(table_id: str, hand_no: int, ctx=ctx_dep):
        return await call(service().verify, table_id, hand_no, ctx['uid'])

    @app.post(PREFIX + '/tables/{table_id}/stream-token')
    async def stream_token(table_id: str, ctx=ctx_dep):
        await call(service().snapshot, table_id, ctx['uid'])
        token, expires = tokens().issue(ctx['uid'], ctx['tg_id'], table_id)
        return {'token': token, 'expires_at': expires,
                'url': f'{PREFIX}/tables/{table_id}/events?token={token}'}

    @app.get(PREFIX + '/tables/{table_id}/events')
    async def events(table_id: str, request: Request, token: str = ''):
        claim = tokens().verify(token, table_id)
        svc = service()
        def valid():
            member = svc.members.find_by_telegram(claim['tg'])
            return bool(member and str(member['emby_user_id']) == claim['uid'])
        if not await run_usage_io(valid):
            raise _fail(401, 'invalid_token', '实时连接凭证失效，请重新连接')
        await call(svc.snapshot, table_id, claim['uid'])
        key = (table_id, claim['uid'])
        if connections[key] >= 3:
            raise _fail(429, 'too_many_streams', '实时连接过多，请关闭其他窗口')
        connections[key] += 1
        async def iterate():
            try:
                async for event in stream(svc, table_id, claim['uid'], valid, claim['exp'], request.is_disconnected):
                    yield event
            finally:
                connections[key] -= 1
                if not connections[key]:
                    del connections[key]
        return StreamingResponse(iterate(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    # Static SSE route is registered first so it cannot be mistaken for a mutation.
    @app.post(PREFIX + '/tables/{table_id}/{operation}')
    async def operation(table_id: str, operation: str, body: dict = json_body, ctx=ctx_dep):
        if operation not in ('seat', 'leave', 'ready', 'start', 'auto', 'actions', 'away', 'top-up'):
            raise _fail(404, 'not_found', '操作不存在')
        return await mutate(ctx, operation, body, table_id)


def register_admin(app, auth):
    @app.get('/api/game-center/cash', dependencies=[Depends(auth)])
    async def cash(response: Response, table_id: str = '', user_id: str = '', limit: int = 100):
        response.headers['Cache-Control'] = 'private, no-store'
        return await call(app.state.game_center.cash_detail, table_id, user_id, limit)
