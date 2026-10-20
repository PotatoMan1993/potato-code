"""Agent × Hook 集成单测：PreToolUse 拦截、PreUserMessage 注入、Stop emit（ch14 T17/F31-F33）。

复用 test_agent.py 的 FakeProvider 模式：真实 hook.Engine 注入合成 rules，
断言 agent 事件流中拦截结果与 reminder 装配的位置正确。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator

from potatocode.agent import Agent, Event, Mode, new_runtime
from potatocode.conversation import Conversation
from potatocode.hook import Engine as HookEngine
from potatocode.hook import Event as HookEvent
from potatocode.hook.rule import Action, ActionType, PromptAction, Rule, ShellAction
from potatocode.llm import Request, StreamEvent, ToolCall
from potatocode.permission import Outcome, new_engine
from potatocode.tool import Registry, new_default_registry


class FakeProvider:
    """极简 fake provider：按脚本返回事件流并记录请求（同 test_agent.FakeProvider）。"""

    def __init__(self, scripts: list[list[StreamEvent]]) -> None:
        self._scripts = scripts
        self.calls = 0
        self.reminders_seen: list[str] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    async def stream(self, req: Request) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        self.reminders_seen.append(req.reminder)
        script = self._scripts[self.calls - 1] if self.calls <= len(self._scripts) else []
        for ev in script:
            if ev.tool_calls:
                yield StreamEvent(
                    tool_calls=[
                        ToolCall(id=f"c{self.calls}_{i}", name=c.name, input=c.input)
                        for i, c in enumerate(ev.tool_calls)
                    ]
                )
            else:
                yield ev


def _tool_call(name: str, args: str) -> StreamEvent:
    return StreamEvent(tool_calls=[ToolCall(id="x", name=name, input=args)])


def _hook_engine(*rules: Rule) -> HookEngine:
    return HookEngine(list(rules), ["test.yaml"])


def _make_agent(provider, hook_engine: HookEngine, registry: Registry) -> Agent:
    root = tempfile.mkdtemp(prefix="potatocode-hook-test-")
    engine, _ = new_engine(root)
    runtime = new_runtime(workspace=root)
    return Agent(
        provider,
        registry,
        engine,
        runtime=runtime,
        hook_engine=hook_engine,
    )


async def _collect(agent: Agent, conv: Conversation) -> list[Event]:
    cancel = asyncio.Event()
    events: list[Event] = []
    async for ev in agent.run(conv, Mode.DEFAULT, cancel):
        if ev.approval is not None:
            ev.approval.respond.set_result(Outcome.ALLOW_ONCE)
            continue
        events.append(ev)
    return events


async def test_pre_tool_use_hook_blocks_tool_execution():
    """PreToolUse 拦截：tool_result 为 `[hook <name>] <reason>`，PhaseStart/End 仍 emit（F32）。"""
    block_rule = Rule(
        name="block-write",
        event=HookEvent.PRE_TOOL_USE,
        action=Action(
            type=ActionType.SHELL, shell=ShellAction(command="echo blocked by hook >&2; exit 2")
        ),
    )
    provider = FakeProvider(
        [
            [_tool_call("write_file", json.dumps({"path": "hooked.txt", "content": "hi"}))],
            [],  # 第二轮自然完成
        ]
    )
    registry = new_default_registry()
    agent = _make_agent(provider, _hook_engine(block_rule), registry)
    conv = Conversation.from_messages([])
    events = await _collect(agent, conv)

    tool_events = [ev.tool for ev in events if ev.tool is not None]
    assert tool_events, "拦截路径也应发出工具 PhaseStart/PhaseEnd 事件"
    assert all(te.phase is not None for te in tool_events)
    end_events = [te for te in tool_events if te.result]
    assert len(end_events) == 1
    assert end_events[0].result == "[hook block-write] blocked by hook"
    assert end_events[0].is_error is True
    # 文件未被写入
    assert not os.path.exists(os.path.join(agent.runtime.workspace, "hooked.txt"))
    # 历史里回灌了拦截结果（保持配对合法）
    roles = [m.role for m in conv.messages()]
    assert roles[-1] == "assistant"


async def test_pre_user_message_prompt_lands_in_next_request_reminder():
    """PreUserMessage 注入的 prompt 出现在下一次请求的 reminder 串中（F20/F33）。"""
    inject_rule = Rule(
        name="remind-zh",
        event=HookEvent.PRE_USER_MESSAGE,
        action=Action(type=ActionType.PROMPT, prompt=PromptAction(text="请用 zh-CN 回复")),
    )
    provider = FakeProvider([[]])  # 一轮即自然完成
    registry = new_default_registry()
    agent = _make_agent(provider, _hook_engine(inject_rule), registry)
    conv = Conversation.from_messages([])
    conv.add_user("hello")
    await _collect(agent, conv)

    assert provider.reminders_seen, "至少发出过一次请求"
    first_reminder = provider.reminders_seen[0]
    assert "请用 zh-CN 回复" in first_reminder
    # 注入只在本轮有效：取出即清空（F21/N4）
    assert agent.runtime.pending_reminders == []


async def test_stop_hook_emits_before_done():
    """Stop hook 在 Done 事件之前被派发：prompt 注入发生在同一轮（F9）。"""
    stop_rule = Rule(
        name="on-stop",
        event=HookEvent.STOP,
        action=Action(type=ActionType.PROMPT, prompt=PromptAction(text="stop-payload")),
    )
    provider = FakeProvider([[]])
    registry = new_default_registry()
    agent = _make_agent(provider, _hook_engine(stop_rule), registry)
    conv = Conversation.from_messages([])
    conv.add_user("hi")
    events = await _collect(agent, conv)

    assert any(ev.done for ev in events), "自然完成应有 done 事件"
    # Stop hook 的 prompt 进了队列（下轮 reminder 才消费）——验证 runtime 状态
    assert agent.runtime.pending_reminders == ["stop-payload"]


async def test_hook_prompt_joins_plan_reminder_in_same_request():
    """plan 模式下 hook 注入与 plan reminder 拼进同一轮请求（F33/checklist 协同项）。"""
    inject_rule = Rule(
        name="remind-format",
        event=HookEvent.PRE_USER_MESSAGE,
        action=Action(type=ActionType.PROMPT, prompt=PromptAction(text="HOOK_REMINDER_MARK")),
    )
    provider = FakeProvider([[]])
    registry = new_default_registry()
    agent = _make_agent(provider, _hook_engine(inject_rule), registry)
    conv = Conversation.from_messages([])
    conv.add_user("hello")
    cancel = asyncio.Event()
    async for ev in agent.run(conv, Mode.PLAN, cancel):
        if ev.approval is not None:
            ev.approval.respond.set_result(Outcome.ALLOW_ONCE)
    first_reminder = provider.reminders_seen[0]
    assert "PLAN MODE" in first_reminder  # plan reminder 在前
    assert first_reminder.index("PLAN MODE") < first_reminder.index("HOOK_REMINDER_MARK")


async def test_dispatch_without_hook_engine_is_noop():
    """未注入 hook 引擎：全部 emit 为空操作，主流程不受影响。"""
    provider = FakeProvider([[]])
    registry = new_default_registry()
    root = tempfile.mkdtemp(prefix="potatocode-nohook-")
    engine, _ = new_engine(root)
    agent = Agent(provider, registry, engine, runtime=new_runtime(workspace=root))
    conv = Conversation.from_messages([])
    conv.add_user("hi")
    events = await _collect(agent, conv)
    assert any(ev.done for ev in events)
