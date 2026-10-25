"""Agent 工具单测（ch15 T18）：参数校验、前台/后台路径、嵌套阻断与开关。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from potatocode.agent.agent_tool import STATUS_ASYNC_LAUNCHED, AgentTool
from potatocode.agent.fork import FORK_BOILERPLATE_TAG
from potatocode.agent.subagent_context import SUBAGENT_DEPTH
from potatocode.conversation import Conversation
from potatocode.subagent import load_catalog
from potatocode.task import Manager
from potatocode.tool import Registry

from fakes import FakeProvider, FakeTool, make_agent, text_turn

ECHO_AGENT_MD = """---
name: echo-agent
description: 测试用回声角色
maxTurns: 5
---

你是回声角色。
"""


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _make_tool(
    tmp_path: Path, provider: FakeProvider, bg_enabled: bool = True
) -> tuple[AgentTool, Manager]:
    """构造完整可用的 AgentTool：catalog（含 echo-agent）+ Manager + parent。"""
    project_dir = tmp_path / "agents"
    _write(project_dir / "echo-agent.md", ECHO_AGENT_MD)
    catalog = load_catalog(str(tmp_path), user_dir=tmp_path / "absent", project_dir=project_dir)
    manager = Manager()
    tool = AgentTool(catalog, manager, bg_enabled=bg_enabled)
    registry = Registry()
    registry.register(FakeTool("echo", read_only=True))
    parent = make_agent(provider, registry, root=str(tmp_path))
    tool.set_parent(parent)
    return tool, manager


def _args(**kwargs: object) -> str:
    return json.dumps(kwargs, ensure_ascii=False)


async def test_missing_prompt_returns_error(tmp_path: Path) -> None:
    tool, _ = _make_tool(tmp_path, FakeProvider())
    result = await tool.execute(_args(description="只有描述"))
    assert result.is_error
    assert "prompt" in result.content


async def test_missing_description_returns_error(tmp_path: Path) -> None:
    tool, _ = _make_tool(tmp_path, FakeProvider())
    result = await tool.execute(_args(prompt="任务"))
    assert result.is_error
    assert "description" in result.content


async def test_unknown_subagent_type_returns_error(tmp_path: Path) -> None:
    tool, _ = _make_tool(tmp_path, FakeProvider())
    result = await tool.execute(_args(prompt="任务", description="d", subagent_type="nope"))
    assert result.is_error
    assert "未知 subagent_type" in result.content
    assert "echo-agent" in result.content  # 提示可用角色


async def test_foreground_defined_agent_returns_final_text(tmp_path: Path) -> None:
    """定义式前台：tool_result 即子 Agent 最后一条 assistant 文本（F2/AC2）。"""
    provider = FakeProvider([text_turn("SUB OK")])
    tool, _ = _make_tool(tmp_path, provider)
    result = await tool.execute(_args(prompt="任务", description="d", subagent_type="echo-agent"))
    assert result.is_error is False
    assert result.content == "SUB OK"
    # 子 Agent 请求带角色的 system prompt（F4）
    assert "你是回声角色。" in provider.systems_seen[-1].stable


async def test_run_in_background_returns_async_launched(tmp_path: Path) -> None:
    """显式后台：立即返回 {task_id, status:async_launched}，任务随后完成（F17-①/AC9）。"""
    provider = FakeProvider([text_turn("BG OK")])
    tool, manager = _make_tool(tmp_path, provider)
    result = await tool.execute(
        _args(prompt="任务", description="d", subagent_type="echo-agent", run_in_background=True)
    )
    payload = json.loads(result.content)
    assert payload["status"] == STATUS_ASYNC_LAUNCHED

    done_id = await asyncio.wait_for(manager.subscribe_done().get(), 5)
    assert done_id == payload["task_id"]
    task = manager.get(done_id)
    assert task is not None
    assert task.status.value == 1  # Status.COMPLETED
    assert task.result == "BG OK"


async def test_fork_forced_background_and_conv_setup(tmp_path: Path) -> None:
    """Fork 路径：run_in_background 被强制视为 true；子对话 = 父历史 + Boilerplate 任务（F18/F22）。"""
    provider = FakeProvider(repeat=text_turn("FORK DONE"))
    project_dir = tmp_path / "agents"
    _write(project_dir / "echo-agent.md", ECHO_AGENT_MD)
    catalog = load_catalog(str(tmp_path), user_dir=tmp_path / "absent", project_dir=project_dir)
    manager = Manager()
    tool = AgentTool(catalog, manager, bg_enabled=True)
    registry = Registry()
    registry.register(FakeTool("echo", read_only=True))
    registry.register(tool)  # 生产装配：Agent 工具也在父注册中心里（cli 顺序的测试版）
    parent = make_agent(provider, registry, root=str(tmp_path))
    tool.set_parent(parent)

    parent_conv = Conversation()
    parent_conv.add_user("父对话第一条")
    parent_conv.add_assistant("父对话回复")
    tool.set_parent_conv_getter(lambda: parent_conv)

    result = await tool.execute(_args(prompt="Fork 任务", description="d"))
    payload = json.loads(result.content)
    assert payload["status"] == STATUS_ASYNC_LAUNCHED  # Fork 无条件后台

    await asyncio.wait_for(manager.subscribe_done().get(), 5)
    task = manager.get(payload["task_id"])
    assert task is not None
    msgs = task.conv.messages()
    assert msgs[0].content == "父对话第一条"  # 父历史被克隆（N2）
    # 任务 user 消息 = Boilerplate + 任务文本（跑完后末尾已追加 assistant 回复）
    boilerplate_msgs = [m for m in msgs if m.role == "user" and m.content.startswith(FORK_BOILERPLATE_TAG)]
    assert len(boilerplate_msgs) == 1
    assert boilerplate_msgs[0].content.endswith("Fork 任务")
    assert msgs[-1].role == "assistant"
    # AC5：Fork 子 Agent 工具列表仍保留 Agent 工具（调用被深度标记拦截）
    assert "Agent" in task.sub_agent.registry.names()


async def test_nested_call_blocked_by_depth(tmp_path: Path) -> None:
    """子 Agent 深度 > 0 时再调 Agent 工具：结构化拒绝（F24/AC5 对应机制）。"""
    tool, _ = _make_tool(tmp_path, FakeProvider())
    token = SUBAGENT_DEPTH.set(SUBAGENT_DEPTH.get() + 1)
    try:
        result = await tool.execute(_args(prompt="再嵌套", description="d"))
    finally:
        SUBAGENT_DEPTH.reset(token)
    assert result.is_error
    assert "子 Agent 不能再启动 Agent" in result.content


async def test_background_disabled_blocks_background_and_fork(tmp_path: Path) -> None:
    """N6：enable_subagent_background=false 时后台与 Fork 路径均报错（AC18）。"""
    provider = FakeProvider([text_turn("SYNC OK")])
    tool, _ = _make_tool(tmp_path, provider, bg_enabled=False)

    result = await tool.execute(
        _args(prompt="任务", description="d", subagent_type="echo-agent", run_in_background=True)
    )
    assert result.is_error
    assert "enable_subagent_background" in result.content

    # Fork 强制后台 → 同样被禁
    tool.set_parent_conv_getter(lambda: Conversation())
    result = await tool.execute(_args(prompt="任务", description="d"))
    assert result.is_error

    # 前台定义式子 Agent 不受影响（N6：强制前台同步）
    provider2 = FakeProvider([text_turn("SYNC OK")])
    tool2, _ = _make_tool(tmp_path / "second", provider2, bg_enabled=False)
    result = await tool2.execute(_args(prompt="任务", description="d", subagent_type="echo-agent"))
    assert result.is_error is False
    assert result.content == "SYNC OK"


async def test_tool_metadata(tmp_path: Path) -> None:
    tool, _ = _make_tool(tmp_path, FakeProvider())
    assert tool.name() == "Agent"
    assert tool.read_only() is False
    schema = tool.parameters()
    assert set(schema["required"]) == {"prompt", "description"}
    for field in ("prompt", "description", "subagent_type", "model", "run_in_background", "name"):
        assert field in schema["properties"]
    assert "echo-agent" in tool.description()


async def test_invalid_json_returns_error(tmp_path: Path) -> None:
    tool, _ = _make_tool(tmp_path, FakeProvider())
    result = await tool.execute("not-json")
    assert result.is_error
