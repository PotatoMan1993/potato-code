"""agent 单测：fake provider 驱动 ReAct 循环（ch04）。

覆盖：多轮连环（AC1）、自然完成（AC2）、迭代上限（AC3）、连续未知工具（AC4）、
流出错恢复（AC5）、保序分批并发（AC8）、取消历史一致（AC9）、Plan 工具集（AC13）。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

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
    new_runtime,
)
from potatocode.compact import estimate_tokens
from potatocode.conversation import Conversation
from potatocode.llm import (
    PromptTooLongError,
    Request,
    StreamEvent,
    System,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from potatocode.llm import Usage as LlmUsage
from potatocode.permission import Outcome, new_engine
from potatocode.permission.engine import Engine
from potatocode.tool import Registry, Result, _parse_args, new_default_registry


def _engine(root: str | None = None) -> Engine:
    """构造一个根于 root（默认 cwd）的权限引擎，供 Agent 注入。"""
    engine, _ = new_engine(root or os.getcwd())
    return engine


def _make_agent(provider, registry: Registry, root: str | None = None) -> Agent:
    """构造带权限引擎与会话运行时的 Agent。

    会话落盘目录固定放在临时目录：这些用例只关心会话行为，不该往工作目录写文件。
    """
    return Agent(
        provider,
        registry,
        _engine(root),
        runtime=new_runtime(workspace=root or tempfile.mkdtemp(prefix="potatocode-test-")),
    )


class FakeProvider:
    """fake provider：按请求次数逐轮返回脚本事件，并记录请求供断言。

    `scripts` 每轮一个事件列表；`repeat` 在脚本耗尽后每轮恒返该脚本。
    工具调用 id 按轮重打，避免多轮历史中出现重复 id。
    记录 `req.tools` / `req.system`（stable+environment）/ `req.reminder` 供断言。
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
        self.systems_seen: list[System] = []
        self.reminders_seen: list[str] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    async def stream(self, req: Request) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        self.tools_seen.append(list(req.tools or []))
        self.systems_seen.append(req.system)
        self.reminders_seen.append(req.reminder)
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
    mode: Mode = Mode.DEFAULT,
    cancel: asyncio.Event | None = None,
) -> list[Event]:
    """异步消费 agent.run 事件流（默认普通模式、不取消）。

    遇到 ApprovalRequest 自动以 ALLOW_ONCE 放行——这些「机制」用例不关心权限，
    避免 default 模式下写文件/未知工具触发人在回路而挂起。
    """
    if cancel is None:
        cancel = asyncio.Event()
    events: list[Event] = []
    async for ev in agent.run(conv, mode, cancel):
        if ev.approval is not None:
            ev.approval.respond.set_result(Outcome.ALLOW_ONCE)
            continue
        events.append(ev)
    return events


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
    agent = _make_agent(provider, reg, root=str(tmp_path))
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
    agent = _make_agent(provider, new_default_registry())
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
    agent = _make_agent(provider, new_default_registry())
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
    agent = _make_agent(provider, new_default_registry())
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
    agent = _make_agent(provider, new_default_registry())
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
    agent = _make_agent(provider, reg)
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
    agent = _make_agent(provider, reg)
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
    agent = _make_agent(provider, reg)
    conv = Conversation()
    conv.add_user("慢任务")

    cancel = asyncio.Event()
    events: list[Event] = []

    async def first_turn() -> None:
        async for ev in agent.run(conv, Mode.DEFAULT, cancel):
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
    agent = _make_agent(provider, new_default_registry())
    conv = Conversation()
    conv.add_user("还没开始就取消")

    cancel = asyncio.Event()
    cancel.set()
    events = await _collect(agent, conv, cancel=cancel)

    assert provider.calls == 0  # 未发起请求
    assert all(not e.done for e in events)
    assert conv.last_role() == "assistant"
    assert conv.messages()[-1].content == NOTICE_CANCELLED


# ────────── 场景 F：Plan Mode 工具集与 reminder（AC13/F7/F10） ──────────


