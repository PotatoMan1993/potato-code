"""完整 Agent 请求链：预检、紧急重发、工具只执行一次与取消记账。"""

import asyncio

import pytest

from context_helpers import ContextProvider, substantial_conversation, summary_events
from potatocode.agent import Agent
from potatocode.context.manager import ContextManager
from potatocode.conversation import Conversation
from potatocode.llm import ContextLimits, StopReason, StreamEvent, ToolCall, Usage
from potatocode.llm.errors import ContextWindowExceeded
from potatocode.permission import Mode, new_engine
from potatocode.tool import Registry, Result


class CountingTool:
    def name(self):
        return "count"

    def description(self):
        return "记录一次真实执行"

    def parameters(self):
        return {"type": "object", "properties": {}}

    def read_only(self):
        return False

    def __init__(self, content="执行完毕"):
        self.calls = 0
        self.content = content

    async def execute(self, args):
        self.calls += 1
        return Result(self.content)


def setup_agent(tmp_path, provider, conv=None, tool=None):
    conv = conv if conv is not None else substantial_conversation()
    engine, _ = new_engine(str(tmp_path))
    registry = Registry()
    if tool is not None:
        registry.register(tool)
    context = ContextManager(conv, engine)
    return Agent(provider, registry, engine, context=context), conv, context


async def run(agent, conv, cancel=None):
    return [e async for e in agent.run(conv, Mode.BYPASS, cancel or asyncio.Event())]


def overflow():
    return [StreamEvent(usage=Usage(111, 2)), StreamEvent(err=ContextWindowExceeded())]


async def test_emergency_replays_request_once_without_repeating_tool(tmp_path):
    tool = CountingTool()
    provider = ContextProvider(
        normal_scripts=[
            [
                StreamEvent(tool_calls=[ToolCall("once", "count", "{}")]),
                StreamEvent(done=True, stop_reason=StopReason.TOOL_CALLS),
            ],
            overflow(),
        ]
    )
    agent, conv, context = setup_agent(tmp_path, provider, tool=tool)
    conv.add_user("继续原来的任务")
    events = await run(agent, conv)
    assert events[-1].done and not any(e.err for e in events)
    assert [r.purpose for r in provider.requests] == ["normal", "normal", "summary", "normal"]
    assert tool.calls == 1
    assert sum(m.content == "继续原来的任务" for m in conv.messages()) == 1
    assert [r.status for r in context.ledger.records()] == [
        "success",
        "failed",
        "success",
        "success",
    ]
    assert any(e.reset_reply for e in events)


async def test_second_overflow_stops_without_recursive_summary(tmp_path):
    provider = ContextProvider(normal_scripts=[overflow(), overflow()])
    agent, conv, context = setup_agent(tmp_path, provider)
    events = await run(agent, conv)
    assert any(isinstance(e.err, ContextWindowExceeded) for e in events)
    assert [r.purpose for r in provider.requests] == ["normal", "summary", "normal"]
    assert len(context.ledger.records()) == 3


async def test_no_legal_prefix_reports_unavailable_even_after_actual_overflow(tmp_path):
    conv = Conversation()
    conv.add_user("保持原文")
    provider = ContextProvider(normal_scripts=[overflow()])
    agent, conv, _ = setup_agent(tmp_path, provider, conv)
    events = await run(agent, conv)
    assert len(provider.requests) == 1
    assert any(e.err and "近期保留" in str(e.err) for e in events)
    assert conv.messages()[0].content == "保持原文"


async def test_light_spills_result_before_tool_followup_request(tmp_path):
    tool = CountingTool("长" * 50001)
    provider = ContextProvider(
        normal_scripts=[
            [
                StreamEvent(tool_calls=[ToolCall("large", "count", "{}")]),
                StreamEvent(done=True, stop_reason=StopReason.TOOL_CALLS),
            ]
        ]
    )
    conv = Conversation()
    conv.add_user("执行工具")
    agent, conv, _ = setup_agent(tmp_path, provider, conv, tool)
    events = await run(agent, conv)
    assert events[-1].done and tool.calls == 1
    result = provider.requests[1].messages[-1].tool_results[0]
    assert result.content.startswith("长" * 1500) and len(result.content) < 50000
    path = next(tmp_path.glob(".potatocode/context/*/tool-results/*.txt"))
    assert path.read_text(encoding="utf-8") == tool.content


async def test_usage_trigger_runs_summary_before_next_tool_iteration(tmp_path):
    tool = CountingTool()
    provider = ContextProvider(
        normal_scripts=[
            [
                StreamEvent(tool_calls=[ToolCall("auto", "count", "{}")]),
                StreamEvent(usage=Usage(95000, 10)),
                StreamEvent(done=True, stop_reason=StopReason.TOOL_CALLS),
            ]
        ]
    )
    agent, conv, _ = setup_agent(tmp_path, provider, tool=tool)
    events = await run(agent, conv)
    assert events[-1].done and tool.calls == 1
    assert [r.purpose for r in provider.requests] == ["normal", "summary", "normal"]
    assert any(e.context and e.context.reason == "auto" for e in events)


@pytest.mark.parametrize("error", [RuntimeError("网络错误"), ValueError("鉴权错误")])
async def test_other_failures_never_trigger_emergency_and_usage_precedes_error(tmp_path, error):
    provider = ContextProvider(
        normal_scripts=[
            [
                StreamEvent(text="未完成的片段"),
                StreamEvent(usage=Usage(12, 3)),
                StreamEvent(err=error),
            ]
        ]
    )
    agent, conv, context = setup_agent(tmp_path, provider)
    events = await run(agent, conv)
    assert len(provider.requests) == 1 and context.ledger.records()[0].status == "failed"
    assert next(i for i, e in enumerate(events) if e.usage) < next(
        i for i, e in enumerate(events) if e.err
    )
    assert not any(m.content == "未完成的片段" for m in conv.messages())


async def test_summary_three_failures_stop_original_request(tmp_path, monkeypatch):
    monkeypatch.setattr("potatocode.context.manager.RETRY_DELAYS", (0.001, 0.001))
    provider = ContextProvider(
        normal_scripts=[overflow()], summary_scripts=[summary_events("bad") for _ in range(3)]
    )
    agent, conv, _ = setup_agent(tmp_path, provider)
    events = await run(agent, conv)
    assert any(e.err and "熔断" in str(e.err) for e in events)
    assert [r.purpose for r in provider.requests] == ["normal", "summary", "summary", "summary"]


async def test_cancel_silent_normal_stream_closes_it_and_keeps_usage(tmp_path):
    provider = ContextProvider(normal_scripts=[[StreamEvent(usage=Usage(20, 1)), asyncio.Event()]])
    agent, conv, context = setup_agent(tmp_path, provider)
    cancel = asyncio.Event()
    task = asyncio.create_task(run(agent, conv, cancel))
    await asyncio.wait_for(provider.waiting.wait(), 1)
    cancel.set()
    events = await asyncio.wait_for(task, 1)
    assert provider.closed == 1 and any(e.usage for e in events)
    assert context.ledger.records()[0].status == "cancelled"


async def test_missing_window_blocks_network_and_keeps_user_input(tmp_path):
    provider = ContextProvider()
    provider.context_limits = ContextLimits()
    conv = Conversation()
    conv.add_user("原始请求")
    agent, conv, _ = setup_agent(tmp_path, provider, conv)
    events = await run(agent, conv)
    assert not provider.requests and any(e.err and "context_window" in str(e.err) for e in events)
    assert conv.messages()[0].content == "原始请求"
