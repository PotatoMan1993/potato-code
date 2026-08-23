"""openai 适配器：封装 AsyncOpenAI chat.completions，统一吐出文本增量与工具调用。

- 内置 system prompt 作为首条 system 消息注入；system_suffix 非空时拼接到其后
  （Plan Mode，F10）；
- tools 非空时随请求注入工具定义（F3）；
- thinking 配置在 openai 协议下忽略；
- 流式按 index 累加 tool_calls 分片，组装完整工具调用（F4）；
- 打开 stream_options include_usage，流末尾 chunk 上抛本轮 token 用量（F8）；
- 正常结束 yield done 事件，异常 yield err 事件（不抛出给上层）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import openai
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

from ..prompt import SYSTEM_PROMPT
from . import Message, StreamEvent, ToolCall, ToolDefinition, Usage

if TYPE_CHECKING:
    from ..config import ProviderConfig


def _effective_system(suffix: str) -> str:
    """系统提示：suffix 为空取单段 SYSTEM_PROMPT；非空时拼接为单段字符串。"""
    return SYSTEM_PROMPT if not suffix else SYSTEM_PROMPT + "\n\n" + suffix


def _to_openai_tools(tools: list[ToolDefinition]) -> list[dict[str, object]]:
    """把协议无关工具定义转为 openai 的 tools 请求参数（F3/AC1）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


def _to_params(m: Message) -> list[ChatCompletionMessageParam]:
    """把统一 Message 转为 openai 消息参数列表。

    assistant 工具调用回合 → 一条带 tool_calls 的 assistant 消息；
    tool 结果回合 → 每个结果各一条 tool 消息。
    """
    if m.role == "user":
        return [{"role": "user", "content": m.content}]
    if m.role == "assistant":
        if m.tool_calls:
            return [
                {
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": c.input or "{}"},
                        }
                        for c in m.tool_calls
                    ],
                }
            ]
        return [{"role": "assistant", "content": m.content}]
    # tool 角色：每个结果独立成一条 tool 消息
    return [
        {"role": "tool", "tool_call_id": r.tool_call_id, "content": r.content}
        for r in m.tool_results
    ]


class OpenAIProvider:
    """openai 协议适配器（兼容各类 OpenAI 协议端点）。"""

    def __init__(self, cfg: ProviderConfig) -> None:
        self._name = cfg.name
        self._model = cfg.model
        # base_url 非空时覆盖默认端点（接入兼容服务）
        self._client = openai.AsyncOpenAI(
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
        system_suffix: str = "",
    ) -> AsyncIterator[StreamEvent]:
        """发起一轮流式对话，以 async generator 吐出 StreamEvent。"""
        # 首条注入内置 system prompt（可带计划态后缀），其后是完整多轮历史
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": _effective_system(system_suffix)}
        ]
        for m in msgs:
            messages.extend(_to_params(m))

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            # 开启后流末尾会出现带 usage 的 chunk，供本轮用量上抛（F8）
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = _to_openai_tools(tools)

        try:
            stream = await self._client.chat.completions.create(**kwargs)
            # stream=True 时 SDK 返回 AsyncStream[ChatCompletionChunk]（收窄联合类型）
            assert not isinstance(stream, ChatCompletion)

            # 按 index 累加 tool_calls 分片（多工具下同时分片，须拼齐参数）
            tool_calls_buf: dict[int, dict[str, str]] = {}
            async for chunk in stream:
                # 个别 chunk（如仅含 usage）可能没有 choices
                if not chunk.choices:
                    if chunk.usage:
                        # 流末尾的用量 chunk：一次性上抛本轮 token 用量（F8）
                        yield StreamEvent(
                            usage=Usage(
                                input_tokens=chunk.usage.prompt_tokens or 0,
                                output_tokens=chunk.usage.completion_tokens or 0,
                            )
                        )
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if delta.content:
                    yield StreamEvent(text=delta.content)
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        if tc.index is None:
                            continue
                        buf = tool_calls_buf.setdefault(tc.index, {})
                        if tc.id:
                            buf["id"] = tc.id
                        if tc.function and tc.function.name:
                            buf["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            buf["args"] = buf.get("args", "") + tc.function.arguments

            if tool_calls_buf:
                # 空 arguments（无参工具）归一为 "{}"，避免回灌时报参数非法
                calls = [
                    ToolCall(
                        id=v.get("id", ""),
                        name=v.get("name", ""),
                        input=v.get("args") or "{}",
                    )
                    for _, v in sorted(tool_calls_buf.items())
                ]
                yield StreamEvent(tool_calls=calls)
            yield StreamEvent(done=True)
        except asyncio.CancelledError:
            raise  # 调用方 cancel 时正常终止
        except Exception as e:  # 鉴权失败 / 限流 / 网络中断 / 模型不存在等
            yield StreamEvent(err=e)
