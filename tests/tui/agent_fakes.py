"""tui 目录测试共享桩：run_to_completion 行为可控的假 Agent（与 tests/task 同构）。"""

from __future__ import annotations

import asyncio

from potatocode.conversation import Conversation


class ScriptAgent:
    """run_to_completion 测试桩：按脚本返回文本 / 抛异常 / 睡眠。"""

    def __init__(
        self,
        scripts: list[str] | None = None,
        raise_on_run: BaseException | None = None,
        sleep_seconds: float = 0.0,
    ) -> None:
        self._scripts = scripts or []
        self._raise = raise_on_run
        self._sleep = sleep_seconds
        self.runs = 0

    def new_conv(self) -> Conversation:
        conv = Conversation()
        conv.add_user("初始任务")
        return conv

    async def run_to_completion(self, conv: Conversation, task: str, events=None) -> str:  # noqa: ANN001
        self.runs += 1
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if self._raise is not None:
            raise self._raise
        if self.runs <= len(self._scripts):
            return self._scripts[self.runs - 1]
        return f"结果{self.runs}"


__all__ = ["ScriptAgent"]