async def test_plan_mode_read_only_tools_and_reminder():
    """AC13/F7：PLAN 模式只注入只读工具定义 + 首轮注入完整 reminder。"""
    provider = FakeProvider(scripts=[[StreamEvent(text="计划：第一步…"), StreamEvent(done=True)]])
    agent = _make_agent(provider, new_default_registry())
    conv = Conversation()
    conv.add_user("给登录功能加单测的方案")

    events = await _collect(agent, conv, mode=Mode.PLAN)

    assert events[-1].done
    assert [d.name for d in provider.tools_seen[0]] == ["read_file", "glob", "grep"]
    # 首轮 reminder 为完整版、带 system-reminder 标签（AC8/F6）
    assert "<system-reminder>" in provider.reminders_seen[0]
    assert "PLAN MODE" in provider.reminders_seen[0]
    # 稳定系统提示与环境段均非空（F2/F3）
    assert provider.systems_seen[0].stable
    assert provider.systems_seen[0].environment
    assert conv.messages()[-1].content == "计划：第一步…"


async def test_normal_mode_full_tools_and_no_reminder():
    """对照：NORMAL 模式注入全部 6 个工具、不注入 reminder。"""
    provider = FakeProvider(scripts=[[StreamEvent(text="好"), StreamEvent(done=True)]])
    agent = _make_agent(provider, new_default_registry())
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
    assert provider.reminders_seen[0] == ""


# ────────── 场景 G：流出错恢复（AC5/F2） ──────────


async def test_stream_error_stops_turn_and_recovers():
    """AC5：流出错 → 停止本轮、历史兜底收尾；程序不退出，下一轮可正常继续。"""
    provider = FakeProvider(
        scripts=[
            [StreamEvent(err=RuntimeError("boom"))],
            [StreamEvent(text="恢复了。"), StreamEvent(done=True)],
        ]
    )
    agent = _make_agent(provider, new_default_registry())
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


# ────────── 场景 H：规划模式按轮次注入 reminder（AC9/F6/F7） ──────────


async def test_plan_mode_reminder_full_then_concise():
    """AC9/F7：首轮完整提醒，间隔轮重复完整，其余轮精简。"""
    provider = FakeProvider(
        scripts=[
            _turn_with_tools(ToolCall(id="x", name="read_file", input='{"path": "x"}')),
            _turn_with_tools(ToolCall(id="x", name="glob", input='{"pattern": "*.py"}')),
            [StreamEvent(text="计划完成。"), StreamEvent(done=True)],
        ]
    )
    agent = _make_agent(provider, new_default_registry())
    conv = Conversation()
    conv.add_user("调研项目结构")

    events = await _collect(agent, conv, mode=Mode.PLAN)

    assert events[-1].done
    # 三轮均注入 reminder，且带标签（AC8）
    assert [r.count("<system-reminder>") for r in provider.reminders_seen] == [1, 1, 1]
    # iter1 完整、iter2 精简（len 更小）；iter3 亦精简
    assert len(provider.reminders_seen[1]) < len(provider.reminders_seen[0])
    assert len(provider.reminders_seen[2]) < len(provider.reminders_seen[0])


async def test_stable_system_same_across_modes():
    """F7/N1：普通与规划模式 `req.system.stable` 相同（规划提醒已移出系统通道）。"""
    p1 = FakeProvider(scripts=[[StreamEvent(text="x"), StreamEvent(done=True)]])
    a1 = _make_agent(p1, new_default_registry())
    c1 = Conversation()
    c1.add_user("hi")
    await _collect(a1, c1, mode=Mode.DEFAULT)

    p2 = FakeProvider(scripts=[[StreamEvent(text="x"), StreamEvent(done=True)]])
    a2 = _make_agent(p2, new_default_registry())
    c2 = Conversation()
    c2.add_user("hi")
    await _collect(a2, c2, mode=Mode.PLAN)

    assert p1.systems_seen[0].stable == p2.systems_seen[0].stable
    # 两模式环境段均非空（同一 cwd/平台，内容一致）
    assert p1.systems_seen[0].environment == p2.systems_seen[0].environment


