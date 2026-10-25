"""task 目录测试共享桩：run_to_completion 行为可控的假 Agent（ch15）。"""

from __future__ import annotations

import asyncio

from potatocode.agent import Event, Phase, ToolEvent, Usage
from potatocode.conversation import Conversation


class ScriptAgent:
    """run_to_completion 测试桩：按脚本返回文本 / 抛异常 / 睡眠。

    `scripts` 按调用次数逐次返回；耗尽后返回 "结果N"。`raise_on_run` 每次调用
    都抛出该异常；`sleep_seconds > 0` 时先睡眠（模拟长任务，供 stop/Busy 测试）。
    仅实现 Manager / AgentTool 依赖的方法（run_to_completion），鸭子类型即可。
    """

    def __init__(
        self,
        scripts: list[str] | None = None,
        raise_on_run: BaseException | None = None,
        sleep_seconds: float = 0.0,
        emit_events: bool = False,
    ) -> None:
        self._scripts = scripts or []
        self._raise = raise_on_run
        self._sleep = sleep_seconds
        self._emit_events = emit_events
        self.runs = 0

    def new_conv(self) -> Conversation:
        """造一个带初始任务的子对话。"""
        conv = Conversation()
        conv.add_user("初始任务")
        return conv

    async def run_to_completion(self, conv: Conversation, task: str, events=None) -> str:  # noqa: ANN001
        self.runs += 1
        if self._emit_events and events is not None:
            events.put_nowait(Event(tool=ToolEvent(name="bash", phase=Phase.START)))
            events.put_nowait(Event(tool=ToolEvent(name="grep", phase=Phase.START)))
            events.put_nowait(Event(usage=Usage(input=100, output=10, cache_read=5)))
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if self._raise is not None:
            raise self._raise
        if self.runs <= len(self._scripts):
            return self._scripts[self.runs - 1]
        return f"结果{self.runs}"


__all__ = ["ScriptAgent"]
