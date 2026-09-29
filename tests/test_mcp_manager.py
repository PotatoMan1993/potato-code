"""MCP Manager 的并发、失败隔离、传输装配与退出测试。"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
from contextlib import asynccontextmanager

import mcp.types as mtypes
import pytest

from potatocode.mcp import Config, ServerConfig, new_manager
from potatocode.mcp import manager as manager_module
from potatocode.mcp.tool import McpTool


class EmptyCaller:
    async def call_tool(self, name, arguments=None):
        return mtypes.CallToolResult(content=[])


def _fake_tool(name: str) -> McpTool:
    return McpTool(
        full_name=f"mcp__{name}__echo",
        remote_name="echo",
        tool_description="echo",
        input_schema={"type": "object"},
        readonly=False,
        caller=EmptyCaller(),
    )


@pytest.mark.asyncio
async def test_empty_config_starts_and_closes():
    manager = await new_manager(Config(), "test")

    assert manager.tools() == []
    await manager.close()
    await manager.close()


@pytest.mark.asyncio
async def test_failures_are_isolated_and_results_are_sorted(monkeypatch, pkg_logs):
    async def fake_connect(manager, name, server, version):
        if name == "broken":
            raise RuntimeError("cannot connect")
        await asyncio.sleep(0.02 if name == "alpha" else 0)
        async with manager._lock:
            manager._tools.append(_fake_tool(name))

    monkeypatch.setattr(manager_module, "_do_connect", fake_connect)
    config = Config(
        servers={
            "broken": ServerConfig(type="stdio", command="missing"),
            "zeta": ServerConfig(type="stdio", command="zeta"),
            "alpha": ServerConfig(type="stdio", command="alpha"),
        }
    )

    manager = await new_manager(config, "test")

    assert [tool.name() for tool in manager.tools()] == [
        "mcp__alpha__echo",
        "mcp__zeta__echo",
    ]
    assert "connect server broken failed: cannot connect" in pkg_logs.text()
    await manager.close()


@pytest.mark.asyncio
async def test_connect_timeout_does_not_block_startup(monkeypatch, pkg_logs):
    async def blocked(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(manager_module, "_do_connect", blocked)
    monkeypatch.setattr(manager_module, "connect_timeout", 0.02)
    config = Config(servers={"slow": ServerConfig(type="stdio", command="slow")})

    manager = await asyncio.wait_for(new_manager(config, "test"), timeout=0.2)

    assert manager.tools() == []
    assert "timeout after 0.02s" in pkg_logs.text()
    await manager.close()


@pytest.mark.asyncio
async def test_close_timeout_is_bounded(monkeypatch, pkg_logs):
    @asynccontextmanager
    async def blocked_context():
        try:
            yield
        finally:
            await asyncio.Event().wait()

    monkeypatch.setattr(manager_module, "close_timeout", 0.02)
    manager = await new_manager(Config(), "test")
    await manager._stack.enter_async_context(blocked_context())

    await asyncio.wait_for(manager.close(), timeout=0.2)

    assert "close timeout (0.02s)" in pkg_logs.text()


@pytest.mark.asyncio
async def test_http_headers_are_attached_to_transport(monkeypatch):
    observed: dict[str, object] = {}
    stop_reached = asyncio.Event()

    @asynccontextmanager
    async def fake_http_client(headers):
        observed["headers"] = headers
        yield "http-client"

    @asynccontextmanager
    async def fake_transport(url, *, http_client):
        observed["url"] = url
        observed["client"] = http_client
        yield "read", "write"

    class FakeSession:
        def __init__(self, read, write, client_info):
            observed["implementation"] = client_info

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            stop_reached.set()

        async def initialize(self):
            observed["initialized"] = True

        async def list_tools(self):
            return mtypes.ListToolsResult(tools=[])

    monkeypatch.setattr(manager_module, "create_mcp_http_client", fake_http_client)
    monkeypatch.setattr(manager_module, "streamable_http_client", fake_transport)
    monkeypatch.setattr(manager_module, "ClientSession", FakeSession)
    config = Config(
        servers={
            "remote": ServerConfig(
                type="http",
                url="https://example.test/mcp",
                headers={"Authorization": "Bearer test-value"},
            )
        }
    )

    manager = await new_manager(config, "9.9")

    assert observed["headers"] == {"Authorization": "Bearer test-value"}
    assert observed["url"] == "https://example.test/mcp"
    assert observed["client"] == "http-client"
    assert observed["initialized"] is True
    assert observed["implementation"].version == "9.9"
    await manager.close()
    assert stop_reached.is_set()


@pytest.mark.asyncio
async def test_real_stdio_server_handshake_env_call_and_close(tmp_path):
    server_script = tmp_path / "server.py"
    server_script.write_text(
        """
import os
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

app = MCPServer("potatocode-test")

@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
def read_env(name: str) -> str:
    return os.environ.get(name, "missing")

if __name__ == "__main__":
    app.run(transport="stdio")
""",
        encoding="utf-8",
    )
    config = Config(
        servers={
            "local": ServerConfig(
                type="stdio",
                command=sys.executable,
                args=[str(server_script)],
                env={"POTATOCODE_MCP_TEST": "injected"},
            )
        }
    )

    manager = await new_manager(config, "test")

    assert [tool.name() for tool in manager.tools()] == ["mcp__local__read_env"]
    assert manager.tools()[0].read_only() is True
    result = await manager.tools()[0].execute(json.dumps({"name": "POTATOCODE_MCP_TEST"}))
    assert result.content == "injected"
    assert result.is_error is False
    workers = [worker.task for worker in manager._workers]
    await manager.close()
    assert all(worker.done() for worker in workers)


@pytest.mark.asyncio
async def test_real_http_server_headers_call_and_delete(tmp_path):
    server_script = tmp_path / "http_server.py"
    request_log = tmp_path / "requests.log"
    server_script.write_text(
        """
import sys
from pathlib import Path

import uvicorn
from mcp.server.mcpserver import MCPServer

port = int(sys.argv[1])
log_path = Path(sys.argv[2])
server = MCPServer("potatocode-http-test")

@server.tool()
def echo(text: str) -> str:
    return text

inner = server.streamable_http_app(
    streamable_http_path="/mcp",
    json_response=True,
    host="127.0.0.1",
)

async def capture_headers(scope, receive, send):
    if scope["type"] == "http":
        headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(f'{scope["method"]} {headers.get("authorization", "")}\\n')
    await inner(scope, receive, send)

uvicorn.run(capture_headers, host="127.0.0.1", port=port, log_level="warning")
""",
        encoding="utf-8",
    )

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, str(server_script), str(port), str(request_log)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    manager = None
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if process.poll() is not None:
                    pytest.fail("HTTP MCP test server exited before accepting connections")
                await asyncio.sleep(0.02)
        else:
            pytest.fail("HTTP MCP test server did not start")

        manager = await new_manager(
            Config(
                servers={
                    "remote": ServerConfig(
                        type="http",
                        url=f"http://127.0.0.1:{port}/mcp",
                        headers={"Authorization": "Bearer test-value"},
                    )
                }
            ),
            "test",
        )
        assert [tool.name() for tool in manager.tools()] == ["mcp__remote__echo"]
        result = await manager.tools()[0].execute('{"text": "hello over http"}')
        assert result.content == "hello over http"
        await manager.close()
        manager = None

        requests = request_log.read_text(encoding="utf-8")
        assert "POST Bearer test-value" in requests
        assert "DELETE Bearer test-value" in requests
    finally:
        if manager is not None:
            await manager.close()
        process.terminate()
        process.wait(timeout=5)
