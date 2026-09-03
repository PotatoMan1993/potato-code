"""MCP 工具命名、字段适配与调用结果测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import mcp.types as mtypes
import pytest

from potatocode.mcp import tool as tool_module
from potatocode.mcp.tool import McpTool, adapt_tool


class StubSession:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, dict | None]] = []

    async def call_tool(self, name: str, arguments: dict | None = None):
        self.calls.append((name, arguments))
        if self.error is not None:
            raise self.error
        return self.result


def _remote_tool(
    *,
    name: str = "echo",
    description: str | None = "Echo text",
    schema: dict | None = None,
    read_only: bool | None = None,
):
    annotations = None
    if read_only is not None:
        annotations = mtypes.ToolAnnotations(readOnlyHint=read_only)
    return mtypes.Tool(
        name=name,
        description=description,
        inputSchema=schema if schema is not None else {"type": "object"},
        annotations=annotations,
    )


def _result(*blocks, is_error: bool = False):
    return mtypes.CallToolResult(content=list(blocks), isError=is_error)


def test_adapt_tool_fields_and_read_only():
    schema = {"type": "object", "properties": {"text": {"type": "string"}}}
    session = StubSession()
    adapted = adapt_tool(
        "demo", _remote_tool(description="", schema=schema, read_only=True), session
    )

    assert isinstance(adapted, McpTool)
    assert adapted.name() == "mcp__demo__echo"
    assert "demo" in adapted.description()
    assert adapted.parameters() == schema
    assert adapted.parameters() is not schema
    assert adapted.read_only() is True


def test_schema_and_annotations_are_none_safe():
    session = StubSession()
    remote = SimpleNamespace(name="plain", description=None, input_schema=None, annotations=None)

    adapted = adapt_tool("demo", remote, session)

    assert adapted is not None
    assert adapted.parameters() == {"type": "object"}
    assert adapted.read_only() is False


@pytest.mark.parametrize(("server", "name"), [("bad.server", "tool"), ("server", "bad@tool")])
def test_illegal_full_name_is_skipped(server, name, capsys):
    assert adapt_tool(server, _remote_tool(name=name), StubSession()) is None
    assert "name contains illegal characters" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_execute_joins_text_and_passes_decoded_arguments():
    session = StubSession(
        _result(mtypes.TextContent(text="first"), mtypes.TextContent(text="second"))
    )
    tool = adapt_tool("demo", _remote_tool(), session)
    assert tool is not None

    result = await tool.execute('{"value": 7}')

    assert result.content == "first\nsecond"
    assert result.is_error is False
    assert session.calls == [("echo", {"value": 7})]


@pytest.mark.asyncio
async def test_execute_maps_remote_error_and_keeps_text():
    session = StubSession(_result(mtypes.TextContent(text="remote failed"), is_error=True))
    tool = adapt_tool("demo", _remote_tool(), session)
    assert tool is not None

    result = await tool.execute("{}")

    assert result.content == "remote failed"
    assert result.is_error is True


@pytest.mark.asyncio
async def test_execute_treats_empty_object_as_no_arguments():
    session = StubSession(_result())
    tool = adapt_tool("demo", _remote_tool(), session)
    assert tool is not None

    result = await tool.execute("{}")

    assert result.is_error is False
    assert session.calls == [("echo", None)]


@pytest.mark.asyncio
async def test_execute_turns_protocol_error_into_result():
    tool = adapt_tool("demo", _remote_tool(), StubSession(error=RuntimeError("disconnected")))
    assert tool is not None

    result = await tool.execute("{}")

    assert result.is_error is True
    assert "MCP 工具调用失败" in result.content
    assert "disconnected" in result.content


@pytest.mark.asyncio
async def test_execute_timeout_is_structured(monkeypatch):
    class BlockingSession:
        async def call_tool(self, name, arguments=None):
            await asyncio.Event().wait()

    monkeypatch.setattr(tool_module, "call_timeout", 0.02)
    tool = adapt_tool("demo", _remote_tool(), BlockingSession())
    assert tool is not None

    result = await tool.execute("{}")

    assert result.is_error is True
    assert "超时" in result.content


@pytest.mark.asyncio
async def test_non_text_is_dropped_and_warned_once(capsys):
    tool_module._non_text_warn_once.clear()
    mixed = _result(
        mtypes.TextContent(text="visible"),
        mtypes.ImageContent(data="aGVsbG8=", mimeType="image/png"),
    )
    tool = adapt_tool("demo", _remote_tool(), StubSession(mixed))
    assert tool is not None

    first = await tool.execute("{}")
    second = await tool.execute("{}")

    assert first.content == second.content == "visible"
    assert capsys.readouterr().err.count("non-text content") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["not-json", "[]"])
async def test_invalid_arguments_do_not_reach_server(args):
    session = StubSession(_result())
    tool = adapt_tool("demo", _remote_tool(), session)
    assert tool is not None

    result = await tool.execute(args)

    assert result.is_error is True
    assert "参数" in result.content
    assert session.calls == []
