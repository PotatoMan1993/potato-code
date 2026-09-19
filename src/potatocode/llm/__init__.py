"""LLM 协议层：协议无关的 Provider 抽象、统一消息/流式事件类型与工厂。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
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
    """协议无关地承载一轮请求的 token 用量（含缓存命中信息，F4）。"""

    input_tokens: int | None = None  # 完整输入；缺失与真实零值分开
    output_tokens: int | None = None  # API 输出，可能包含未保留的思考
    cache_write: int = 0  # Anthropic: cache_creation_input_tokens；OpenAI 恒 0
    cache_read: int = 0  # Anthropic: cache_read_input_tokens；OpenAI: cached_tokens
    raw_counts: dict[str, int | None] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextLimits:
    """端点显式预算；保留非法配置到请求前报告，不猜测模型窗口。"""

    context_window: int | None = None
    max_output_tokens: int = 4096
    compact_max_output_tokens: int = 20000

    def validate(self) -> None:
        if type(self.context_window) is not int or self.context_window <= 33000:
            raise ValueError("请配置 context_window：必须是大于 33,000 的整数")
        if type(self.max_output_tokens) is not int or not (
            0 < self.max_output_tokens < self.context_window
        ):
            raise ValueError("max_output_tokens 必须是小于 context_window 的正整数")
        if type(self.compact_max_output_tokens) is not int or not (
            0 < self.compact_max_output_tokens <= 20000
        ):
            raise ValueError("compact_max_output_tokens 必须是至多 20,000 的正整数")


@dataclass(frozen=True)
class BudgetShape:
    """真实协议输入的规范 JSON；消息分组与内部历史一一对应。"""

    fixed: tuple[str, ...] = ()
    messages: tuple[tuple[str, ...], ...] = ()
    trailing: tuple[str, ...] = ()


class StopReason(StrEnum):
    """正常终止信号与输出完整性不能混为一谈。"""

    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    REFUSAL = "refusal"
    UNKNOWN = "unknown"


@dataclass
class System:
    """系统提示两段：稳定块（可缓存）与环境段（不缓存）（F3）。"""

    stable: str = ""  # 可缓存：装配好的稳定系统提示（工具定义随 tools 一并进缓存前缀）
    environment: str = ""  # 不缓存：环境信息段


@dataclass
class Request:
    """一次流式请求的全部入参（替代 stream 的位置参数，N8）。"""

    messages: list[Message] = field(default_factory=list)  # 持久对话历史（不含本轮 reminder）
    tools: list[ToolDefinition] = field(default_factory=list)  # 本轮工具集（普通=全量/规划=只读）
    system: System = field(default_factory=System)  # 稳定系统提示 + 环境段
    reminder: str = ""  # 本轮 system-reminder 内容（已含标签；空=不注入）
    purpose: Literal["normal", "summary"] = "normal"
    max_output_tokens: int = 4096


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
    stop_reason: StopReason | None = None
    raw_stop_reason: str | None = None


@runtime_checkable
class Provider(Protocol):
    """协议无关的对话 provider 接口。

    `stream(req)` 发起一轮流式对话：`req.system.stable`（可缓存）与
    `req.system.environment`（不缓存）分属不同内容块；`req.tools` 随请求注入工具定义；
    `req.reminder` 非空时按各自协议安全织入消息通道（不污染缓存、不破坏角色交替）；
    思考增量内部丢弃；流中模型发起工具调用时以 StreamEvent(tool_calls=...) 上抛；
    流结束后以 StreamEvent(usage=...) 上抛本轮 token 用量（含缓存命中字段）；
    以 async generator 吐出 StreamEvent；调用方 cancel 对应 task 即终止本轮。
    """

    @property
    def name(self) -> str: ...  # -> 状态栏左

    @property
    def model(self) -> str: ...  # -> 状态栏右

    @property
    def identity(self) -> str: ...

    @property
    def protocol(self) -> str: ...

    @property
    def context_limits(self) -> ContextLimits: ...

    def budget_shape(self, req: Request) -> BudgetShape: ...

    def stream(self, req: Request) -> AsyncIterator[StreamEvent]: ...


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
    "Request",
    "StreamEvent",
    "System",
    "ToolCall",
    "ToolDefinition",
    "ToolResult",
    "Usage",
    "ContextLimits",
    "BudgetShape",
    "StopReason",
    "ROLE_ASSISTANT",
    "ROLE_TOOL",
    "ROLE_USER",
    "new_provider",
]