async def test_reminder_not_in_conversation_history():
    """AC8/N3：reminder 每轮动态构造、不写入持久对话历史。"""
    provider = FakeProvider(scripts=[[StreamEvent(text="计划。"), StreamEvent(done=True)]])
    agent = _make_agent(provider, new_default_registry())
    conv = Conversation()
    conv.add_user("做个计划")

    await _collect(agent, conv, mode=Mode.PLAN)

    assert [m.role for m in conv.messages()] == ["user", "assistant"]
    for m in conv.messages():
        assert "<system-reminder>" not in m.content


async def test_cache_usage_passthrough():
    """AC6/F4：fake 发缓存用量 → `Event.usage` 透传 cache_write/cache_read。"""
    provider = FakeProvider(
        scripts=[
            [
                StreamEvent(text="ok"),
                StreamEvent(
                    usage=LlmUsage(
                        input_tokens=10,
                        output_tokens=5,
                        cache_write=30,
                        cache_read=40,
                    )
                ),
                StreamEvent(done=True),
            ]
        ]
    )
    agent = _make_agent(provider, new_default_registry())
    conv = Conversation()
    conv.add_user("hi")

    events = await _collect(agent, conv)

    usages = [e.usage for e in events if e.usage is not None]
    assert len(usages) == 1
    assert (usages[0].input, usages[0].output) == (10, 5)
    assert usages[0].cache_write == 30
    assert usages[0].cache_read == 40


# ────────── 场景 I：权限集成（ch06：Deny 回灌 / 保序 / 人在回路 / 取消） ──────────


async def test_deny_feedback_loop_continues(tmp_path):
    """AC11/F9：被拒工具调用回灌错误结果，Agent Loop 继续到次轮。"""
    eng = _engine(str(tmp_path))
    provider = FakeProvider(
        scripts=[
            _turn_with_tools(ToolCall(id="x", name="read_file", input='{"path": "/etc/passwd"}')),
            [StreamEvent(text="换个项目内路径"), StreamEvent(done=True)],
        ]
    )
    agent = Agent(
        provider, new_default_registry(), eng, runtime=new_runtime(workspace=str(tmp_path))
    )
    conv = Conversation()
    conv.add_user("读一个文件")

    events = await _collect(agent, conv)

    tool_evs = [e.tool for e in events if e.tool is not None]
    assert tool_evs[1].is_error
    assert "路径在项目目录之外" in tool_evs[1].result
    assert events[-1].done
    assert conv.messages()[-1].content == "换个项目内路径"


async def test_mixed_batch_deny_and_allow_order(tmp_path):
    """AC11/F9/N3：单批含「被拒 + 放行」按调用序、各自调用 ID 配对回灌，不串位。"""
    eng = _engine(str(tmp_path))
    provider = FakeProvider(
        scripts=[
            _turn_with_tools(
                ToolCall(id="x", name="read_file", input='{"path": "/etc/passwd"}'),
                ToolCall(id="x", name="read_file", input='{"path": "pyproject.toml"}'),
            ),
            [StreamEvent(text="完成"), StreamEvent(done=True)],
        ]
    )
    agent = Agent(
        provider, new_default_registry(), eng, runtime=new_runtime(workspace=str(tmp_path))
    )
    conv = Conversation()
    conv.add_user("混合批")

    events = await _collect(agent, conv)

    results = conv.messages()[2].tool_results
    assert results[0].is_error and "路径在项目目录之外" in results[0].content
    assert not results[1].is_error
    assert [r.tool_call_id for r in results] == [c.id for c in conv.messages()[1].tool_calls]
    assert events[-1].done


