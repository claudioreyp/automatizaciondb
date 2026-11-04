"""Keep synchronous ORM locks off ASGI while private storage awaits completion.

These small transactions use a synchronous Session throughout. A worker owns
their sequential database/storage work; the main event loop remains available
to complete other requests. Websocket notifications stay on their original loop.
"""
import asyncio
from functools import wraps

from starlette.concurrency import run_in_threadpool


async def run_private_transaction(operation, *args, **kwargs):
    def run():
        return asyncio.run(operation(*args, **kwargs))
    return await run_in_threadpool(run)


def private_transaction(operation):
    @wraps(operation)
    async def offloaded(*args, **kwargs):
        return await run_private_transaction(operation, *args, **kwargs)
    return offloaded
