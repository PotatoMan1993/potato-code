"""用真实 SDK 和内存 HTTP 传输验证协议预算、用量、停止原因与有限尝试。"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import anthropic
import httpx
import openai
import pytest

from potatocode.config import ProviderConfig
from potatocode.llm import (
    ContextLimits,
    Message,
    Request,
    StopReason,
    System,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from potatocode.llm.anthropic_provider import AnthropicProvider
from potatocode.llm.errors import ContextWindowExceeded, classify_error
from potatocode.llm.openai_provider import OpenAIProvider


def openai_chunks(*, reason="stop", usage=True, tools=False):
    delta = {"content": "摘要正文"}
    if tools:
        delta = {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                }
            ]
        }
    item = {
        "id": "c",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test",
        "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
    }
    if usage:
        # 与 choices 同块，验证兼容端点不会漏采 usage。
        item["usage"] = {
            "prompt_tokens": 60,
            "completion_tokens": 4,
            "total_tokens": 64,
            "prompt_tokens_details": {"cached_tokens": 30},
        }
    return ("data: " + json.dumps(item) + "\n\ndata: [DONE]\n\n").encode()


def anthropic_chunks(*, reason="end_turn", complete=True):
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "m",
                "type": "message",
                "role": "assistant",
                "model": "test",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 30,
                },
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "摘要正文"},
        },
        {"type": "content_block_stop", "index": 0},
    ]
    if complete:
        events += [
            {
                "type": "message_delta",
                "delta": {"stop_reason": reason, "stop_sequence": None},
                "usage": {"output_tokens": 4},
            },
            {"type": "message_stop"},
        ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


@asynccontextmanager
async def connected(protocol, handler, **kwargs):
    cfg = ProviderConfig(
        name="test",
        protocol=protocol,
        api_key="fake",
        model="test",
        context_window=128000,
        **kwargs,
    )
    provider = AnthropicProvider(cfg) if protocol == "anthropic" else OpenAIProvider(cfg)
    await provider._client.close()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client_type = anthropic.AsyncAnthropic if protocol == "anthropic" else openai.AsyncOpenAI
    provider._client = client_type(
        api_key="fake", base_url="https://test.invalid", http_client=http_client
    )
    try:
        yield provider
    finally:
        await provider._client.close()


def response(protocol, **kwargs):
    content = anthropic_chunks(**kwargs) if protocol == "anthropic" else openai_chunks(**kwargs)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)


@pytest.mark.parametrize("protocol", ["anthropic", "openai"])
async def test_summary_parameters_usage_and_actual_budget_shape(protocol):
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        return response(protocol)

    req = Request(
        messages=[Message(role="user", content="历史")],
        system=System(stable="摘要指令"),
        purpose="summary",
        max_output_tokens=12000,
    )
    async with connected(protocol, handler, thinking=True) as provider:
        events = [e async for e in provider.stream(req)]
        assert not [e.err for e in events if e.err]
        assert events[-1].stop_reason == StopReason.STOP
        usages = [e.usage for e in events if e.usage]
        assert (usages[-1].input_tokens, usages[-1].output_tokens) == (60, 4)
        assert usages[-1].cache_read == 30
        assert len(sent) == 1
        body = sent[0]
        assert "tools" not in body and "thinking" not in body
        assert body["max_tokens" if protocol == "anthropic" else "max_completion_tokens"] == 12000
        shape = provider.budget_shape(req)
        assert json.loads(shape.messages[0][0]) == body["messages"][-1]
        assert "fake" not in repr(shape)
        if protocol == "anthropic":
            assert usages[-1].raw_counts["input_tokens"] == 10
            assert usages[-1].cache_write == 20
            assert json.loads(shape.fixed[0])["system"] == body["system"]
        else:
            assert usages[-1].raw_counts["prompt_tokens"] == 60
            assert usages[-1].raw_counts["prompt_tokens_details.cached_tokens"] == 30
            assert "max_tokens" not in body


@pytest.mark.parametrize("protocol", ["anthropic", "openai"])
async def test_wire_groups_and_reminder_share_real_serialization(protocol):
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        return response(protocol)

    req = Request(
        messages=[
            Message(
                role="assistant",
                tool_calls=[ToolCall("c1", "read_file", "{}"), ToolCall("c2", "read_file", "{}")],
            ),
            Message(role="tool", tool_results=[ToolResult("c1", "甲"), ToolResult("c2", "乙")]),
        ],
        tools=[ToolDefinition("read_file", "读", {"type": "object"})],
        system=System("固定", "环境"),
        reminder="提醒",
    )
    async with connected(protocol, handler) as provider:
        shape = provider.budget_shape(req)
        events = [event async for event in provider.stream(req)]
        assert not any(event.err for event in events)
        wire = [json.loads(m) for group in shape.messages for m in group]
        wire += [json.loads(m) for m in shape.trailing]
        assert wire == sent[0]["messages"][(1 if protocol == "openai" else 0) :]
        assert len(shape.messages[1]) == (2 if protocol == "openai" else 1)
        assert req.messages[-1].tool_results[-1].content == "乙"


@pytest.mark.parametrize("protocol", ["anthropic", "openai"])
async def test_summary_disables_hidden_sdk_retries(protocol):
    calls = 0

    def handler(req):
        nonlocal calls
        calls += 1
        return httpx.Response(
            429, json={"error": {"type": "rate_limit_error", "message": "稍后再试"}}
        )

    async with connected(protocol, handler) as provider:
        events = [e async for e in provider.stream(Request(purpose="summary"))]
    assert calls == 1
    assert events[-1].err is not None
    assert not isinstance(events[-1].err, ContextWindowExceeded)


@pytest.mark.parametrize("protocol", ["anthropic", "openai"])
async def test_length_is_not_input_overflow(protocol):
    reason = "max_tokens" if protocol == "anthropic" else "length"
    async with connected(protocol, lambda req: response(protocol, reason=reason)) as provider:
        events = [e async for e in provider.stream(Request(purpose="summary"))]
    assert events[-1].stop_reason == StopReason.LENGTH
    assert not any(e.err for e in events)


async def test_anthropic_partial_usage_survives_missing_stop():
    async with connected(
        "anthropic", lambda req: response("anthropic", complete=False)
    ) as provider:
        events = [e async for e in provider.stream(Request())]
    usages = [e.usage for e in events if e.usage]
    assert usages[-1].input_tokens == 60
    assert usages[-1].output_tokens == 1
    assert events[-1].err is not None
    assert not any(e.done for e in events)


async def test_openai_missing_usage_remains_unknown_and_old_limit_is_explicit():
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        return response("openai", usage=False)

    async with connected("openai", handler, openai_output_limit_parameter="max_tokens") as provider:
        events = [e async for e in provider.stream(Request(max_output_tokens=2000))]
    assert not any(e.usage for e in events)
    assert sent[0]["max_tokens"] == 2000
    assert "max_completion_tokens" not in sent[0]


async def test_openai_does_not_emit_truncated_tool_calls():
    async with connected(
        "openai", lambda req: response("openai", reason="length", tools=True)
    ) as provider:
        events = [e async for e in provider.stream(Request())]
    assert events[-1].stop_reason == StopReason.LENGTH
    assert not any(e.tool_calls for e in events)


@pytest.mark.parametrize("protocol", ["anthropic", "openai"])
async def test_explicit_input_overflow_is_normalized(protocol):
    detail = {"type": "invalid_request_error", "message": "prompt is too long"}
    if protocol == "openai":
        detail["code"] = "context_length_exceeded"
    async with connected(
        protocol, lambda req: httpx.Response(400, json={"error": detail})
    ) as provider:
        events = [e async for e in provider.stream(Request(purpose="summary"))]
    assert isinstance(events[-1].err, ContextWindowExceeded)


@pytest.mark.parametrize(
    "status,message",
    [
        (401, "prompt is too long"),
        (413, "prompt is too long"),
        (400, "max_tokens must be less than 4096"),
        (500, "maximum context length"),
    ],
)
def test_other_errors_keep_original_identity(status, message):
    err = RuntimeError("原错误")
    err.status_code = status
    err.body = {"type": "invalid_request_error", "message": message}
    assert classify_error(err, "anthropic") is err


class BlockingStream(httpx.AsyncByteStream):
    def __init__(self, initial):
        self.initial = initial
        self.waiting = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield self.initial
        self.waiting.set()
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("protocol", ["anthropic", "openai"])
async def test_cancelling_silent_stream_closes_http_response(protocol):
    initial = (
        anthropic_chunks(complete=False) if protocol == "anthropic" else openai_chunks(reason=None)
    )
    stream = BlockingStream(initial.replace(b"data: [DONE]\n\n", b""))
    async with connected(
        protocol,
        lambda req: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=stream
        ),
    ) as provider:

        async def consume():
            return [e async for e in provider.stream(Request(purpose="summary"))]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(stream.waiting.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stream.closed


@pytest.mark.parametrize("protocol", ["anthropic", "openai"])
async def test_missing_window_prevents_network_request(protocol):
    calls = []
    async with connected(protocol, lambda req: calls.append(req)) as provider:
        provider._limits = ContextLimits()
        events = [e async for e in provider.stream(Request())]
    assert not calls
    assert isinstance(events[-1].err, ValueError)


@pytest.mark.parametrize("protocol", ["anthropic", "openai"])
async def test_normal_request_keeps_sdk_retry_policy(protocol):
    calls = 0

    def handler(req):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"retry-after-ms": "1"},
                json={"error": {"type": "rate_limit_error", "message": "稍后再试"}},
            )
        return response(protocol)

    async with connected(protocol, handler) as provider:
        events = [e async for e in provider.stream(Request())]
    assert calls == 2
    assert events[-1].stop_reason == StopReason.STOP


async def test_openai_usage_is_preserved_when_finish_signal_is_missing():
    async with connected("openai", lambda req: response("openai", reason=None)) as provider:
        events = [e async for e in provider.stream(Request())]
    assert [e.usage.input_tokens for e in events if e.usage] == [60]
    assert events[-1].err is not None
    assert not any(e.done for e in events)


async def test_openai_complete_tool_call_survives_stream():
    async with connected(
        "openai", lambda req: response("openai", reason="tool_calls", tools=True)
    ) as provider:
        events = [e async for e in provider.stream(Request())]
    assert events[-1].stop_reason == StopReason.TOOL_CALLS
    assert [c for e in events for c in e.tool_calls] == [
        ToolCall("c1", "read_file", '{"path":"a.py"}')
    ]


async def test_anthropic_thinking_rejects_incompatible_output_budget_before_http():
    calls = []
    async with connected("anthropic", lambda req: calls.append(req), thinking=True) as provider:
        events = [e async for e in provider.stream(Request(max_output_tokens=2048))]
    assert not calls
    assert isinstance(events[-1].err, ValueError)
    assert "thinking" in str(events[-1].err)


@pytest.mark.parametrize("protocol", ["anthropic", "openai"])
async def test_summary_rejects_output_above_fixed_reserve(protocol):
    calls = []
    async with connected(protocol, lambda req: calls.append(req)) as provider:
        events = [
            e async for e in provider.stream(Request(purpose="summary", max_output_tokens=20001))
        ]
    assert not calls
    assert isinstance(events[-1].err, ValueError)