async def test_ask_approval_allow_once(tmp_path):
    """AC10/F8：default 下写文件触发 ApprovalRequest，选「允许本次」→ 执行。"""
    eng = _engine(str(tmp_path))
    target = tmp_path / "out.txt"
    provider = FakeProvider(
        scripts=[
            _turn_with_tools(
                ToolCall(
                    id="x",
                    name="write_file",
                    input=json.dumps({"path": str(target), "content": "hi"}),
                )
            ),
            [StreamEvent(text="写好了"), StreamEvent(done=True)],
        ]
    )
    agent = Agent(
        provider, new_default_registry(), eng, runtime=new_runtime(workspace=str(tmp_path))
    )
    conv = Conversation()
    conv.add_user("写文件")

    approvals = []
    events = []
    async for ev in agent.run(conv, Mode.DEFAULT, asyncio.Event()):
        if ev.approval is not None:
            approvals.append(ev.approval)
            ev.approval.respond.set_result(Outcome.ALLOW_ONCE)
        else:
            events.append(ev)

    assert len(approvals) == 1
    assert approvals[0].name == "write_file"
    assert "需确认" in approvals[0].reason
    assert target.read_text(encoding="utf-8") == "hi"
    assert events[-1].done


async def test_ask_approval_deny_once(tmp_path):
    """AC10/F8：选「拒绝本次」→ 回灌被拒、不执行。"""
    eng = _engine(str(tmp_path))
    target = tmp_path / "out.txt"
    provider = FakeProvider(
        scripts=[
            _turn_with_tools(
                ToolCall(
                    id="x",
                    name="write_file",
                    input=json.dumps({"path": str(target), "content": "hi"}),
                )
            ),
            [StreamEvent(text="换个做法"), StreamEvent(done=True)],
        ]
    )
    agent = Agent(
        provider, new_default_registry(), eng, runtime=new_runtime(workspace=str(tmp_path))
    )
    conv = Conversation()
    conv.add_user("写文件")

    async for ev in agent.run(conv, Mode.DEFAULT, asyncio.Event()):
        if ev.approval is not None:
            ev.approval.respond.set_result(Outcome.DENY_ONCE)

    assert not target.exists()  # 未执行
    # 被拒结果回灌：tool 回合 is_error
    results = conv.messages()[2].tool_results
    assert results[0].is_error
    assert "需确认" in results[0].content


async def test_ask_approval_allow_forever(tmp_path):
    """AC10/F8：选「永久」→ 执行并写本地层精确 allow 规则。"""
    from pathlib import Path

    eng = _engine(str(tmp_path))
    provider = FakeProvider(
        scripts=[
            _turn_with_tools(ToolCall(id="x", name="bash", input='{"command": "echo ok"}')),
            [StreamEvent(text="完成"), StreamEvent(done=True)],
        ]
    )
    agent = Agent(
        provider, new_default_registry(), eng, runtime=new_runtime(workspace=str(tmp_path))
    )
    conv = Conversation()
    conv.add_user("跑命令")

    async for ev in agent.run(conv, Mode.DEFAULT, asyncio.Event()):
        if ev.approval is not None:
            ev.approval.respond.set_result(Outcome.ALLOW_FOREVER)

    assert "Bash(echo ok)" in Path(eng.local_path).read_text(encoding="utf-8")


async def test_read_only_batch_no_approval(tmp_path):
    """AC13/N3：一批只读不产生 ApprovalRequest，且并发不退化。"""
    eng = _engine(str(tmp_path))
    reg, state = _probe_registry()
    provider = FakeProvider(
        scripts=[
            _turn_with_tools(
                ToolCall(id="x", name="probe_ro", input='{"tag": "a"}'),
                ToolCall(id="x", name="probe_ro", input='{"tag": "b"}'),
            ),
            [StreamEvent(text="完成"), StreamEvent(done=True)],
        ]
    )
    agent = Agent(provider, reg, eng, runtime=new_runtime(workspace=str(tmp_path)))
    conv = Conversation()
    conv.add_user("并发只读")

    approvals = []
    async for ev in agent.run(conv, Mode.DEFAULT, asyncio.Event()):
        if ev.approval is not None:
            approvals.append(ev.approval)

    assert not approvals
    assert state["peak"] >= 2  # 只读仍并发执行


