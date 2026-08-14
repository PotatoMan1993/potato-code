"""单轮闭环消费与计时：`_consume_agent_events` 消费 agent 事件流驱动渲染。

以 mixin 形式混入 PotatoCodeApp（见 app.py）。mixin 引用的属性在 App 上定义，
此处仅在类型检查期声明，便于静态检查。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from rich.console import Group
from rich.text import Text
from textual.timer import Timer
from textual.widgets import RichLog, Static

from ..agent import Agent, Phase, ToolEvent
from .view import (
    SessionState,
    ToolDisplay,
    assistant_block,
    error_block,
    preamble_block,
    tool_line,
    tool_result_summary,
)

if TYPE_CHECKING:
    from ..conversation import Conversation
    from ..llm import Provider
    from ..tool import Registry

# "处理中"动画帧（N2：等待期间带动画与实时秒数）
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class StreamMixin:
    """一轮单轮闭环的消费与收尾逻辑。"""

    # —— 以下属性由 PotatoCodeApp 提供（仅类型声明，无运行时赋值）——
    state: SessionState
    provider: Provider | None
    conv: Conversation
    cur_reply: str
    turn_start: float
    _tool_registry: Registry
    _cur_tool: ToolDisplay | None
    _timer: Timer | None
    _stream_task: asyncio.Task | None

    async def _consume_agent_events(self) -> None:
        """消费 agent 单轮闭环事件：preamble/最终答复进动态区，工具行与结果摘要进 scrollback。"""
        assert self.provider is not None
        agent = Agent(self.provider, self._tool_registry)
        try:
            async for ev in agent.run(self.conv):
                if ev.err is not None:
                    self._finish_with_error(ev.err)
                    return
                if ev.tool is not None:
                    self._handle_tool_event(ev.tool)
                if ev.text:
                    self.cur_reply += ev.text
                    self._refresh_streaming_view()
                if ev.done:
                    self._finish_turn(self.cur_reply)
                    return
            # 兜底：agent 保证 yield done/err，此处仅为防御
            self._finish_turn(self.cur_reply)
        except asyncio.CancelledError:
            raise  # App 退出时取消本轮
        except Exception as e:
            self._finish_with_error(e)

    def _handle_tool_event(self, te: ToolEvent) -> None:
        """分派工具事件：START 提交已流式的 preamble 并置执行指示；END 写工具行+结果摘要。"""
        log = self.query_one("#log", RichLog)  # type: ignore[attr-defined]
        if te.phase == Phase.START:
            # 先把已流式的 preamble 文本定型提交，再进入工具执行指示
            if self.cur_reply:
                log.write(preamble_block(self.cur_reply))
                self.cur_reply = ""
            self._cur_tool = ToolDisplay(te.name, te.args)
        else:
            log.write(tool_line(te.name, te.args))
            log.write(tool_result_summary(te.result, te.is_error))
            self._cur_tool = None

    def _tick(self) -> None:
        """定时器回调：流式期间每 0.1s 刷新动画与秒数。"""
        if self.state is SessionState.STREAMING:
            self._refresh_streaming_view()

    def _refresh_streaming_view(self) -> None:
        """重绘动态区：流式正文 + 执行中工具指示 + "Imagining… (Ns)" 计时行。"""
        widget = self.query_one("#streaming", Static)  # type: ignore[attr-defined]
        if self.state is not SessionState.STREAMING:
            widget.display = False
            return
        elapsed = time.monotonic() - self.turn_start
        spinner = _SPINNER[int(elapsed * 10) % len(_SPINNER)]
        if self._cur_tool is not None:
            # 工具执行中：显示工具行 + spinner（N2 执行指示）
            waiting = Text(
                f"{spinner} ● {self._cur_tool.name}({self._cur_tool.args}) Running…",
                style="dim italic",
            )
        else:
            waiting = Text(f"{spinner} Imagining… ({int(elapsed)}s)", style="dim italic")
        if self.cur_reply:
            widget.update(Group(Text(f"● {self.cur_reply}"), waiting))
        else:
            widget.update(waiting)
        widget.display = True

    def _finish_turn(self, reply: str) -> None:
        """正常结束：markdown 定型追加到对话区，回到 IDLE。

        会话历史由 agent 维护（已回灌 assistant/tool 回合），此处不重复记账。
        """
        elapsed = time.monotonic() - self.turn_start
        self._stop_turn()
        log = self.query_one("#log", RichLog)  # type: ignore[attr-defined]
        log.write(assistant_block(reply, elapsed))
        self.state = SessionState.IDLE
        self._refocus_input()

    def _finish_with_error(self, err: Exception) -> None:
        """出错：错误块追加到对话区，程序不退出，回到 IDLE。"""
        self._stop_turn()
        log = self.query_one("#log", RichLog)  # type: ignore[attr-defined]
        log.write(error_block(err))
        self.state = SessionState.IDLE
        self._refocus_input()

    def _stop_turn(self) -> None:
        """停止计时器、清理流式 task 引用、清空增量缓冲与执行指示、隐藏动态区。"""
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._stream_task = None
        self.cur_reply = ""
        self._cur_tool = None
        streaming = self.query_one("#streaming", Static)  # type: ignore[attr-defined]
        streaming.display = False
        streaming.update("")

    def _refocus_input(self) -> None:
        """回合结束后把焦点交还输入框。"""
        from .app import ChatTextArea  # 延迟导入避免循环

        self.query_one(ChatTextArea).focus()  # type: ignore[attr-defined]
