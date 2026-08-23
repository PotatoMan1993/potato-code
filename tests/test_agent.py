"""agent 单测：fake provider 驱动 ReAct 循环（ch04）。

覆盖：多轮连环（AC1）、自然完成（AC2）、迭代上限（AC3）、连续未知工具（AC4）、
流出错恢复（AC5）、保序分批并发（AC8）、取消历史一致（AC9）、Plan 工具集（AC13）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from potatocode import prompt
from potatocode.agent import (
    MAX_ITERATIONS,
    MAX_UNKNOWN_RUN,
    NOTICE_CANCELLED,
    NOTICE_MAX_ITER,
    NOTICE_STREAM_ERR,
    NOTICE_UNKNOWN_TOOLS,
    Agent,
    Event,
    Mode,
    Phase,
)
from potatocode.conversation import Conversation
from potatocode.llm import Message, StreamEvent, ToolCall, ToolDefinition
from potatocode.llm import Usage as LlmUsage
from potatocode.tool import Registry, Result, _parse_args, new_default_registry


class FakeProvider:
    """fake provider：按请求次数逐轮返回脚本事件，并记录请求供断言。

    `scripts` 每轮一个事件列表；`repeat` 在脚本耗尽后每轮恒返该脚本。
    工具调用 id 按轮重打，避免多轮历史中出现重复 id。
    """

    def __init__(
        self,
        scripts: list[list[StreamEvent]] | None = None,
        repeat: list[StreamEvent] | None = None,
    ) -> None:
        self._scripts = scripts or []
        self._repeat = repeat
        self.calls = 0
        self.tools_seen: list[list[ToolDefinition]] = []
        self.suffix_seen: list[str] = []

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
        system_suffix: str = "",
    ) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        self.tools_seen.append(list(tools))
        self.suffix_seen.append(system_suffix)
        if self.calls <= len(self._scripts):
            script = self._scripts[self.calls - 1]
        else:
            script = self._repeat if self._repeat is not None else []
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
    """构造单个工具调用的脚本事件（id 由 FakeProvider 按轮重打）。"""
    return StreamEvent(tool_calls=[ToolCall(id="x", name=name, input=args)])


def _turn_with_tools(*calls: ToolCall) -> list[StreamEvent]:
    """构造「工具调用回合」脚本：一次上抛全部调用。"""
    return [StreamEvent(tool_calls=list(calls)), StreamEvent(done=True)]


async def _collect(
    agent: Agent,
    conv: Conversation,
    mode: Mode = Mode.NORMAL,
    cancel: asyncio.Event | None = None,
) -> list[Event]:
    """异步消费 agent.run 事件流（默认普通模式、不取消）。"""
    if cancel is None:
        cancel = asyncio.Event()
    return [ev async for ev in agent.run(conv, mode, cancel)]


# ────────── 场景 A：多轮自动连环（AC1/AC2/F1/F4/F6/F8） ──────────


async def test_multi_round_tool_chain(tmp_path):
    """AC1：读 → 写 → 纯文本收尾，跨多轮自动连环，无需中途催促。"""
    target = tmp_path / "summary.txt"
    reg = new_default_registry()
    provider = FakeProvider(
        scripts=[
            # 第 1 轮：读文件（附用量事件，验证 F8 透传）
            [
                StreamEvent(text="我先读一下。"),
                _tool_call("read_file", '{"path": "pyproject.toml"}'),
                StreamEvent(usage=LlmUsage(input_tokens=100, output_tokens=20)),
                StreamEvent(done=True),
            ],
            # 第 2 轮：续答里再次请求工具——不再丢弃（ch04 关键变化）
            [
                StreamEvent(text="再写摘要。"),
                _tool_call(
                    "write_file", json.dumps({"path": str(target), "content": "一句话摘要"})
                ),
                StreamEvent(usage=LlmUsage(input_tokens=150, output_tokens=30)),
                StreamEvent(done=True),
            ],
            # 第 3 轮：无工具调用的纯文本 → 自然完成
            [
                StreamEvent(text="已完成：读取并写出摘要。"),
                StreamEvent(usage=LlmUsage(input_tokens=200, output_tokens=10)),
                StreamEvent(done=True),
            ],
        ]
    )
    agent = Agent(provider, reg)
    conv = Conversation()
    conv.add_user("读 pyproject.toml 并写一份摘要")

    events = await _collect(agent, conv)

    # 进度事件：第 1/2/3 轮按序（F9）
    assert [e.iter for e in events if e.iter] == [1, 2, 3]
    # 事件类型完备：text / tool / usage / iter / done（AC6）
    assert any(e.text for e in events)
    assert any(e.tool is not None for e in events)
    assert any(e.usage is not None for e in events)
    assert events[-1].done
    assert all(e.err is None for e in events)
    assert all(not e.notice for e in events)

    # 文本增量：两个 preamble + 最终答复
    assert [e.text for e in events if e.text] == [
        "我先读一下。",
        "再写摘要。",
        "已完成：读取并写出摘要。",
    ]

    # 工具事件：两对 START/END，按调用序
    tool_evs = [e.tool for e in events if e.tool is not None]
    assert [t.phase for t in tool_evs] == [Phase.START, Phase.END, Phase.START, Phase.END]
    assert [t.name for t in tool_evs] == ["read_file", "read_file", "write_file", "write_file"]
    assert tool_evs[0].args.startswith('{"path"')
    assert not tool_evs[1].is_error and "name = " in tool_evs[1].result
    assert not tool_evs[3].is_error and "已写入" in tool_evs[3].result

    # 用量：逐轮上抛（F8）
    usages = [e.usage for e in events if e.usage is not None]
    assert [(u.input, u.output) for u in usages] == [(100, 20), (150, 30), (200, 10)]

    # 自然完成停止（AC2）：恰好 3 次请求，不再多发
    assert provider.calls == 3

    # 历史（F6）：user → assistant(tool_use) → tool → assistant(tool_use) → tool → assistant
    msgs = conv.messages()
    assert [m.role for m in msgs] == ["user", "assistant", "tool", "assistant", "tool", "assistant"]
    assert [c.name for c in msgs[1].tool_calls] == ["read_file"]
    # 流式收集双路（AC7）：完整工具调用参数拼齐、可解析
    assert json.loads(msgs[1].tool_calls[0].input) == {"path": "pyproject.toml"}
    assert msgs[2].tool_results[0].tool_call_id == msgs[1].tool_calls[0].id
    assert [c.name for c in msgs[3].tool_calls] == ["write_file"]
    assert msgs[4].tool_results[0].tool_call_id == msgs[3].tool_calls[0].id
    assert msgs[5].content == "已完成：读取并写出摘要。"
    assert target.read_text(encoding="utf-8") == "一句话摘要"


async def test_natural_completion_empty_text():
    """模型回复无文本无工具：以占位文本收尾，避免空 assistant 回合。"""
    provider = FakeProvider(scripts=[[StreamEvent(done=True)]])
    agent = Agent(provider, new_default_registry())
    conv = Conversation()
    conv.add_user("hi")

    events = await _collect(agent, conv)

    assert events[-1].done
    assert conv.last_role() == "assistant"
    assert conv.messages()[-1].content  # 占位文本非空


# ────────── 场景 B：迭代上限兜底（AC3） ──────────


async def test_max_iterations_stop():
    """AC3：模型反复请求工具不收手，恰好在 MAX_ITERATIONS 轮停止并提示。"""
    provider = FakeProvider(
        repeat=[
            _tool_call("read_file", '{"path": "pyproject.toml"}'),
            StreamEvent(done=True),
        ]
    )
    agent = Agent(provider, new_default_registry())
    conv = Conversation()
    conv.add_user("一直读")

    events = await _collect(agent, conv)

    assert provider.calls == MAX_ITERATIONS  # 恰好上限轮请求，不无限循环
    assert [e.notice for e in events if e.notice] == [NOTICE_MAX_ITER]
    assert events[-1].done
    # 历史合法收尾（F6）：末尾是 assistant 文本（上限提示）
    assert conv.last_role() == "assistant"
    assert conv.messages()[-1].content == NOTICE_MAX_ITER


# ────────── 场景 C：连续未知工具停止（AC4） ──────────


def _unknown_turn() -> list[StreamEvent]:
    return [_tool_call("hallucinate_tool", "{}"), StreamEvent(done=True)]


async def test_unknown_tools_stop():
    """AC4：连续 MAX_UNKNOWN_RUN 轮只请求未注册工具即停并提示。"""
    provider = FakeProvider(repeat=_unknown_turn())
    agent = Agent(provider, new_default_registry())
    conv = Conversation()
    conv.add_user("用你的魔法工具")

    events = await _collect(agent, conv)

    assert provider.calls == MAX_UNKNOWN_RUN
    assert [e.notice for e in events if e.notice] == [NOTICE_UNKNOWN_TOOLS]
    assert events[-1].done
    assert conv.last_role() == "assistant"
    # 未知工具的结构化错误已回灌（给过模型纠偏机会）
    assert "未知工具" in conv.messages()[2].tool_results[0].content


async def test_unknown_tools_counter_reset_by_known():
    """AC4：混入一个已注册工具即重置计数，不提前停止。"""
    provider = FakeProvider(
        scripts=[
            _unknown_turn(),  # r1：unknown_run=1
            _turn_with_tools(
                ToolCall(id="x", name="read_file", input='{"path": "pyproject.toml"}')
            ),  # r2：已知工具 → 重置为 0
            _unknown_turn(),  # r3：unknown_run=1
            _unknown_turn(),  # r4：unknown_run=2
            _unknown_turn(),  # r5：unknown_run=3 → 停
        ]
    )
    agent = Agent(provider, new_default_registry())
    conv = Conversation()
    conv.add_user("混合调用")

    events = await _collect(agent, conv)

    assert provider.calls == 5  # 未在第 3 轮提前停
    assert [e.notice for e in events if e.notice] == [NOTICE_UNKNOWN_TOOLS]
    assert events[-1].done


# ────────── 场景 D：保序分批并发（AC8/F5/N6） ──────────


class _ProbeReadOnly:
    """插桩只读工具：记录并发执行峰值与完成数。"""

    def __init__(self, state: dict) -> None:
        self._state = state

    def name(self) -> str:
        return "probe_ro"

    def description(self) -> str:
        return "只读探针（测试用）"

    def parameters(self) -> dict:
        return {"type": "object", "properties": {"tag": {"type": "string"}}}

    def read_only(self) -> bool:
        return True

    async def execute(self, args: str) -> Result:
        s = self._state
        s["running"] += 1
        s["peak"] = max(s["peak"], s["running"])
        await asyncio.sleep(0.05)  # 制造重叠窗口
        s["running"] -= 1
        s["ro_done"] += 1
        return Result(content=f"ro-{_parse_args(args).get('tag', '')}")


class _ProbeWrite:
    """插桩有副作用工具：记录开始执行时只读工具的完成数。"""

    def __init__(self, state: dict) -> None:
        self._state = state

    def name(self) -> str:
        return "probe_rw"

    def description(self) -> str:
        return "副作用探针（测试用）"

    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    def read_only(self) -> bool:
        return False

    async def execute(self, args: str) -> Result:
        self._state["ro_done_at_rw_start"] = self._state["ro_done"]
        return Result(content="rw-done")


def _probe_registry() -> tuple[Registry, dict]:
    state = {"running": 0, "peak": 0, "ro_done": 0, "ro_done_at_rw_start": -1}
    reg = Registry()
    reg.register(_ProbeReadOnly(state))
    reg.register(_ProbeWrite(state))
    return reg, state


async def test_concurrent_batch_order_preserved():
    """AC8：[ro, ro, rw] → 两只读并发、写串行在其后，结果按调用序回灌。"""
    reg, state = _probe_registry()
    provider = FakeProvider(
        scripts=[
            _turn_with_tools(
                ToolCall(id="x", name="probe_ro", input='{"tag": "a"}'),
                ToolCall(id="x", name="probe_ro", input='{"tag": "b"}'),
                ToolCall(id="x", name="probe_rw", input="{}"),
            ),
            [StreamEvent(text="完成。"), StreamEvent(done=True)],
        ]
    )
    agent = Agent(provider, reg)
    conv = Conversation()
    conv.add_user("三步任务")

    events = await _collect(agent, conv)

    # 两只读确实并发（峰值 ≥2），写工具在两只读都完成后才开始
    assert state["peak"] >= 2
    assert state["ro_done_at_rw_start"] == 2

    # 事件顺序按批：批 1（两只读并发）S,S,E,E → 批 2（写串行）S,E；
    # 批内「开始按序、结束按序」，整体工具行顺序 == 调用序（N3）
    tool_evs = [e.tool for e in events if e.tool is not None]
    assert [t.phase for t in tool_evs] == [
        Phase.START,
        Phase.START,
        Phase.END,
        Phase.END,
        Phase.START,
        Phase.END,
    ]
    assert [t.name for t in tool_evs] == [
        "probe_ro",
        "probe_ro",
        "probe_ro",
        "probe_ro",
        "probe_rw",
        "probe_rw",
    ]
    assert [t.result for t in tool_evs if t.phase == Phase.END] == ["ro-a", "ro-b", "rw-done"]

    # 结果按原始调用顺序回灌进历史（按内容/ID 比对）
    tool_msg = conv.messages()[2]
    assert [r.content for r in tool_msg.tool_results] == ["ro-a", "ro-b", "rw-done"]
    assert [r.tool_call_id for r in tool_msg.tool_results] == [
        c.id for c in conv.messages()[1].tool_calls
    ]
    assert events[-1].done


async def test_read_only_batch_of_one_still_works():
    """单个只读调用也走分批路径，正常完成。"""
    reg, state = _probe_registry()
    provider = FakeProvider(
        scripts=[
            _turn_with_tools(ToolCall(id="x", name="probe_ro", input='{"tag": "solo"}')),
            [StreamEvent(text="完成。"), StreamEvent(done=True)],
        ]
    )
    agent = Agent(provider, reg)
    conv = Conversation()
    conv.add_user("单步")

    events = await _collect(agent, conv)

    assert state["peak"] == 1
    assert conv.messages()[2].tool_results[0].content == "ro-solo"
    assert events[-1].done


# ────────── 场景 E：取消与历史一致（AC9/F6/F7/N5） ──────────


class _SlowRead:
    """插桩慢只读工具：阻塞 10s，等待被取消。"""

    def name(self) -> str:
        return "slow_read"

    def description(self) -> str:
        return "慢读取探针（测试用）"

    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    def read_only(self) -> bool:
        return True

    async def execute(self, args: str) -> Result:
        await asyncio.sleep(10)
        return Result(content="never")


async def test_cancel_during_execution_keeps_history_valid():
    """AC9：执行中取消 → 历史配对合法、无悬空 tool_use，且可继续对话。"""
    reg = Registry()
    reg.register(_SlowRead())
    provider = FakeProvider(
        scripts=[
            _turn_with_tools(ToolCall(id="x", name="slow_read", input="{}")),
            # 取消后的下一轮：纯文本正常跑（验证角色交替未坏）
            [StreamEvent(text="我回来了。"), StreamEvent(done=True)],
        ]
    )
    agent = Agent(provider, reg)
    conv = Conversation()
    conv.add_user("慢任务")

    cancel = asyncio.Event()
    events: list[Event] = []

    async def first_turn() -> None:
        async for ev in agent.run(conv, Mode.NORMAL, cancel):
            events.append(ev)

    task = asyncio.create_task(first_turn())
    await asyncio.sleep(0.05)  # 等工具开始执行
    cancel.set()
    await asyncio.wait_for(task, timeout=5)  # 及时停止，不等满 10s（N5）

    # 工具事件成对收尾（含「已取消」结果）
    tool_evs = [e.tool for e in events if e.tool is not None]
    assert [t.phase for t in tool_evs] == [Phase.START, Phase.END]
    assert tool_evs[1].is_error and tool_evs[1].result == NOTICE_CANCELLED
    # 取消路径不 yield done/notice：generator 终结即本轮结束
    assert all(not e.done for e in events)
    assert all(not e.notice for e in events)

    # 历史合法（F6）：user → assistant(tool_use) → tool(已取消) → assistant(兜底文本)
    msgs = conv.messages()
    assert [m.role for m in msgs] == ["user", "assistant", "tool", "assistant"]
    assert msgs[1].tool_calls[0].id == msgs[2].tool_results[0].tool_call_id  # 无悬空配对
    assert msgs[2].tool_results[0].is_error
    assert msgs[2].tool_results[0].content == NOTICE_CANCELLED
    assert msgs[3].content == NOTICE_CANCELLED

    # 取消后会话可继续：下一轮纯文本正常完成，无角色交替错误
    conv.add_user("继续")
    events2 = await _collect(agent, conv)
    assert events2[-1].done
    assert conv.messages()[-1].content == "我回来了。"


async def test_cancel_before_stream_starts():
    """流开始前就取消：不发请求，历史以「已取消」收尾。"""
    provider = FakeProvider(scripts=[[StreamEvent(text="不该出现"), StreamEvent(done=True)]])
    agent = Agent(provider, new_default_registry())
    conv = Conversation()
    conv.add_user("还没开始就取消")

    cancel = asyncio.Event()
    cancel.set()
    events = await _collect(agent, conv, cancel=cancel)

    assert provider.calls == 0  # 未发起请求
    assert all(not e.done for e in events)
    assert conv.last_role() == "assistant"
    assert conv.messages()[-1].content == NOTICE_CANCELLED


# ────────── 场景 F：Plan Mode 工具集（AC13/F10） ──────────


async def test_plan_mode_read_only_tools_and_suffix():
    """AC13：PLAN 模式只注入只读工具定义 + 计划态系统后缀。"""
    provider = FakeProvider(scripts=[[StreamEvent(text="计划：第一步…"), StreamEvent(done=True)]])
    agent = Agent(provider, new_default_registry())
    conv = Conversation()
    conv.add_user("给登录功能加单测的方案")

    events = await _collect(agent, conv, mode=Mode.PLAN)

    assert events[-1].done
    assert [d.name for d in provider.tools_seen[0]] == ["read_file", "glob", "grep"]
    assert provider.suffix_seen[0] == prompt.PLAN_MODE_REMINDER
    assert conv.messages()[-1].content == "计划：第一步…"


async def test_normal_mode_full_tools_and_no_suffix():
    """对照：NORMAL 模式注入全部 6 个工具、无系统后缀。"""
    provider = FakeProvider(scripts=[[StreamEvent(text="好"), StreamEvent(done=True)]])
    agent = Agent(provider, new_default_registry())
    conv = Conversation()
    conv.add_user("hi")

    events = await _collect(agent, conv)

    assert events[-1].done
    assert [d.name for d in provider.tools_seen[0]] == [
        "read_file",
        "write_file",
        "edit_file",
        "bash",
        "glob",
        "grep",
    ]
    assert provider.suffix_seen[0] == ""


# ────────── 场景 G：流出错恢复（AC5/F2） ──────────


async def test_stream_error_stops_turn_and_recovers():
    """AC5：流出错 → 停止本轮、历史兜底收尾；程序不退出，下一轮可正常继续。"""
    provider = FakeProvider(
        scripts=[
            [StreamEvent(err=RuntimeError("boom"))],
            [StreamEvent(text="恢复了。"), StreamEvent(done=True)],
        ]
    )
    agent = Agent(provider, new_default_registry())
    conv = Conversation()
    conv.add_user("第一条会失败")

    events = await _collect(agent, conv)

    # err 事件上抛；无 done（TUI 以 err 收尾本轮）
    assert isinstance(events[0].err, RuntimeError) or any(
        isinstance(e.err, RuntimeError) for e in events
    )
    assert all(not e.done for e in events)
    # 历史兜底（F6）：user 之后补 assistant 文本，角色交替合法
    assert conv.last_role() == "assistant"
    assert conv.messages()[-1].content == NOTICE_STREAM_ERR

    # 下一轮正常继续（不出现连续同角色导致 400 的形态）
    conv.add_user("再来一次")
    events2 = await _collect(agent, conv)
    assert events2[-1].done
    assert conv.messages()[-1].content == "恢复了。"
    assert [m.role for m in conv.messages()] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
