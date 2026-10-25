"""Agent 工具集成测试（ch15 T32）：主 Agent 全链路调用 Agent 工具（mock provider）。

覆盖：前台定义式调用的 tool_result 回灌、子 Agent 工具过滤生效、后台路径
立即返回与任务完成通知。
"""

from __future__ import annotations

import asyncio
import json

from potatocode.agent import Agent, Mode
from potatocode.agent.agent_tool import AgentTool
from potatocode.agent.runtime import new_runtime
from potatocode.conversation import Conversation
from potatocode.llm import StreamEvent, ToolCall
from potatocode.permission import Outcome, new_engine
from potatocode.subagent import load_catalog
from potatocode.task import Manager
from potatocode.tool import Registry

from fakes import FakeProvider, FakeTool

ECHO_AGENT_MD = """---
name: echo-agent
description: 集成测试用回声角色
maxTurns: 5
---

你是回声角色。
"""


def _write(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _agent_turn(call_name: str, call_input: str) -> list[StreamEvent]:
    """「主 Agent 请求工具」脚本：一次上抛单个调用。"""
    return [StreamEvent(tool_calls=[ToolCall(id="x", name=call_name, input=call_input)]), StreamEvent(done=True)]


def _text_turn(text: str) -> list[StreamEvent]:
    return [StreamEvent(text=text), StreamEvent(done=True)]


def _build_world(tmp_path, scripts: list[list[StreamEvent]], repeat=None):
    """构造主 Agent + 注册了 Agent 工具的 Registry（cli 装配顺序的测试版）。"""
    _write(tmp_path / "agents" / "echo-agent.md", ECHO_AGENT_MD)
    catalog = load_catalog(
        str(tmp_path), user_dir=tmp_path / "absent", project_dir=tmp_path / "agents"
    )
    manager = Manager()
    registry = Registry()
    registry.register(FakeTool("echo", read_only=True))
    agent_tool = AgentTool(catalog, manager, bg_enabled=True)
    registry.register(agent_tool)

    provider = FakeProvider(scripts, repeat=repeat)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    main_agent = Agent(
        provider,
        registry,
        new_engine(str(workspace))[0],
        "test-ch15",
        runtime=new_runtime(workspace=str(workspace)),
    )
    agent_tool.set_parent(main_agent)
    conv = Conversation()
    return main_agent, provider, manager, conv


async def _run_collecting(agent: Agent, conv: Conversation) -> list:
    """跑主 Agent 一轮并收集事件；审批一律 Allow（不关心主循环权限机制）。"""
    events: list = []
    async for ev in agent.run(conv, Mode.DEFAULT, asyncio.Event()):
        if ev.approval is not None:
            ev.approval.respond.set_result(Outcome.ALLOW_ONCE)
            continue
        events.append(ev)
    return events


def _tool_results_text(conv: Conversation) -> str:
    return "\n".join(
        r.content for m in conv.messages() if m.role == "tool" for r in m.tool_results
    )


async def test_foreground_subagent_result_flows_to_main(tmp_path) -> None:
    """主 Agent 调 Agent 工具（定义式前台）：tool_result 是子 Agent 的最终文本（AC2）。"""
    agent_call = json.dumps(
        {"prompt": "找一个数", "description": "数数", "subagent_type": "echo-agent"}
    )
    main_agent, provider, _manager, conv = _build_world(
        tmp_path,
        scripts=[
            _agent_turn("Agent", agent_call),
            _text_turn("子 Agent 结果"),
        ],
        repeat=_text_turn("main done"),
    )
    await _run_collecting(main_agent, conv)
    assert "子 Agent 结果" in _tool_results_text(conv)
    # 子 Agent 的请求（第 2 次）工具列表不含 Agent（ALL_AGENT_DISALLOWED_TOOLS，F26）
    sub_tool_names = [d.name for d in provider.tools_seen[1]]
    assert "Agent" not in sub_tool_names
    assert "echo" in sub_tool_names
    # 主 Agent 请求仍含 Agent 工具（N1：主列表稳定）
    assert "Agent" in [d.name for d in provider.tools_seen[0]]


async def test_background_subagent_launches_and_notifies(tmp_path) -> None:
    """后台路径：主 Agent 立即拿到 async_launched，任务完成后经 done 队列通知（AC9）。"""
    agent_call = json.dumps(
        {
            "prompt": "后台任务",
            "description": "后台",
            "subagent_type": "echo-agent",
            "run_in_background": True,
        }
    )
    main_agent, _provider, manager, conv = _build_world(
        tmp_path,
        scripts=[
            _agent_turn("Agent", agent_call),
            _text_turn("BG RESULT"),  # 第 2 次请求来自后台子 Agent（与主对话共享计数）
        ],
        repeat=_text_turn("main continues"),  # 第 3 次请求：主对话收尾
    )
    await _run_collecting(main_agent, conv)
    assert "async_launched" in _tool_results_text(conv)

    task_id = await asyncio.wait_for(manager.subscribe_done().get(), 5)
    task = manager.get(task_id)
    assert task is not None
    assert task.status.value == 1  # Status.COMPLETED
    assert task.result == "BG RESULT"


async def test_tool_list_stable_across_calls(tmp_path) -> None:
    """主 Agent 的工具定义数量稳定：Agent 工具注册后不因调用而增减（N1/G1）。"""
    _write(tmp_path / "agents" / "echo-agent.md", ECHO_AGENT_MD)
    registry = Registry()
    registry.register(FakeTool("echo", read_only=True))
    catalog = load_catalog(
        str(tmp_path), user_dir=tmp_path / "absent", project_dir=tmp_path / "agents"
    )
    agent_tool = AgentTool(catalog, Manager(), bg_enabled=True)
    registry.register(agent_tool)
    count_before = registry.count()
    agent_tool.set_parent(None)
    # 调用失败（parent 未就绪）也不影响工具列表
    result = await agent_tool.execute(json.dumps({"prompt": "x", "description": "y"}))
    assert result.is_error
    assert registry.count() == count_before
