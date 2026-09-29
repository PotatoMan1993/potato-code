"""把 MCP SDK 工具适配为 PotatoCode 的 Tool 协议。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

import mcp.types as mtypes

from ..tool import Result

logger = logging.getLogger(__name__)

call_timeout: float = 30.0

_VALID_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_non_text_warn_once: set[str] = set()


class CallerSession(Protocol):
    """MCP 会话中 McpTool 唯一需要的能力。"""

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> mtypes.CallToolResult: ...


@dataclass
class McpTool:
    """一个带来源命名空间的远端 MCP 工具。"""

    full_name: str
    remote_name: str
    tool_description: str
    input_schema: dict[str, Any]
    readonly: bool
    caller: CallerSession

    def name(self) -> str:
        return self.full_name

    def description(self) -> str:
        return self.tool_description

    def parameters(self) -> dict[str, Any]:
        return self.input_schema

    def read_only(self) -> bool:
        return self.readonly

    async def execute(self, args: str) -> Result:
        """调用远端工具，把所有错误转换成可回灌给模型的 Result。"""
        try:
            decoded = json.loads(args) if args and args.strip() else None
            if decoded is not None and not isinstance(decoded, dict):
                raise ValueError("参数必须是 JSON 对象")
        except (json.JSONDecodeError, ValueError) as exc:
            return Result(content=f"MCP 工具参数错误: {exc}", is_error=True)

        arguments = decoded or None
        try:
            result = await asyncio.wait_for(
                self.caller.call_tool(self.remote_name, arguments),
                timeout=call_timeout,
            )
        except TimeoutError:
            return Result(content=f"MCP 工具调用超时 ({call_timeout:g}s)", is_error=True)
        except Exception as exc:
            return Result(content=f"MCP 工具调用失败: {exc}", is_error=True)

        texts: list[str] = []
        dropped_non_text = False
        for block in result.content:
            if isinstance(block, mtypes.TextContent):
                texts.append(block.text)
            else:
                dropped_non_text = True

        if dropped_non_text and self.full_name not in _non_text_warn_once:
            _non_text_warn_once.add(self.full_name)
            logger.warning(f"tool {self.full_name} returned non-text content blocks (dropped)")

        is_error = getattr(result, "is_error", getattr(result, "isError", False))
        return Result(content="\n".join(texts), is_error=is_error is True)


def _schema(tool: mtypes.Tool) -> dict[str, Any]:
    value = getattr(tool, "input_schema", None)
    if value is None:
        value = getattr(tool, "inputSchema", None)
    return dict(value) if isinstance(value, dict) and value else {"type": "object"}


def _read_only(tool: mtypes.Tool) -> bool:
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return False
    hint = getattr(annotations, "read_only_hint", None)
    if hint is None:
        hint = getattr(annotations, "readOnlyHint", None)
    return hint is True


def adapt_tool(server_name: str, tool: mtypes.Tool, session: CallerSession) -> McpTool | None:
    """校验并适配一个远端工具；非法名称不会暴露给 LLM。"""
    full_name = f"mcp__{server_name}__{tool.name}"
    if _VALID_NAME.fullmatch(full_name) is None:
        logger.warning(f"skip tool {full_name}: name contains illegal characters")
        return None

    description = tool.description or f"来自 MCP server {server_name} 的工具 {tool.name}"
    return McpTool(
        full_name=full_name,
        remote_name=tool.name,
        tool_description=description,
        input_schema=_schema(tool),
        readonly=_read_only(tool),
        caller=session,
    )


__all__ = ["CallerSession", "McpTool", "adapt_tool"]
