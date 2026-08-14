"""agent 单测：fake provider 驱动单轮闭环（AC8/AC9/F6）。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from potatocode.agent import Agent, Event, Phase
from potatocode.conversation import Conversation
from potatocode.llm import Message, StreamEvent, ToolCall, ToolDefinition
from potatocode.tool import new_default_registry


class FakeProvider:
    """fake provider：按请求次数切换脚本，模拟工具调用与续答。"""

    def __init__(self, script1: list[StreamEvent], script2: list[StreamEvent]) -> None:
        self._script1 = script1
        self._script2 = script2
        self._calls = 0
        self.tools_seen: list[list[ToolDefinition]] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    async def stream(
        self,
        msgs: list[Message],
        tools: list[ToolDefinition],
    ) -> AsyncIterator[StreamEvent]:
        self._calls += 1
        self.tools_seen.append(list(tools))
        script = self._script1 if self._calls == 1 else self._script2
        for ev in script:
            yield ev


async def _collect(agent: Agent, conv: Conversation) -> list[Event]:
    """异步消费 agent.run 事件流。"""
    return [ev async for ev in agent.run(conv)]


async def test_single_round_tool_loop():
    """AC8：请求#1 发起 read_file → 执行 → 结果回灌 → 请求#2 最终文本总结。"""
    reg = new_default_registry()
    provider = FakeProvider(
        script1=[
            StreamEvent(text="我来读取。"),
            StreamEvent(
                tool_calls=[
                    ToolCall(id="call_1", name="read_file", input='{"path": "pyproject.toml"}')
                ]
            ),
            StreamEvent(done=True),
        ],
        script2=[StreamEvent(text="文件已读取，共 46 行。"), StreamEvent(done=True)],
    )
    agent = Agent(provider, reg)
    conv = Conversation()
    conv.add_user("读一下 pyproject.toml 并总结")

    events = await _collect(agent, conv)

    # 事件序列：preamble 文本 → tool START/END → 最终文本 → done
    assert [e.text for e in events if e.text] == ["我来读取。", "文件已读取，共 46 行。"]
    tool_evs = [e.tool for e in events if e.tool is not None]
    assert [t.phase for t in tool_evs] == [Phase.START, Phase.END]
    assert tool_evs[0].name == "read_file"
    assert tool_evs[0].args.startswith('{"path"')
    assert not tool_evs[1].is_error and "46" in tool_evs[1].result
    assert events[-1].done

    # 历史：user → assistant(带工具) → tool(结果) → assistant(最终文本)
    msgs = conv.messages()
    assert [m.role for m in msgs] == ["user", "assistant", "tool", "assistant"]
    assert [c.id for c in msgs[1].tool_calls] == ["call_1"]
    assert msgs[2].tool_results[0].tool_call_id == "call_1"
    assert msgs[3].content == "文件已读取，共 46 行。"

    # 工具定义随两次请求注入（F3/AC1）
    assert len(provider.tools_seen) == 2
    assert [d.name for d in provider.tools_seen[0]] == [
        "read_file",
        "write_file",
        "edit_file",
        "bash",
        "glob",
        "grep",
    ]


async def test_single_round_limit():
    """AC9：请求#2 仍返回工具调用 → 不再执行，以单轮上限提示结束。"""
    reg = new_default_registry()
    provider = FakeProvider(
        script1=[
            StreamEvent(
                tool_calls=[
                    ToolCall(id="call_1", name="read_file", input='{"path": "pyproject.toml"}')
                ]
            ),
            StreamEvent(done=True),
        ],
        script2=[
            StreamEvent(
                tool_calls=[ToolCall(id="call_2", name="bash", input='{"command": "echo x"}')]
            ),
            StreamEvent(done=True),
        ],
    )
    agent = Agent(provider, reg)
    conv = Conversation()
    conv.add_user("两步任务")

    events = await _collect(agent, conv)
    tool_evs = [e.tool for e in events if e.tool is not None]
    # 只执行了一轮工具（read_file 一个），bash 未执行
    assert [t.phase for t in tool_evs] == [Phase.START, Phase.END]
    assert [t.name for t in tool_evs if t.phase == Phase.START] == ["read_file"]
    assert events[-1].done
    # 最终答复是单轮上限提示占位
    assert conv.messages()[-1].role == "assistant"
    assert "本轮完成" in conv.messages()[-1].content