async def test_cancel_during_approval(tmp_path):
    """AC12/N4：人在回路等待中 task.cancel() → 干净收尾、历史合法、无挂起 task。"""
    eng = _engine(str(tmp_path))
    provider = FakeProvider(
        scripts=[
            _turn_with_tools(
                ToolCall(
                    id="x", name="write_file", input=json.dumps({"path": "x.txt", "content": "y"})
                )
            )
        ]
    )
    agent = Agent(
        provider, new_default_registry(), eng, runtime=new_runtime(workspace=str(tmp_path))
    )
    conv = Conversation()
    conv.add_user("写文件")

    saw_approval = asyncio.Event()

    async def drive() -> None:
        async for ev in agent.run(conv, Mode.DEFAULT, asyncio.Event()):
            if ev.approval is not None:
                saw_approval.set()
                continue  # 下一次 __anext__ 让 agent 卡在 await respond

    task = asyncio.create_task(drive())
    await asyncio.wait_for(saw_approval.wait(), timeout=2)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    # 历史合法：assistant(tool_use) → tool(已取消) → assistant(尾巴)
    msgs = conv.messages()
    assert msgs[-1].role == "assistant"
    assert msgs[-1].content == NOTICE_CANCELLED
    assert msgs[-2].role == "tool"


# ────────── 上下文管理集成（ch08） ──────────


class AnchorSpy(FakeProvider):
    """在每次发起请求前记录 Agent 当前的用量锚点，用于验证「替换而非累加」。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.agent: Agent | None = None
        self.anchors: list[int] = []

    async def stream(self, req: Request) -> AsyncIterator[StreamEvent]:
        if self.agent is not None:
            self.anchors.append(self.agent.runtime.usage_anchor)
        async for ev in super().stream(req):
            yield ev


class RawToolsProvider(FakeProvider):
    """按引用记录每次请求的 tools 列表（不做拷贝），用于比对对象同一性。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.raw_tools: list[list[ToolDefinition]] = []

    async def stream(self, req: Request) -> AsyncIterator[StreamEvent]:
        self.raw_tools.append(req.tools)
        async for ev in super().stream(req):
            yield ev


def _compact_phases(events: list[Event]) -> list[str]:
    """把事件流里的压缩事件折算成 phase 值序列。"""
    return [e.compact.phase.value for e in events if e.compact is not None]


def _usage(inp: int, out: int) -> StreamEvent:
    """构造一条只带用量的流事件。"""
    return StreamEvent(usage=LlmUsage(input_tokens=inp, output_tokens=out))


async def test_agent_uses_latest_usage_as_anchor(tmp_path):
    """I4/I4-bis：每轮 stream 结束后锚点被最新 usage 替换，而不是累加。"""
    provider = AnchorSpy(
        scripts=[
            [
                StreamEvent(text="先读。"),
                _usage(900, 100),
                _tool_call("read_file", '{"path": "a.txt"}'),
            ],
            [StreamEvent(text="读完了。"), _usage(1400, 100)],
        ]
    )
    agent = _make_agent(provider, new_default_registry(), root=str(tmp_path))
    provider.agent = agent
    conv = Conversation()
    conv.add_user("读一下 a.txt")

    await _collect(agent, conv)

    assert provider.anchors == [0, 1000]  # 第 2 次请求前的锚点是第 1 轮真实用量
    assert agent.runtime.usage_anchor == 1500  # 替换为最新一轮（不是 1000 + 1500）
    # 锚点条数记录的是用量产生当时的会话长度（终稿 assistant 尚未入历史）
    assert agent.runtime.anchor_msg_len == 3
    assert conv.length() == 4


