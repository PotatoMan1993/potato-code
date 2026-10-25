"""run_to_completion 单测（ch15 T16）：跑到底循环、dont_ask、升级回调与事件转发。"""

from __future__ import annotations

import asyncio

import pytest

from potatocode.agent import MaxTurnsReached
from potatocode.conversation import Conversation
from potatocode.permission import Outcome
from potatocode.tool import Registry

from fakes import FakeProvider, FakeTool, allow_upgrader, make_agent, text_turn, tool_turn


def _registry(*tools: FakeTool) -> Registry:
    registry = Registry()
    for tool in tools:
        registry.register(tool)
    return registry


async def test_plain_text_returns_final_text() -> None:
    """模型直接回纯文本：返回最后一条 assistant 文本（F9/AC2 基础）。"""
    provider = FakeProvider([text_turn("子 Agent 完成")])
    agent = make_agent(provider, _registry())
    conv = Conversation()
    final = await agent.run_to_completion(conv, "做一个任务")
    assert final == "子 Agent 完成"
    # 任务作为 user 消息进入子对话（F9）
    assert conv.messages()[0].role == "user"
    assert "做一个任务" in conv.messages()[0].content
    # 末尾为 assistant 回合（历史合法性）
    assert conv.messages()[-1].role == "assistant"


async def test_tool_then_text_executes_tool() -> None:
    """工具调用回合 + 纯文本回合：工具被执行、final_text 正确（F5/G5）。"""
    provider = FakeProvider([tool_turn(("echo", "{}")), text_turn("完成")])
    echo = FakeTool("echo", read_only=True, content="ECHO-OK")
    agent = make_agent(provider, _registry(echo))
    final = await agent.run_to_completion(Conversation(), "调用工具")
    assert echo.executed == 1
    assert final == "完成"


async def test_task_empty_string_skips_add_user() -> None:
    """task 为空串：不追加 user 消息（Fork / Skill fork 预装填场景，T28）。"""
    provider = FakeProvider([text_turn("ok")])
    agent = make_agent(provider, _registry())
    conv = Conversation()
    conv.add_user("预装填的任务")
    await agent.run_to_completion(conv, "")
    assert len(conv.messages()) == 2  # user + assistant，没有空 user


async def test_max_turns_raises_with_final_text() -> None:
    """一直调工具不出文本：触达 max_turns 抛 MaxTurnsReached 并携带 final_text（F9）。"""
    provider = FakeProvider(scripts=[], repeat=tool_turn(("echo", "{}")))
    echo = FakeTool("echo", read_only=True)
    agent = make_agent(provider, _registry(echo), max_turns=3)
    conv = Conversation()
    with pytest.raises(MaxTurnsReached) as exc_info:
        await agent.run_to_completion(conv, "无限调用")
    # final_text 为历史末尾的 assistant 收尾文本（上限提示兜底）
    assert "已达最大迭代轮数" in exc_info.value.final_text
    # 历史合法性：最后一轮 tool_use 已配对结果，且有 assistant 文本尾巴
    assert conv.last_role() == "assistant"


async def test_dont_ask_auto_allows_ask_tools(tmp_path) -> None:
    """dontAsk 角色：写文件类 Ask 工具被自动放行，无审批事件（F12-②/AC7）。"""
    import json

    from potatocode.agent.runtime import new_runtime

    target = (tmp_path / "w.txt").as_posix()
    provider = FakeProvider(
        [tool_turn(("write_file", json.dumps({"path": target, "content": "x"}))), text_turn("done")]
    )
    writer = FakeTool("write_file", read_only=False)
    agent = make_agent(
        provider,
        _registry(writer),
        root=str(tmp_path),
        dont_ask=True,
        runtime=new_runtime(workspace=str(tmp_path)),
    )
    events: asyncio.Queue = asyncio.Queue()
    final = await agent.run_to_completion(Conversation(), "写文件", events)
    assert writer.executed == 1
    assert final == "done"
    # 无审批事件被转发（Ask 已被 dontAsk 短路）
    while not events.empty():
        assert events.get_nowait().approval is None


async def test_approval_upgrader_called_and_outcome_applied() -> None:
    """设了升级回调：Ask 时回调被命中，ALLOW_ONCE 放行工具（F12-③）。"""
    provider = FakeProvider([tool_turn(("write_thing", "{}")), text_turn("done")])
    writer = FakeTool("write_thing", read_only=False, content="WROTE")
    upgrader, calls = allow_upgrader(Outcome.ALLOW_ONCE)
    agent = make_agent(provider, _registry(writer), approval_upgrader=upgrader)
    final = await agent.run_to_completion(Conversation(), "执行")
    assert len(calls) == 1
    assert writer.executed == 1
    assert final == "done"


