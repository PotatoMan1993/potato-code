"""LLM 协议层：协议无关的 Provider 抽象、统一消息/流式事件类型与工厂。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..config import ProviderConfig

# 消息角色（标注 Literal 以配合 Message.role 的窄化字面量）
ROLE_USER: Literal["user"] = "user"
ROLE_ASSISTANT: Literal["assistant"] = "assistant"
ROLE_TOOL: Literal["tool"] = "tool"  # 携带工具执行结果的回合


@dataclass
class ToolCall:
    """协议无关地承载模型发起的一次工具调用（流式拼接完成后）。

    `input` 是拼接完成的 JSON 参数字符串（raw JSON），由注册中心按名解析执行。
    """

    id: str  # provider 侧调用 id；回灌结果时配对
    name: str  # 工具名（注册中心按名查找）
    input: str  # JSON 参数字符串（raw JSON）


@dataclass
class ToolResult:
    """协议无关地承载一次工具执行结果。"""

    tool_call_id: str  # 对应 ToolCall.id
    content: str  # 执行产出（成功内容或结构化错误文本）
    is_error: bool = False  # 是否为错误结果（F9）


@dataclass
class ToolDefinition:
    """注册中心导出的协议无关工具定义。"""

    name: str
    description: str
    input_schema: dict[str, Any]  # 完整 JSON Schema：type/properties/required


@dataclass
class Message:
    """一轮对话中的一条消息。

    user/assistant 为普通文本回合；assistant 可携带 tool_calls（工具调用回合），
    tool 角色携带 tool_results（工具结果回合）。system 由适配器注入。
    """

    role: Literal["user", "assistant", "tool"]
    content: str = ""  # 纯文本内容（增量/完整正文）
    tool_calls: list[ToolCall] = field(default_factory=list)  # 仅 assistant
    tool_results: list[ToolResult] = field(default_factory=list)  # 仅 tool 角色


@dataclass
class Usage:
    """协议无关地承载一轮请求的 token 用量。"""

    input_tokens: int = 0  # 本轮请求输入（含完整历史）token 数
    output_tokens: int = 0  # 本轮响应输出 token 数


@dataclass
class StreamEvent:
    """流式事件：文本增量 / 工具调用 / 用量 / 正常结束 / 出错。

    五态：text 非空表示正文增量；tool_calls 非空表示本轮模型请求执行这些工具
    （在 done 之前一次性上抛）；usage 非空表示本轮 token 用量（done 之前一次性发出）；
    done 与 err 互斥，标识本轮结束。
    """

    text: str = ""  # 文本增量
    tool_calls: list[ToolCall] = field(default_factory=list)  # 非空：本轮模型请求执行的工具
    usage: Usage | None = None  # 非空：本轮 token 用量，done 之前一次性发出
    done: bool = False  # 本轮正常结束
    err: Exception | None = None  # 出错时携带异常


@runtime_checkable
class Provider(Protocol):
    """协议无关的对话 provider 接口。

    `stream(msgs, tools, system_suffix)` 发起一轮流式对话：内部注入内置 system prompt
    与 thinking 配置；`system_suffix` 非空时拼接到内置 SYSTEM_PROMPT 之后（Plan Mode
    计划态约束，F10）；tools 非空时随请求注入工具定义（F3）；思考增量内部丢弃；
    流中模型发起工具调用时以 StreamEvent(tool_calls=...) 上抛；流结束后以
    StreamEvent(usage=...) 上抛本轮 token 用量（F8）；以 async generator 吐出
    StreamEvent；调用方 cancel 对应 task 即终止本轮。
    """

    @property
    def name(self) -> str: ...  # -> 状态栏左

    @property
    def model(self) -> str: ...  # -> 状态栏右

    def stream(
        self,
        msgs: list[Message],
        tools: list[ToolDefinition],
        system_suffix: str = "",
    ) -> AsyncIterator[StreamEvent]: ...


def new_provider(cfg: ProviderConfig) -> Provider:
    """按 protocol 构造对应适配器。"""
    if cfg.protocol == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(cfg)
    if cfg.protocol == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(cfg)
    raise ValueError(f"未知协议: {cfg.protocol!r}")


__all__ = [
    "Message",
    "Provider",
    "StreamEvent",
    "ToolCall",
    "ToolDefinition",
    "ToolResult",
    "Usage",
    "ROLE_ASSISTANT",
    "ROLE_TOOL",
    "ROLE_USER",
    "new_provider",
]