async def test_agent_summary_request_keeps_anchor(tmp_path):
    """I4-bis：摘要请求自身的 usage 不写回锚点，锚点只由主对话路径维护。"""
    provider = FakeProvider(
        scripts=[
            [
                StreamEvent(text="先读。"),
                _usage(900, 100),
                _tool_call("read_file", '{"path": "a.txt"}'),
            ],
            [StreamEvent(text="<summary>历史摘要</summary>"), _usage(9000, 9000)],  # 摘要请求
            [StreamEvent(text="完成了。"), _usage(1500, 500)],  # 压缩后的主对话
        ]
    )
    agent = _make_agent(provider, new_default_registry(), root=str(tmp_path))
    agent.runtime.context_window = 34000  # 阈值 1000：第 2 轮必然触发自动压缩
    conv = Conversation()
    conv.add_user("读一下 a.txt")

    await _collect(agent, conv)

    assert provider.calls == 3
    assert agent.runtime.usage_anchor == 2000  # 摘要请求的 18000 没有写回


async def test_agent_tool_defs_identity_shared_with_stream(tmp_path, monkeypatch):
    """I3：同一轮迭代里 manage_context 与 stream 请求共用同一份工具定义对象。"""
    import potatocode.agent as agent_mod

    seen: list[list[ToolDefinition]] = []
    real_manage = agent_mod.manage_context

    async def spy(in_):
        seen.append(in_.tool_defs)
        return await real_manage(in_)

    provider = RawToolsProvider(scripts=[[StreamEvent(text="好的。")]])
    agent = _make_agent(provider, new_default_registry(), root=str(tmp_path))
    conv = Conversation()
    conv.add_user("你好")
    monkeypatch.setattr(agent_mod, "manage_context", spy)

    await _collect(agent, conv)

    assert len(seen) == 1
    assert seen[0] is provider.raw_tools[0]  # 同一列表引用，而非内容相等


async def test_agent_plan_mode_shares_read_only_defs(tmp_path, monkeypatch):
    """I3：Plan 模式下恢复段与请求都用只读工具集，且仍是同一引用。"""
    import potatocode.agent as agent_mod

    seen: list[list[ToolDefinition]] = []
    real_manage = agent_mod.manage_context

    async def spy(in_):
        seen.append(in_.tool_defs)
        return await real_manage(in_)

    provider = RawToolsProvider(scripts=[[StreamEvent(text="计划如下。")]])
    registry = new_default_registry()
    agent = _make_agent(provider, registry, root=str(tmp_path))
    conv = Conversation()
    conv.add_user("规划一下")
    monkeypatch.setattr(agent_mod, "manage_context", spy)

    await _collect(agent, conv, mode=Mode.PLAN)

    names = {d.name for d in seen[0]}
    assert names == {d.name for d in registry.read_only_definitions()}
    assert seen[0] is provider.raw_tools[0]


async def test_agent_tracks_read_file(tmp_path):
    """I5：read_file 成功后写入纯净字节的文件追踪（与磁盘原文逐字节相等）。"""
    target = tmp_path / "note.txt"
    target.write_text("第一行\n第二行\n", encoding="utf-8")
    provider = FakeProvider(
        scripts=[
            _turn_with_tools(
                ToolCall(id="r1", name="read_file", input=json.dumps({"path": str(target)}))
            ),
            [StreamEvent(text="读完了。")],
        ]
    )
    agent = _make_agent(provider, new_default_registry(), root=str(tmp_path))
    conv = Conversation()
    conv.add_user("读 note.txt")

    await _collect(agent, conv)

    snapshot = agent.runtime.recovery.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0].path == str(target.resolve())
    # 与磁盘原文逐字节相等（不带 read_file 的行号前缀；换行跟随平台写入结果）
    assert snapshot[0].content == target.read_bytes().decode("utf-8")


async def test_agent_only_tracks_read_file(tmp_path):
    """I5：非 read_file 工具不写入文件追踪。"""
    provider = FakeProvider(
        scripts=[
            _turn_with_tools(ToolCall(id="b1", name="bash", input='{"cmd": "echo hi"}')),
            [StreamEvent(text="好了。")],
        ]
    )
    agent = _make_agent(provider, new_default_registry(), root=str(tmp_path))
    conv = Conversation()
    conv.add_user("跑个命令")

    await _collect(agent, conv)

    assert agent.runtime.recovery.snapshot() == []


