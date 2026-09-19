"""openai 适配器：封装 AsyncOpenAI chat.completions，统一吐出文本增量与工具调用。

- 系统消息单条：stable 在前、environment 拼其后（F3）；stable 居前缀以便端点前缀缓存命中；
- tools 非空时随请求注入工具定义（F3）；
- reminder 非空时追加一条尾部 user 消息（OpenAI 容忍连续 user，N3）；
- thinking 配置在 openai 协议下忽略；
- 流式按 index 累加 tool_calls 分片，组装完整工具调用（F4）；
- 打开 stream_options include_usage，流末尾 chunk 上抛本轮 token 用量（含 cached_tokens，F4/F8）；
- 正常结束 yield done 事件，异常 yield err 事件（不抛出给上层）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import openai
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

from . import (
    BudgetShape,
    ContextLimits,
    Message,
    Request,
    StopReason,
    StreamEvent,
    ToolCall,
    ToolDefinition,
)
from .errors import classify_error
from .usage import canonical_json, merge_counts, usage_from_counts

if TYPE_CHECKING:
    from ..config import ProviderConfig


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


def _to_openai_messages(req: Request) -> list[ChatCompletionMessageParam]:
    """组装 openai 消息序列：单条 system（stable+env）+ 历史 + 可选尾部 reminder。"""
    sys_content = req.system.stable
    if req.system.environment:
        sys_content = f"{req.system.stable}\n\n{req.system.environment}"
    messages: list[ChatCompletionMessageParam] = [{"role": "system", "content": sys_content}]
    for m in req.messages:
        messages.extend(_to_params(m))
    if req.reminder:
        # 追加尾部 user 消息承载补充指令（OpenAI 容忍连续 user / tool 后接 user，N3）
        messages.append({"role": "user", "content": req.reminder})
    return messages


class OpenAIProvider:
    """openai 协议适配器（兼容各类 OpenAI 协议端点）。"""

    def __init__(self, cfg: ProviderConfig) -> None:
        self._name = cfg.name
        self._model = cfg.model
        self._identity = uuid.uuid4().hex
        self._limits = ContextLimits(
            cfg.context_window, cfg.max_output_tokens, cfg.compact_max_output_tokens
        )
        self._output_parameter = cfg.openai_output_limit_parameter
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

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def protocol(self) -> str:
        return "openai"

    @property
    def context_limits(self) -> ContextLimits:
        return self._limits

    def _input_params(self, req: Request) -> dict[str, Any]:
        params: dict[str, Any] = {"messages": _to_openai_messages(req)}
        if req.tools:
            params["tools"] = _to_openai_tools(req.tools)
        return params

    def budget_shape(self, req: Request) -> BudgetShape:
        params = self._input_params(req)
        messages = params.pop("messages")
        fixed = [canonical_json(messages[0])]
        if params:
            fixed.append(canonical_json(params))
        groups: list[tuple[str, ...]] = []
        position = 1
        for message in req.messages:
            size = len(message.tool_results) if message.role == "tool" else 1
            groups.append(tuple(canonical_json(m) for m in messages[position : position + size]))
            position += size
        return BudgetShape(
            fixed=tuple(fixed),
            messages=tuple(groups),
            trailing=tuple(canonical_json(m) for m in messages[position:]),
        )

    async def stream(self, req: Request) -> AsyncIterator[StreamEvent]:
        """发起一轮流式对话，以 async generator 吐出 StreamEvent。"""
        stream = None
        try:
            self._limits.validate()
            if type(req.max_output_tokens) is not int or not (
                0 < req.max_output_tokens < self._limits.context_window  # type: ignore[operator]
            ):
                raise ValueError("请求 max_output_tokens 必须是小于 context_window 的正整数")
            if req.purpose == "summary" and req.max_output_tokens > 20000:
                raise ValueError("摘要输出上限不能超过 20,000")
            kwargs = self._input_params(req)
            kwargs.update(model=self._model, stream=True, stream_options={"include_usage": True})
            kwargs[self._output_parameter] = req.max_output_tokens
            client = (
                self._client.with_options(max_retries=0)
                if req.purpose == "summary"
                else self._client
            )
            stream = await client.chat.completions.create(**kwargs)
            # stream=True 时 SDK 返回 AsyncStream[ChatCompletionChunk]（收窄联合类型）
            assert not isinstance(stream, ChatCompletion)

            # 按 index 累加 tool_calls 分片（多工具下同时分片，须拼齐参数）
            tool_calls_buf: dict[int, dict[str, str]] = {}
            counts: dict[str, int | None] = {}
            raw_reason: str | None = None
            async for chunk in stream:
                if chunk.usage is not None:
                    merge_counts(counts, chunk.usage)
                    yield StreamEvent(usage=usage_from_counts(counts, self.protocol))
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason is not None:
                    raw_reason = choice.finish_reason
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

            if raw_reason is None:
                raise RuntimeError("模型流提前结束，未收到正常终止信号")
            reason = {
                "stop": StopReason.STOP,
                "tool_calls": StopReason.TOOL_CALLS,
                "length": StopReason.LENGTH,
                "content_filter": StopReason.REFUSAL,
            }.get(raw_reason, StopReason.UNKNOWN)
            if tool_calls_buf and reason in (StopReason.STOP, StopReason.TOOL_CALLS):
                # 空 arguments（无参工具）归一为 "{}"，避免回灌时报参数非法
                calls = [
                    ToolCall(
                        id=v.get("id", ""),
                        name=v.get("name", ""),
                        input=v.get("args") or "{}",
                    )
                    for _, v in sorted(tool_calls_buf.items())
                ]
                for call in calls:
                    if not call.id or not call.name or not isinstance(json.loads(call.input), dict):
                        raise ValueError("模型返回不完整的工具调用")
                yield StreamEvent(tool_calls=calls)
            yield StreamEvent(done=True, stop_reason=reason, raw_stop_reason=raw_reason)
        except asyncio.CancelledError:
            raise  # 调用方 cancel 时正常终止
        except Exception as e:  # 鉴权失败 / 限流 / 网络中断 / 模型不存在等
            yield StreamEvent(err=classify_error(e, self.protocol))
        finally:
            if stream is not None and not isinstance(stream, ChatCompletion):
                await stream.close()
