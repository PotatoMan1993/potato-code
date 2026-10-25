"""Agent 构造选项单测（ch15 T10）：6 个新关键字参数接受且生效。"""

from __future__ import annotations

import pytest

from potatocode.agent import Agent, MaxTurnsReached
from potatocode.conversation import Conversation
from potatocode.permission import Mode
from potatocode.tool import Registry

from fakes import FakeProvider, FakeTool, make_agent, make_engine, text_turn, tool_turn


def test_defaults_are_neutral() -> None:
    """默认值语义：全部「未设置」，主 Agent 行为不变（F10）。"""
    provider = FakeProvider([text_turn("ok")])
    agent = make_agent(provider, Registry())
    assert agent.system_prompt is None
    assert agent.max_turns == 0
    assert agent.permission_mode is None
    assert agent.dont_ask is False
    assert agent.approval_upgrader is None
    assert agent.provider is provider  # 未覆盖时与构造传入一致


def test_provider_override_replaces_provider() -> None:
    """provider_override 非空：Agent 的 provider 被替换（F10）。"""
    provider = FakeProvider([text_turn("main")])
    override = FakeProvider([text_turn("override")])
    agent = make_agent(provider, Registry(), provider_override=override)
    assert agent.provider is override


def test_readonly_accessors() -> None:
    """provider / registry / version / hook_engine 只读访问器（Agent 工具构造子 Agent 用）。"""
    provider = FakeProvider([text_turn("ok")])
    registry = Registry()
    agent = make_agent(provider, registry)
    assert agent.registry is registry
    assert agent.version == "test-ch15"
    assert agent.hook_engine is None


def _reg(*tools: FakeTool) -> Registry:
    registry = Registry()
    for tool in tools:
        registry.register(tool)
    return registry


async def test_max_turns_takes_effect() -> None:
    """max_turns=2：触达即抛 MaxTurnsReached（F9/F10）。"""
    provider = FakeProvider(scripts=[], repeat=tool_turn(("echo", "{}")))
    echo = FakeTool("echo", read_only=True)
    agent = make_agent(provider, _reg(echo), max_turns=2)
    with pytest.raises(MaxTurnsReached):
        await agent.run_to_completion(Conversation(), "任务")


def test_permission_mode_saved() -> None:
    """permission_mode 保存为 Mode 值，None 表示未设置（F10）。"""
    provider = FakeProvider([text_turn("ok")])
    agent = make_agent(provider, Registry(), permission_mode=Mode.PLAN)
    assert agent.permission_mode is Mode.PLAN
    agent_default = make_agent(FakeProvider([text_turn("ok")]), Registry())
    assert agent_default.permission_mode is None


def test_engine_is_independent_object(tmp_path) -> None:
    """engine 形参接受注入的权限引擎（子 Agent 与父共享或独立均可，F10 预留）。"""
    from potatocode.agent.runtime import new_runtime

    provider = FakeProvider([text_turn("ok")])
    engine = make_engine(str(tmp_path))
    agent = Agent(provider, Registry(), engine, "test", runtime=new_runtime(workspace=str(tmp_path)))
    assert agent.engine is engine