async def test_agent_emits_auto_compact_events(tmp_path):
    """I13：越过阈值时 yield BEFORE_AUTO / AFTER_AUTO 一对事件。"""
    summary = "<summary>## 1 主要请求和意图\n用户要求测试压缩。\n## 2 关键技术概念\n摘要</summary>"
    provider = FakeProvider(
        scripts=[
            [StreamEvent(text=summary)],
            [StreamEvent(text="压缩后继续。"), _usage(100, 20)],
        ]
    )
    agent = _make_agent(provider, new_default_registry(), root=str(tmp_path))
    agent.runtime.context_window = 35000  # 阈值 2000
    conv = Conversation()
    # 对话总量远超"近期原文"下界（10000 token），压缩后才会真正变小
    for _ in range(12):
        conv.add_user("x" * 6000)
        conv.add_assistant("y" * 6000)

    events = await _collect(agent, conv)

    assert _compact_phases(events) == ["before_auto", "after_auto"]
    after = [e.compact for e in events if e.compact is not None][1]
    assert after.err is None
    assert after.before > after.after
    # 两个事件之间不得出现正文事件（说明压缩发生在主对话开始之前）
    first_text = next(i for i, e in enumerate(events) if e.text)
    last_compact = max(i for i, e in enumerate(events) if e.compact is not None)
    assert last_compact < first_text


async def test_agent_no_compact_event_below_threshold(tmp_path):
    """I13：估算远低于阈值时不发任何压缩事件。"""
    provider = FakeProvider(
        scripts=[
            _turn_with_tools(ToolCall(id="r1", name="read_file", input='{"path": "a.txt"}')),
            [StreamEvent(text="好了。")],
        ]
    )
    agent = _make_agent(provider, new_default_registry(), root=str(tmp_path))
    conv = Conversation()
    conv.add_user("读 a.txt")

    events = await _collect(agent, conv)

    assert [e for e in events if e.compact is not None] == []


async def test_agent_emergency_compact_succeeds(tmp_path):
    """I6/E6：撞墙 → 紧急压缩 → 用新消息重试一次并成功。"""
    ptl = PromptTooLongError("prompt is too long")
    provider = FakeProvider(
        scripts=[
            [StreamEvent(err=ptl)],
            [StreamEvent(text="<summary>历史摘要</summary>")],  # 紧急压缩的摘要请求
            [StreamEvent(text="重试成功。"), _usage(100, 20)],  # 重试本轮主对话
        ]
    )
    agent = _make_agent(provider, new_default_registry(), root=str(tmp_path))
    conv = Conversation()
    conv.add_user("你好")

    events = await _collect(agent, conv)

    assert provider.calls == 3  # 主对话 + 摘要 + 重试，不再多
    assert _compact_phases(events) == ["before_emergency", "after_emergency"]
    assert not [e for e in events if e.err is not None]
    assert any(e.done for e in events)
    assert agent.runtime.usage_anchor == 120  # 紧急压缩清零后由重试请求重新锚定
    assert agent.runtime.anchor_msg_len == 3  # 重试请求发出时的会话长度


async def test_agent_emergency_compact_re_raise_on_second_ptl(tmp_path):
    """I6/E6：紧急压缩后的重试仍撞墙 → 上抛错误，不再做第三次。"""
    ptl = PromptTooLongError("prompt is too long")
    provider = FakeProvider(
        scripts=[
            [StreamEvent(err=ptl)],
            [StreamEvent(text="<summary>历史摘要</summary>")],
            [StreamEvent(err=ptl)],
        ]
    )
    agent = _make_agent(provider, new_default_registry(), root=str(tmp_path))
    conv = Conversation()
    conv.add_user("你好")

    events = await _collect(agent, conv)

    assert provider.calls == 3
    assert events[-1].err is ptl


