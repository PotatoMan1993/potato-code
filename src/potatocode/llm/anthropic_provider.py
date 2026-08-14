"""anthropic 适配器：封装 AsyncAnthropic，统一吐出文本增量与工具调用。

- 注入内置 system prompt；
- tools 非空时随请求注入工具定义（F3）；
- 按配置开启扩展思考（thinking）；含工具交互历史的请求关闭 thinking（避免 400）；
- 思考增量接收即丢弃；模型发起工具调用时以 StreamEvent(tool_calls=...) 一次性上抛；
- 正常结束 yield done 事件，异常 yield err 事件（不抛出给上层）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import anthropic

from ..prompt import SYSTEM_PROMPT
from . import Message, StreamEvent, ToolCall, ToolDefinition

if TYPE_CHECKING:
    from ..config import ProviderConfig


def _to_anthropic_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    """把协议无关工具定义转为 anthropic 的 tools 请求参数（F3/AC1）。"""
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in tools
    ]


def _to_anthropic_messages(msgs: list[Message]) -> list[dict[str, Any]]:
    """把统一 Message 列表转为 anthropic messages 参数。

    assistant 工具调用回合 → content 数组（text + tool_use 块）；
    tool 结果回合 → 单条 user 消息（tool_result 内容块数组）。
    """
    out: list[dict[str, Any]] = []
    for m in msgs:
        if m.role == "user":
            out.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            if m.tool_calls:
                content: list[dict[str, Any]] = []
                if m.content:
                    content.append({"type": "text", "text": m.content})
                for c in m.tool_calls:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": c.id,
                            "name": c.name,
                            "input": json.loads(c.input),
                        }
                    )
                out.append({"role": "assistant", "content": content})
            else:
                out.append({"role": "assistant", "content": m.content})
        elif m.role == "tool":
            content = [
                {
                    "type": "tool_result",
                    "tool_use_id": r.tool_call_id,
                    "content": r.content,
                    "is_error": r.is_error,
                }
                for r in m.tool_results
            ]
            out.append({"role": "user", "content": content})
    return out


def _has_tool_history(msgs: list[Message]) -> bool:
    """历史中是否已含工具交互（tool_calls 或 tool_results）——是则关闭 thinking。"""
    return any(m.tool_calls or m.tool_results for m in msgs)


class AnthropicProvider:
    """anthropic 协议适配器。"""

    def __init__(self, cfg: ProviderConfig) -> None:
        self._name = cfg.name
        self._model = cfg.model
        self._thinking = cfg.thinking
        # base_url 非空时覆盖默认端点（接入兼容服务）
        self._client = anthropic.AsyncAnthropic(
            api_key=cfg.api_key,
            base_url=cfg.base_url or None,
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model

    async def stream(
        self,
        msgs: list[Message],
        tools: list[ToolDefinition],
    ) -> AsyncIterator[StreamEvent]:
        """发起一轮流式对话，以 async generator 吐出 StreamEvent。"""
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 4096,
            "system": SYSTEM_PROMPT,
            "messages": _to_anthropic_messages(msgs),
        }
        if tools:
            params["tools"] = _to_anthropic_tools(tools)
        if self._thinking and not _has_tool_history(msgs):
            # 扩展思考：budget_tokens 必须小于 max_tokens；含工具历史的请求关闭以避免 400
            params["thinking"] = {"type": "enabled", "budget_tokens": 2048}

        try:
            async with self._client.messages.stream(**params) as stream:
                async for event in stream:
                    # 只取正文文本增量；thinking_delta/input_json_delta 等一律丢弃
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        yield StreamEvent(text=event.delta.text)
            final_message = await stream.get_final_message()
            if final_message.stop_reason == "tool_use":
                # 用 SDK 汇总的完整消息收集 tool_use 块（SDK 已累加 input JSON）
                calls = [
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        input=json.dumps(block.input),
                    )
                    for block in final_message.content
                    if block.type == "tool_use"
                ]
                if calls:
                    yield StreamEvent(tool_calls=calls)
            yield StreamEvent(done=True)
        except asyncio.CancelledError:
            raise  # 调用方 cancel 时正常终止，SDK 流由 async with 清理
        except Exception as e:  # 鉴权失败 / 限流 / 网络中断 / 模型不存在等
            yield StreamEvent(err=e)
