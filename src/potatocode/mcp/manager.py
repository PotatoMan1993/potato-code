"""MCP server 并发连接与生命周期管理。"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from typing import Any

import mcp.types as mtypes
from mcp import ClientSession, StdioServerParameters
from mcp.client import streamable_http as _streamable_http
from mcp.client.stdio import stdio_client

from .config import Config, ServerConfig
from .tool import McpTool, adapt_tool

logger = logging.getLogger(__name__)

# SDK 2.x 使用 streamable_http_client，1.x 使用 streamablehttp_client。
create_mcp_http_client: Any = getattr(_streamable_http, "create_mcp_http_client", None)
streamable_http_client: Any = getattr(_streamable_http, "streamable_http_client", None)
if streamable_http_client is None:  # pragma: no cover - 仅 MCP 1.x
    streamable_http_client = vars(_streamable_http)["streamablehttp_client"]

connect_timeout: float = 30.0
close_timeout: float = 5.0


@dataclass
class _Session:
    name: str
    session: ClientSession


@dataclass
class _Worker:
    stop: asyncio.Event
    task: asyncio.Task[None]


class Manager:
    """持有成功建立的会话、工具快照及其后台生命周期任务。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sessions: list[_Session] = []
        self._tools: list[McpTool] = []
        # 供 Manager 自身资源与测试注入使用；SDK transport 由各 worker 同任务退栈。
        self._stack = AsyncExitStack()
        self._workers: list[_Worker] = []
        self._closed = False

    def tools(self) -> list[McpTool]:
        """返回启动时固定下来的工具快照副本。"""
        return list(self._tools)

    async def close(self) -> None:
        """关闭全部 server；单个关闭卡住时最多等待 close_timeout。"""
        if self._closed:
            return
        self._closed = True

        async def close_all() -> None:
            for worker in self._workers:
                worker.stop.set()
            if self._workers:
                await asyncio.gather(
                    *(worker.task for worker in self._workers), return_exceptions=True
                )
            await self._stack.aclose()

        try:
            await asyncio.wait_for(close_all(), timeout=close_timeout)
        except TimeoutError:
            for worker in self._workers:
                worker.task.cancel()
            logger.warning(f"close timeout ({close_timeout:g}s), some sessions may leak")
        except Exception as exc:
            logger.warning(f"close failed: {exc}")


async def new_manager(cfg: Config, version: str) -> Manager:
    """并发连接全部配置项，失败或超时仅跳过对应 server。"""
    manager = Manager()
    await manager._stack.__aenter__()
    tasks = [
        asyncio.create_task(_connect_one(manager, name, server, version))
        for name, server in cfg.servers.items()
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    manager._tools.sort(key=lambda tool: tool.full_name)
    return manager


async def _connect_one(manager: Manager, name: str, server: ServerConfig, version: str) -> None:
    try:
        await asyncio.wait_for(_do_connect(manager, name, server, version), timeout=connect_timeout)
    except TimeoutError:
        logger.warning(f"connect server {name} timeout after {connect_timeout:g}s")
    except Exception as exc:
        logger.warning(f"connect server {name} failed: {exc}")


async def _do_connect(manager: Manager, name: str, server: ServerConfig, version: str) -> None:
    """启动会话 owner task，并等待其完成握手与工具发现。"""
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[tuple[ClientSession, list[McpTool]]] = loop.create_future()
    stop = asyncio.Event()
    task = asyncio.create_task(_session_worker(name, server, version, ready, stop))

    try:
        session, tools = await ready
    except BaseException:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        raise

    async with manager._lock:
        manager._sessions.append(_Session(name, session))
        manager._tools.extend(tools)
        manager._workers.append(_Worker(stop, task))
    logger.info(f"connected server {name}: {len(tools)} tool(s)")


async def _session_worker(
    name: str,
    server: ServerConfig,
    version: str,
    ready: asyncio.Future[tuple[ClientSession, list[McpTool]]],
    stop: asyncio.Event,
) -> None:
    """在同一 task 中进入并退出 SDK context，满足 AnyIO 的作用域约束。"""
    try:
        async with AsyncExitStack() as stack:
            if server.type == "stdio":
                params = StdioServerParameters(
                    command=server.command,
                    args=server.args,
                    env={**os.environ, **server.env},
                )
                transport = await stack.enter_async_context(stdio_client(params))
            else:
                if create_mcp_http_client is None:
                    # MCP 1.x 直接在 transport 上传 headers。
                    transport = await stack.enter_async_context(
                        streamable_http_client(server.url, headers=server.headers or None)
                    )
                else:
                    # MCP 2.x 通过预配置的 httpx2 client 为每次请求附加 headers。
                    http_client = await stack.enter_async_context(
                        create_mcp_http_client(server.headers or None)
                    )
                    transport = await stack.enter_async_context(
                        streamable_http_client(server.url, http_client=http_client)
                    )

            read, write = transport[0], transport[1]
            session = await stack.enter_async_context(
                ClientSession(
                    read,
                    write,
                    client_info=mtypes.Implementation(name="potatocode", version=version),
                )
            )
            await session.initialize()
            listed = await session.list_tools()
            tools = _adapt_tools(name, listed.tools, session)
            if not ready.done():
                ready.set_result((session, tools))
            await stop.wait()
    except asyncio.CancelledError:
        if not ready.done():
            ready.cancel()
        raise
    except BaseException as exc:
        if not ready.done():
            ready.set_exception(exc)


def _adapt_tools(name: str, remote_tools: list[Any], session: ClientSession) -> list[McpTool]:
    """适配并处理同一 server 自报重名工具的边界情形。"""
    tools: dict[str, McpTool] = {}
    for remote_tool in remote_tools:
        tool = adapt_tool(name, remote_tool, session)
        if tool is None:
            continue
        if tool.full_name in tools:
            logger.warning(f"duplicate tool {tool.full_name}: keeping later definition")
        tools[tool.full_name] = tool
    return list(tools.values())


__all__ = ["Manager", "new_manager"]
