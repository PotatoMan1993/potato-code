"""launch_fork 公共启动函数单测（ch15 T31/G10）：Skill fork 复用的 SubAgent 底座。"""

from __future__ import annotations

import pytest

from potatocode.agent.fork import FORK_BOILERPLATE_TAG
from potatocode.agent.launch import ForkLaunchOpts, launch_fork
from potatocode.agent.subagent_context import SUBAGENT_DEPTH
from potatocode.conversation import Conversation
from potatocode.llm import StreamEvent
from potatocode.tool import Registry

from fakes import FakeProvider, FakeTool, make_agent, text_turn


async def test_launch_fork_returns_final_text(tmp_path) -> None:
    """conv 已预装填任务：launch_fork 前台跑完返回最终文本（F5/G10）。"""
    provider = FakeProvider([text_turn("FORK FINAL")])
    registry = Registry()
    registry.register(FakeTool("echo", read_only=True))
    parent_runtime = make_agent(provider, registry, root=str(tmp_path)).runtime

    conv = Conversation()
    conv.add_user(FORK_BOILERPLATE_TAG + "\n做一个任务")
    final = await launch_fork(
        ForkLaunchOpts(
            provider=provider,
            registry=registry,
            engine=make_agent(provider, registry, root=str(tmp_path)).engine,
            version="test",
            conv=conv,
            parent_runtime=parent_runtime,
        )
    )
    assert final == "FORK FINAL"
    assert conv.last_role() == "assistant"


async def test_launch_fork_depth_reset_after_run(tmp_path) -> None:
    """跑动期间深度标记生效、结束后复位（F24）。"""
    provider = FakeProvider([text_turn("ok")])
    registry = Registry()
    parent_runtime = make_agent(provider, registry, root=str(tmp_path)).runtime

    assert SUBAGENT_DEPTH.get() == 0
    depth_seen_during_run: list[int] = []

    class DepthProbe(FakeProvider):
        async def stream(self, req):
            depth_seen_during_run.append(SUBAGENT_DEPTH.get())
            async for ev in super().stream(req):
                yield ev

    probe = DepthProbe([text_turn("ok")])
    conv = Conversation()
    conv.add_user("任务")
    await launch_fork(
        ForkLaunchOpts(
            provider=probe,
            registry=registry,
            engine=make_agent(probe, registry, root=str(tmp_path)).engine,
            version="test",
            conv=conv,
            parent_runtime=parent_runtime,
        )
    )
    assert depth_seen_during_run == [1]  # 跑动期间处于子 Agent 深度
    assert SUBAGENT_DEPTH.get() == 0  # 结束后复位


async def test_launch_fork_error_propagates(tmp_path) -> None:
    """子 Agent 异常透传给调用方（SkillExecutor 有错误文本兜底）。"""

    class BoomProvider(FakeProvider):
        async def stream(self, req):
            self.calls += 1
            yield StreamEvent(err=RuntimeError("fork 崩了"))

    provider = BoomProvider()
    registry = Registry()
    conv = Conversation()
    conv.add_user("任务")
    with pytest.raises(RuntimeError, match="fork 崩了"):
        await launch_fork(
            ForkLaunchOpts(
                provider=provider,
                registry=registry,
                engine=make_agent(provider, registry, root=str(tmp_path)).engine,
                version="test",
                conv=conv,
                parent_runtime=make_agent(provider, registry, root=str(tmp_path)).runtime,
            )
        )
