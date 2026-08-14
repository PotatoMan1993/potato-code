"""agent：单轮闭环编排（请求#1 带工具 → 执行 → 结果回灌 → 请求#2 续答）。

只依赖 llm / tool / conversation，不 import anthropic/openai，保持协议无关。
对外以 async generator 吐出统一 `Event` 流，供 TUI 渲染（F5/F6/AC9）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum

from ..conversation import Conversation
from ..llm import Provider, ToolCall, ToolDefinition, ToolResult
from ..tool import DEFAULT_TIMEOUT, Registry

# 工具行 / 结果摘要里展示的参数预览最大长度
_ARGS_PREVIEW_LIMIT = 80


class Phase(Enum):
    """工具调用阶段：START 开始执行，END 执行完毕。"""

    START = "start"
    END = "end"


@dataclass
class ToolEvent:
    """一次工具调用的开始/结束（供 TUI 渲染工具行与结果摘要）。"""

    name: str
    args: str = ""  # 参数预览（用于 ● name(args)）
    phase: Phase = Phase.START
    result: str = ""  # phase=END：结果摘要
    is_error: bool = False  # phase=END：是否错误


@dataclass
class Event:
    """单轮闭环对外事件流元素，TUI 据非 None 字段分派渲染。"""

    text: str = ""  # 文本增量（preamble 或最终答复）
    tool: ToolEvent | None = None  # 工具调用开始/结束
    done: bool = False  # 本轮结束
    err: Exception | None = None  # 出错（不中断会话）


def _preview(s: str, limit: int = _ARGS_PREVIEW_LIMIT) -> str:
    """参数预览：压缩换行、截断到 limit 字符，用于工具行展示。"""
    s = s.replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


class Agent:
    """持有 provider 与注册中心，执行单轮闭环（F5/F6）。

    调用方 cancel() 此 task（退出/Ctrl+C）即终止；工具执行受 DEFAULT_TIMEOUT 约束。
    """

    def __init__(self, provider: Provider, registry: Registry) -> None:
        self._provider = provider
        self._registry = registry

    async def run(self, conv: Conversation) -> AsyncIterator[Event]:
        """执行单轮闭环，以 async generator 吐出事件流。"""
        defs = self._registry.definitions()

        # —— 请求#1：带工具定义，收集文本增量与工具调用 ——
        preamble_buf: list[str] = []
        calls: list[ToolCall] = []
        try:
            async for text in self._stream_once(conv, defs, preamble_buf, calls):
                yield Event(text=text)
        except Exception as e:
            yield Event(err=e)
            return

        preamble = "".join(preamble_buf)

        if not calls:
            # 纯文本回合：与 ch02 等价，直接结束
            conv.add_assistant(preamble)
            yield Event(done=True)
            return

        # —— 有工具调用：记录回合，顺序执行每个工具 ——
        conv.add_assistant_with_tool_calls(preamble, calls)
        results: list[ToolResult] = []
        for call in calls:
            preview = _preview(call.input)
            yield Event(tool=ToolEvent(name=call.name, args=preview, phase=Phase.START))
            r = await self._registry.execute(call.name, call.input, timeout=DEFAULT_TIMEOUT)
            yield Event(
                tool=ToolEvent(
                    name=call.name,
                    args=preview,
                    phase=Phase.END,
                    result=r.content,
                    is_error=r.is_error,
                )
            )
            results.append(ToolResult(tool_call_id=call.id, content=r.content, is_error=r.is_error))

        # —— 结果回灌进历史（F6）——
        conv.add_tool_results(results)

        # —— 请求#2：续答，生成最终文本；忽略其再次返回的工具调用（单轮，AC9）——
        final_buf: list[str] = []
        ignored: list[ToolCall] = []
        try:
            async for text in self._stream_once(conv, defs, final_buf, ignored):
                yield Event(text=text)
        except Exception as e:
            yield Event(err=e)
            return

        final = "".join(final_buf)
        if not final.strip():
            # 空最终答复：以单轮上限提示占位（AC9）
            final = "（工具结果已回灌，本轮完成；如需继续操作，请再次告诉我。）"
            yield Event(text=final)
        conv.add_assistant(final)
        yield Event(done=True)

    async def _stream_once(
        self,
        conv: Conversation,
        defs: list[ToolDefinition],
        text_buf: list[str],
        calls_buf: list[ToolCall],
    ) -> AsyncIterator[str]:
        """发起一次请求：逐段 yield 文本增量，累积完整文本与工具调用。

        provider 报错时抛异常（由 run 转为 Event(err=...)）；CancelledError 原样上抛。
        """
        async for ev in self._provider.stream(conv.messages(), defs):
            if ev.err is not None:
                raise ev.err
            if ev.text:
                text_buf.append(ev.text)
                yield ev.text
            if ev.tool_calls:
                calls_buf.extend(ev.tool_calls)


__all__ = ["Agent", "Event", "Phase", "ToolEvent"]
