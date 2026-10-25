"""agent 目录测试共享桩：fake provider、fake 工具与 Agent 构造辅助（ch15）。"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from typing import Any

from potatocode.agent import Agent, new_runtime
from potatocode.llm import Request, StreamEvent, System, ToolCall, ToolDefinition
from potatocode.permission import Outcome, new_engine
from potatocode.permission.engine import Engine
from potatocode.tool import Registry, Result


class FakeProvider:
    """按请求次数逐轮返回脚本事件的 provider 替身（与 tests/test_agent.py 同构）。

    `scripts` 每轮一个事件列表；`repeat` 在脚本耗尽后每轮恒返该脚本。
    工具调用 id 按轮重打；记录 `tools_seen` / `systems_seen` 供断言。
    主 Agent 与子 Agent 共用同一 provider 实例时，请求按发生顺序共享计数。
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
    """单个工具调用的脚本事件（id 由 FakeProvider 按轮重打）。"""
    return StreamEvent(tool_calls=[ToolCall(id="x", name=name, input=args)])


def text_turn(text: str) -> list[StreamEvent]:
    """「纯文本回合」脚本：自然完成。"""
    return [StreamEvent(text=text), StreamEvent(done=True)]


def tool_turn(*calls: tuple[str, str]) -> list[StreamEvent]:
    """「工具调用回合」脚本：一次上抛全部调用。"""
    return [_tool_call(name, args) for name, args in calls] + [StreamEvent(done=True)]


class FakeTool:
    """最小工具桩：echo 类只读工具，返回固定文本。"""

    def __init__(self, name: str, read_only: bool, content: str = "fake-ok") -> None:
        self._name = name
        self._read_only = read_only
        self._content = content
        self.executed = 0

    def name(self) -> str:
        return self._name

    def description(self) -> str:
        return "fake tool for subagent tests"

    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def read_only(self) -> bool:
        return self._read_only

    async def execute(self, args: str) -> Result:
        self.executed += 1
        return Result(content=self._content)


def make_engine(root: str | None = None) -> Engine:
    """构造根于 root（默认临时目录）的权限引擎。"""
    engine, _ = new_engine(root or os.getcwd())
    return engine


def make_agent(
    provider: FakeProvider,
    registry: Registry,
    root: str | None = None,
    **kwargs: Any,
) -> Agent:
    """构造带权限引擎与会话运行时的 Agent（落盘目录固定在临时目录）。"""
    workspace = root or tempfile.mkdtemp(prefix="potatocode-ch15-")
    kwargs.setdefault("runtime", new_runtime(workspace=workspace))
    return Agent(provider, registry, make_engine(workspace), "test-ch15", **kwargs)


def allow_upgrader(outcome: Outcome = Outcome.ALLOW_ONCE):
    """构造一个总是同意的审批升级回调（记录调用）。"""
    calls: list[Any] = []

    async def upgrade(req):
        calls.append(req)
        return outcome, True

    return upgrade, calls


__all__ = [
    "FakeProvider",
    "FakeTool",
    "allow_upgrader",
    "make_agent",
    "make_engine",
    "text_turn",
    "tool_turn",
]
