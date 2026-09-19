"""两种真实 SDK 通过内存 HTTP 流运行完整 Agent 与 Textual 流程。"""

import asyncio
import json

import httpx
import pytest

from context_helpers import summary_json
from potatocode.permission import Mode
from potatocode.tui.view import SessionState
from test_context_agent import CountingTool, run, setup_agent
from test_context_providers import BlockingStream, connected
from test_context_tui import app_for, idle, submit


def wire(protocol, text="完成", *, tool_id=None, tokens=60, complete=True):
    if protocol == "openai":
        delta = {"content": text}
        if tool_id:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": tool_id,
                        "type": "function",
                        "function": {"name": "count", "arguments": "{}"},
                    }
                ]
            }
        chunk = {
            "id": "r",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": ("tool_calls" if tool_id else "stop") if complete else None,
                }
            ],
            "usage": {"prompt_tokens": tokens, "completion_tokens": 4, "total_tokens": tokens + 4},
        }
        return (
            f"data: {json.dumps(chunk)}\n\n" + ("data: [DONE]\n\n" if complete else "")
        ).encode()
    content = (
        {"type": "tool_use", "id": tool_id, "name": "count", "input": {}}
        if tool_id
        else {"type": "text", "text": ""}
    )
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
                "usage": {"input_tokens": tokens, "output_tokens": 1},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": content},
        {"type": "content_block_stop", "index": 0},
    ]
    if not tool_id:
        events.insert(
            2,
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        )
    if complete:
        events.extend(
            [
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": "tool_use" if tool_id else "end_turn",
                        "stop_sequence": None,
                    },
                    "usage": {"output_tokens": 4},
                },
                {"type": "message_stop"},
            ]
        )
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def response(content):
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)


@pytest.mark.parametrize("protocol", ["anthropic", "openai"])
async def test_sdk_multi_tool_auto_compact_and_continue(tmp_path, protocol):
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        if len(sent) <= 2:
            return response(
                wire(protocol, tool_id=f"tool-{len(sent)}", tokens=60 if len(sent) == 1 else 95000)
            )
        if len(sent) == 3:
            assert "tools" not in body
            return response(wire(protocol, summary_json()))
        return response(wire(protocol))

    async with connected(protocol, handler) as provider:
        tool = CountingTool()
        agent, conv, context = setup_agent(tmp_path, provider, tool=tool)
        events = await run(agent, conv)
    assert events[-1].done and not any(e.err for e in events)
    assert tool.calls == 2 and len(sent) == 4
    assert [r.purpose for r in context.ledger.records()] == [
        "normal",
        "normal",
        "summary",
        "normal",
    ]
    assert conv.snapshot().entries[0].origin == "summary"
    # 两协议重建后的第一条历史均是 user 摘要（OpenAI 前面额外有 system）。
    offset = 1 if protocol == "openai" else 0
    assert sent[-1]["messages"][offset]["role"] == "user"
    assert "本会话延续之前的对话" in sent[-1]["messages"][offset]["content"]


@pytest.mark.parametrize("protocol", ["anthropic", "openai"])
async def test_sdk_textual_manual_cancel_failure_then_chat(tmp_path, monkeypatch, protocol):
    monkeypatch.setattr("potatocode.context.manager.RETRY_DELAYS", (0.001, 0.001))
    sent = []
    stream = BlockingStream(wire(protocol, complete=False))

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        if len(sent) == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
        return response(wire(protocol, "bad" if len(sent) <= 4 else "完成"))

    async with connected(protocol, handler) as provider:
        app = app_for(tmp_path, provider, monkeypatch)
        async with app.run_test() as pilot:
            app.mode = Mode.PLAN
            before = app.conv.snapshot()
            await submit(app, pilot, "/compact")
            await asyncio.wait_for(stream.waiting.wait(), 2)
            assert app.state is SessionState.COMPACTING
            await pilot.press("escape")
            await idle(app)
            assert stream.closed and app.conv.snapshot() == before
            await submit(app, pilot, "/compact")
            await idle(app)
            assert len(sent) == 4 and app.conv.snapshot() == before
            assert all("tools" not in body for body in sent)
            assert all(r.purpose == "summary" for r in app.context_manager.ledger.records())
            await submit(app, pilot, "继续")
            await idle(app)
            assert len(sent) == 5 and app.conv.messages()[-1].content == "完成"
            assert app.mode is Mode.PLAN
