"""工具系统：统一工具抽象、执行结果、注册中心与 6 个核心工具。

- 每个工具暴露名称、给模型看的描述、参数 Schema、执行入口（F1）；
- 所有失败都包成 `Result(is_error=True)` 返回，绝不向调用方抛 Python 异常（F9/N4）；
- 工具执行受 `DEFAULT_TIMEOUT` 超时约束（N1）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..llm import ToolDefinition

# 单个工具执行的默认超时秒数（N1：超时不配置化，内置合理默认值）
DEFAULT_TIMEOUT: float = 30.0

# 各工具结果体量上限（N5：避免撑爆上下文/界面）
READ_MAX_LINES = 2000  # read_file 行数上限
READ_MAX_CHARS = 256 * 1024  # read_file 字节上限
BASH_MAX_LINES = 10_000  # bash 输出行数上限
BASH_MAX_CHARS = 30_000  # bash 输出字符上限
GLOB_MAX_RESULTS = 100  # glob 命中条数上限
GREP_MAX_RESULTS = 100  # grep 命中条数上限


@dataclass
class Result:
    """工具执行结果——永远以值类型返回，从不抛异常给上层。

    `is_error=True` 表示结构化错误，`content` 即错误描述（回灌给模型让其调整）。
    """

    content: str
    is_error: bool = False


@runtime_checkable
class Tool(Protocol):
    """统一工具抽象（F1）。

    实现以普通类即可；`name()`/`description()`/`parameters()` 供注册中心导出定义，
    `execute(args)` 接收 raw JSON 参数字符串并返回 `Result`。
    """

    def name(self) -> str: ...  # 模型看到的工具名，如 "read_file"

    def description(self) -> str: ...  # 给模型的用途说明

    def parameters(self) -> dict[str, Any]: ...  # 手写 JSON Schema（type/properties/required）

    def read_only(self) -> bool: ...  # True=只读：可并发执行 & Plan Mode 放行（F5/F10）

    async def execute(self, args: str) -> Result: ...


def _truncate(s: str, max_lines: int, max_chars: int) -> str:
    """按行数与字符数双上限截断；截断发生时尾部追加 `[truncated]` 标注（N5）。"""
    truncated = False
    lines = s.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars]
        truncated = True
    if truncated:
        out += "\n[truncated]"
    return out


class Registry:
    """工具注册中心：集中登记、按名查找、导出定义、按名执行（F1/F5）。"""

    def __init__(self) -> None:
        self._order: list[str] = []  # 保持注册顺序，导出稳定
        self._tools: dict[str, Tool] = {}

    def register(self, t: Tool) -> None:
        """登记工具；重名注册抛 ValueError（避免静默覆盖）。"""
        name = t.name()
        if name in self._tools:
            raise ValueError(f"工具重名: {name}")
        self._tools[name] = t
        self._order.append(name)

    def get(self, name: str) -> Tool | None:
        """按名查找工具，未命中返回 None。"""
        return self._tools.get(name)

    def definitions(self) -> list[ToolDefinition]:
        """按注册顺序导出协议无关工具定义（F3/AC1）。"""
        return [
            ToolDefinition(
                name=name,
                description=self._tools[name].description(),
                input_schema=self._tools[name].parameters(),
            )
            for name in self._order
        ]

    def read_only_definitions(self) -> list[ToolDefinition]:
        """Plan Mode：只导出 read_only 工具的定义，保留注册顺序（F10）。"""
        return [
            ToolDefinition(
                name=name,
                description=self._tools[name].description(),
                input_schema=self._tools[name].parameters(),
            )
            for name in self._order
            if self._tools[name].read_only()
        ]

    def is_read_only(self, name: str) -> bool:
        """分批判定：工具是否只读（可并发）；未知工具返回 False（按串行处理）（F5）。"""
        t = self._tools.get(name)
        return t is not None and t.read_only()

    async def execute(self, name: str, args: str, timeout: float = DEFAULT_TIMEOUT) -> Result:
        """按名执行工具；未命中 / 超时 / 任何异常都包成结构化 Result（F5/F9/N4）。"""
        tool = self._tools.get(name)
        if tool is None:
            return Result(content=f"未知工具: {name}", is_error=True)
        try:
            return await asyncio.wait_for(tool.execute(args), timeout)
        except TimeoutError:
            return Result(content=f"工具 {name} 执行超时（{timeout}s）", is_error=True)
        except Exception as e:
            return Result(content=f"工具 {name} 异常: {e}", is_error=True)


def _parse_args(args: str) -> dict[str, Any]:
    """把工具入参（raw JSON 字符串）解析为 dict。

    空串（OpenAI 可能给空串而非 `{}`）归一为 `{}`，避免误报参数错误。
    """
    if not args or not args.strip():
        return {}
    return json.loads(args)


def new_default_registry() -> Registry:
    """构造并注册 6 个核心工具，返回注册中心（F2/AC1）。"""
    from .bash import BashTool
    from .edit_file import EditFileTool
    from .glob_tool import GlobTool
    from .grep_tool import GrepTool
    from .read_file import ReadFileTool
    from .write_file import WriteFileTool

    registry = Registry()
    for tool in (
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        BashTool(),
        GlobTool(),
        GrepTool(),
    ):
        registry.register(tool)
    return registry


__all__ = [
    "DEFAULT_TIMEOUT",
    "Registry",
    "Result",
    "Tool",
    "ToolDefinition",
    "new_default_registry",
    "_parse_args",
    "_truncate",
]