async def test_agent_emergency_unrecoverable_when_still_too_big(tmp_path):
    """E6：压缩后重新估算仍 ≥ 窗口 − 手动余量 → 不重试，直接上抛。"""
    ptl = PromptTooLongError("prompt is too long")
    provider = FakeProvider(
        scripts=[
            [StreamEvent(err=ptl)],
            [StreamEvent(text="<summary>历史摘要</summary>")],
        ]
    )
    agent = _make_agent(provider, new_default_registry(), root=str(tmp_path))
    agent.runtime.context_window = 34000  # 手动余量 3000：需降到 31000 以下才重试
    for _ in range(3):  # 让自动路径熔断，脚本留给紧急路径使用
        agent.runtime.auto_tracking.record_failure()
    conv = Conversation()
    # 尾部 5 条原文本身就超过 31000 token：压缩后仍会留在"近期原文"里
    for _ in range(5):
        conv.add_user("x" * 35000)
        conv.add_assistant("y" * 35000)

    events = await _collect(agent, conv)

    assert provider.calls == 2  # 主对话 + 摘要请求，没有第二次主对话请求
    assert events[-1].err is ptl


async def test_agent_emergency_offloads_big_tool_results(tmp_path):
    """E6：紧急压缩先强制跑一次第 1 层，大工具结果被落盘。"""
    ptl = PromptTooLongError("prompt is too long")
    provider = FakeProvider(
        scripts=[
            [StreamEvent(err=ptl)],
            [StreamEvent(text="<summary>历史摘要</summary>")],
            [StreamEvent(text="重试成功。")],
        ]
    )
    agent = _make_agent(provider, new_default_registry(), root=str(tmp_path))
    conv = Conversation()
    conv.add_user("读大文件")
    conv.add_assistant_with_tool_calls("", [ToolCall(id="big", name="read_file", input="{}")])
    conv.add_tool_results([ToolResult(tool_call_id="big", content="x" * 60000)])

    await _collect(agent, conv)

    spilled = list(Path(agent.runtime.session.spill_dir).iterdir())
    assert [p.name for p in spilled] == ["big"]


async def test_agent_run_force_compact(tmp_path):
    """C15：run_force_compact 返回压缩前后估算 token，并替换会话历史。"""
    provider = FakeProvider(scripts=[[StreamEvent(text="<summary>历史摘要</summary>")]])
    agent = _make_agent(provider, new_default_registry(), root=str(tmp_path))
    conv = Conversation()
    conv.add_user("你好")
    expected_before = estimate_tokens(0, conv.messages(), 0)

    before, after = await agent.run_force_compact(conv)

    assert provider.calls == 1  # 只发了一次摘要请求，没走普通对话
    assert before == expected_before
    assert after >= 0
    assert "## 历史会话摘要" in conv.messages()[0].content


async def test_agent_run_and_force_compact_are_serialized(tmp_path):
    """I12：run 进行中时 run_force_compact 必须等待，两者不并发触发压缩。"""
    started = asyncio.Event()
    release = asyncio.Event()
    summary_requests: list[Request] = []

    class GatedProvider(FakeProvider):
        """主对话请求阻塞到 release 被置位；摘要请求（无工具）直接返回。"""

        async def stream(self, req: Request) -> AsyncIterator[StreamEvent]:
            if not req.tools:
                summary_requests.append(req)
                async for ev in super().stream(req):
                    yield ev
                return
            started.set()
            await release.wait()
            async for ev in super().stream(req):
                yield ev

    provider = GatedProvider(
        scripts=[
            [StreamEvent(text="<summary>历史摘要</summary>")],  # 手动压缩的摘要请求
            [StreamEvent(text="主对话完成。")],  # 主对话（被阻塞）
        ]
    )
    agent = _make_agent(provider, new_default_registry(), root=str(tmp_path))
    conv = Conversation()
    conv.add_user("你好")

    run_task = asyncio.create_task(_collect(agent, conv))
    await started.wait()
    compact_task = asyncio.create_task(agent.run_force_compact(conv))
    await asyncio.sleep(0.05)

    assert not compact_task.done()  # 手动压缩在等 run 让出会话锁
    assert summary_requests == []  # 期间没有任何摘要请求发出

    release.set()
    _, after = await compact_task
    await run_task

    assert len(summary_requests) == 1  # 压缩在 run 结束后才执行
    assert after >= 0
