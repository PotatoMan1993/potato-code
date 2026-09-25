"""anthropic 适配器：封装 AsyncAnthropic，统一吐出文本增量与工具调用。

- 系统提示分两块：稳定块打缓存断点（cache_control: ephemeral）、环境块不打（F3）；
- tools 非空时随请求注入工具定义（F3，不另打断点——断点前缀已含全部工具）；
- reminder 非空时织入最后一条 user 消息的 content 块（不污染缓存、不破坏角色交替，N3）；
- 按配置开启扩展思考（thinking）；含工具交互历史的请求关闭 thinking（避免 400）；
- 思考增量接收即丢弃；模型发起工具调用时以 StreamEvent(tool_calls=...) 一次性上抛；
- 流正常结束后上抛本轮 token 用量 StreamEvent(usage=...)（含缓存写/读，F4/F8）；
- 正常结束 yield done 事件，异常 yield err 事件（不抛出给上层）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import anthropic

from . import Message, Request, StreamEvent, ToolCall, ToolDefinition, Usage
from .errors import is_anthropic_prompt_too_long, wrap_prompt_too_long

if TYPE_CHECKING:
    from ..config import ProviderConfig


def _build_system_blocks(stable: str, environment: str) -> list[dict[str, Any]]:
    """构造 anthropic 的 system 入参（块列表）：稳定块打缓存断点、环境块不打。

    稳定块非空 → 文本块带 `cache_control: {"type": "ephemeral"}`（默认 5m TTL）；
    环境块非空 → 纯文本块（无 cache_control），位于断点之后不进缓存（F3/N1）。
    """
    blocks: list[dict[str, Any]] = []
    if stable:
        blocks.append({"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}})
    if environment:
        blocks.append({"type": "text", "text": environment})
    return blocks


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


def _append_reminder_anthropic(messages: list[dict[str, Any]], reminder: str) -> None:
    """把 reminder 织入消息通道：追加到末条 user 消息的 content 块（N3）。

    循环中末条恒为 user 或 tool_result→user，追加文本块仍是合法 user 消息；
    极端情形（末条为 assistant 或空）则新起一条 user 消息。
    """
    if not reminder:
        return
    if messages and messages[-1].get("role") == "user":
        content = messages[-1]["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        content.append({"type": "text", "text": reminder})
        messages[-1]["content"] = content
    else:
        messages.append({"role": "user", "content": reminder})


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

    async def stream(self, req: Request) -> AsyncIterator[StreamEvent]:
        """发起一轮流式对话，以 async generator 吐出 StreamEvent。"""
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 4096,
            "system": _build_system_blocks(req.system.stable, req.system.environment),
            "messages": _to_anthropic_messages(req.messages),
        }
        if req.reminder:
            # 织入补充指令：并入末条 user 消息的 content 块，不污染缓存、不破坏角色交替
            _append_reminder_anthropic(params["messages"], req.reminder)
        if req.tools:
            params["tools"] = _to_anthropic_tools(req.tools)
        if self._thinking and not _has_tool_history(req.messages):
            # 扩展思考：budget_tokens 必须小于 max_tokens；含工具历史的请求关闭以避免 400
            params["thinking"] = {"type": "enabled", "budget_tokens": 2048}

        try:
            async with self._client.messages.stream(**params) as stream:
                async for event in stream:
                    # 只取正文文本增量；thinking_delta/input_json_delta 等一律丢弃
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        yield StreamEvent(text=event.delta.text)
            final_message = await stream.get_final_message()
            # 流正常结束后一次性上抛本轮 token 用量（F8），先于 done 事件
            # 缓存字段缺失即零值，不额外校验、不抛异常（N6）
            yield StreamEvent(
                usage=Usage(
                    input_tokens=final_message.usage.input_tokens,
                    output_tokens=final_message.usage.output_tokens,
                    cache_write=getattr(final_message.usage, "cache_creation_input_tokens", 0) or 0,
                    cache_read=getattr(final_message.usage, "cache_read_input_tokens", 0) or 0,
                )
            )
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
            # 上下文超长包装成哨兵异常：主循环据此走紧急压缩
            if is_anthropic_prompt_too_long(e):
                yield StreamEvent(err=wrap_prompt_too_long(e, "anthropic 上下文超出窗口"))
            else:
                yield StreamEvent(err=e)