async def test_approval_upgrader_deny_produces_error_result() -> None:
    """升级回调拒绝：工具不执行，拒绝原因作为错误结果回灌（F12-③）。"""
    provider = FakeProvider([tool_turn(("write_thing", "{}")), text_turn("改道完成")])
    writer = FakeTool("write_thing", read_only=False)
    upgrader, calls = allow_upgrader(Outcome.DENY_ONCE)
    agent = make_agent(provider, _registry(writer), approval_upgrader=upgrader)
    final = await agent.run_to_completion(Conversation(), "执行")
    assert len(calls) == 1
    assert writer.executed == 0
    assert final == "改道完成"


async def test_upgrader_not_handled_falls_back_to_auto_deny() -> None:
    """升级失败（ok=False）：回退默认 emit 路径 → run_to_completion 自动拒绝（N3）。"""

    async def failing_upgrader(req):
        return Outcome.DENY_ONCE, False  # ok=False：无消费者

    provider = FakeProvider([tool_turn(("write_thing", "{}")), text_turn("fallback")])
    writer = FakeTool("write_thing", read_only=False)
    agent = make_agent(provider, _registry(writer), approval_upgrader=failing_upgrader)
    final = await agent.run_to_completion(Conversation(), "执行")
    assert writer.executed == 0
    assert final == "fallback"


async def test_events_forwarded_to_queue() -> None:
    """events 队列转发：text / tool 事件可被外部消费（F16/T16-⑥）。"""
    provider = FakeProvider([tool_turn(("echo", "{}")), text_turn("最终回复")])
    echo = FakeTool("echo", read_only=True, content="ECHO")
    agent = make_agent(provider, _registry(echo))
    events: asyncio.Queue = asyncio.Queue()
    await agent.run_to_completion(Conversation(), "跑", events)
    forwarded: list = []
    while not events.empty():
        forwarded.append(events.get_nowait())
    # 含文本增量与工具 START/END 事件
    assert any(ev.text for ev in forwarded)
    assert any(ev.tool is not None and ev.tool.name == "echo" for ev in forwarded)


async def test_permission_mode_overrides_loop_mode(tmp_path) -> None:
    """permission_mode=PLAN：子 Agent 只看到只读工具定义（F10）。"""
    from potatocode.agent.runtime import new_runtime
    from potatocode.permission import Mode

    provider = FakeProvider([text_turn("ok")])
    echo = FakeTool("echo", read_only=True)
    writer = FakeTool("write_file", read_only=False)
    agent = make_agent(
        provider,
        _registry(echo, writer),
        root=str(tmp_path),
        permission_mode=Mode.PLAN,
        runtime=new_runtime(workspace=str(tmp_path)),
    )
    await agent.run_to_completion(Conversation(), "计划")
    seen_names = [d.name for d in provider.tools_seen[-1]]
    assert "echo" in seen_names
    assert "write_file" not in seen_names


async def test_system_prompt_override_reaches_provider() -> None:
    """system_prompt 非空：稳定系统提示被整体替换（F10）。"""
    provider = FakeProvider([text_turn("ok")])
    agent = make_agent(provider, _registry(), system_prompt="你是专属角色。")
    await agent.run_to_completion(Conversation(), "hi")
    assert provider.systems_seen[-1].stable == "你是专属角色。"


async def test_stream_error_raises_to_caller() -> None:
    """流出错：异常上抛给调用方（由 Manager 包成 failed，N3）。"""
    from potatocode.llm import StreamEvent

    class BoomProvider(FakeProvider):
        async def stream(self, req):
            self.calls += 1
            yield StreamEvent(err=RuntimeError("provider 崩了"))

    provider = BoomProvider()
    agent = make_agent(provider, _registry())
    with pytest.raises(RuntimeError, match="provider 崩了"):
        await agent.run_to_completion(Conversation(), "任务")


async def test_cancelled_closes_history_legally() -> None:
    """task.cancel()：CancelledError 透传且历史补 assistant 尾巴（F6）。"""
    provider = FakeProvider(scripts=[], repeat=tool_turn(("echo", "{}")))
    echo = FakeTool("echo", read_only=True)
    agent = make_agent(provider, _registry(echo), max_turns=10)
    conv = Conversation()
    run_task = asyncio.create_task(agent.run_to_completion(conv, "长任务"))
    await asyncio.sleep(0.05)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert conv.last_role() == "assistant"
