import asyncio
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
import uvicorn

from scripts.sdk_smoke import EmbeddedServer, bound_socket, shutdown


async def test_shutdown_aborts_held_model_request_before_closing_native_owner():
    entered, aborted = asyncio.Event(), asyncio.Event()
    app = FastAPI()

    @app.post('/held')
    async def held():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            aborted.set()

    events = []

    class Owner:
        async def aclose(self):
            assert aborted.is_set()
            events.append('native_closed')

    class Shared:
        async def close(self):
            events.append('http_closed')

    sock = bound_socket()
    server = EmbeddedServer(uvicorn.Config(app, log_level='critical', access_log=False,
                                          timeout_graceful_shutdown=1))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    request = None
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(.01)
            async with httpx.AsyncClient() as client:
                request = asyncio.create_task(client.post(f'http://127.0.0.1:{sock.getsockname()[1]}/held'))
                await entered.wait()
                await shutdown({'model': server}, [task],
                               SimpleNamespace(_allocations={'owned': Owner()}), Shared())
                await asyncio.gather(request, return_exceptions=True)
        assert task.done() and events == ['native_closed', 'http_closed']
    finally:
        server.should_exit = True
        if request is not None:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        await task
        sock.close()
