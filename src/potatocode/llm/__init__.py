"""LLM 协议层：协议无关的 Provider 抽象、统一消息/流式事件类型与工厂。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..config import ProviderConfig


@dataclass
class Message:
    """一轮对话中的一条消息（仅 user/assistant，system 由适配器注入）。"""

    role: Literal["user", "assistant"]
    content: str


@dataclass
class StreamEvent:
    """流式事件：文本增量 / 正常结束 / 出错（done 与 err 互斥）。"""

    text: str = ""  # 文本增量
    done: bool = False  # 本轮正常结束
    err: Exception | None = None  # 出错时携带异常


@runtime_checkable
class Provider(Protocol):
    """协议无关的对话 provider 接口。

    `stream(msgs)` 发起一轮流式对话：内部注入内置 system prompt 与 thinking
    配置；思考增量内部丢弃；以 async generator 吐出 StreamEvent；
    调用方 cancel 对应 task 即终止本轮。
    """

    @property
    def name(self) -> str: ...  # -> 状态栏左

    @property
    def model(self) -> str: ...  # -> 状态栏右

    def stream(self, msgs: list[Message]) -> AsyncIterator[StreamEvent]: ...


def new_provider(cfg: ProviderConfig) -> Provider:
    """按 protocol 构造对应适配器。"""
    if cfg.protocol == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(cfg)
    if cfg.protocol == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(cfg)
    raise ValueError(f"未知协议: {cfg.protocol!r}")


__all__ = ["Message", "Provider", "StreamEvent", "new_provider"]
