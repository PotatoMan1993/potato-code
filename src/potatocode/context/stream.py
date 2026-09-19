"""竞争网络流读取与用户取消，保证静默连接也能及时停止。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator

from ..llm import Provider, Request, StreamEvent


async def cancellable_stream(
    provider: Provider, request: Request, cancel: asyncio.Event
) -> AsyncGenerator[StreamEvent, None]:
    stream = provider.stream(request)
    cancelled = asyncio.create_task(cancel.wait())
    pending: asyncio.Task[StreamEvent] | None = None
    try:
        while True:
            if cancel.is_set():
                raise asyncio.CancelledError
            pending = asyncio.create_task(_next(stream))
            done, _ = await asyncio.wait({pending, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done or cancel.is_set():
                raise asyncio.CancelledError
            try:
                event = pending.result()
            except StopAsyncIteration:
                break
            pending = None
            yield event
    finally:
        cancelled.cancel()
        if pending is not None:
            pending.cancel()
        await asyncio.gather(
            cancelled, *([pending] if pending is not None else []), return_exceptions=True
        )
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()


async def _next(stream: AsyncIterator[StreamEvent]) -> StreamEvent:
    return await anext(stream)
