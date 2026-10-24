"""Keep embedded Gym HTTP requests out of their dispatcher's ContextVars."""

import asyncio
from contextvars import Context


class RequestContextIsolation:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        task = asyncio.create_task(self.app(scope, receive, send), context=Context())
        return await task
