"""流式消费与计时：`_consume_stream` 逐事件驱动动态区，`_tick` 刷新秒数。

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

from .view import SessionState, assistant_block, error_block

if TYPE_CHECKING:
    from ..conversation import Conversation
    from ..llm import Provider

# "处理中"动画帧（N2：等待期间带动画与实时秒数）
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class StreamMixin:
    """一轮流式对话的消费与收尾逻辑。"""

    # —— 以下属性由 PotatoCodeApp 提供（仅类型声明，无运行时赋值）——
    state: SessionState
    provider: Provider | None
    conv: Conversation
    cur_reply: str
    turn_start: float
    _timer: Timer | None
    _stream_task: asyncio.Task | None

    async def _consume_stream(self) -> None:
        """消费 provider 流：text → 动态区；done → markdown 定型；err → 错误块。"""
        assert self.provider is not None
        try:
            async for ev in self.provider.stream(self.conv.messages()):
                if ev.err is not None:
                    self._finish_with_error(ev.err)
                    return
                if ev.text:
                    self.cur_reply += ev.text
                    self._refresh_streaming_view()
                if ev.done:
                    self._finish_with_assistant(self.cur_reply)
                    return
            # 兜底：适配器保证 yield done/err，此处仅为防御
            self._finish_with_assistant(self.cur_reply)
        except asyncio.CancelledError:
            raise  # App 退出时取消本轮
        except Exception as e:
            self._finish_with_error(e)

    def _tick(self) -> None:
        """定时器回调：流式期间每 0.1s 刷新动画与秒数。"""
        if self.state is SessionState.STREAMING:
            self._refresh_streaming_view()

    def _refresh_streaming_view(self) -> None:
        """重绘动态区：流式正文（纯文本）+ "Imagining… (Ns)" 计时行。"""
        widget = self.query_one("#streaming", Static)  # type: ignore[attr-defined]
        if self.state is not SessionState.STREAMING:
            widget.display = False
            return
        elapsed = time.monotonic() - self.turn_start
        spinner = _SPINNER[int(elapsed * 10) % len(_SPINNER)]
        waiting = Text(f"{spinner} Imagining… ({int(elapsed)}s)", style="dim italic")
        if self.cur_reply:
            widget.update(Group(Text(f"● {self.cur_reply}"), waiting))
        else:
            widget.update(waiting)
        widget.display = True

    def _finish_with_assistant(self, reply: str) -> None:
        """正常结束：markdown 定型追加到对话区，历史记账，回到 IDLE。"""
        elapsed = time.monotonic() - self.turn_start
        self._stop_turn()
        log = self.query_one("#log", RichLog)  # type: ignore[attr-defined]
        log.write(assistant_block(reply, elapsed))
        self.conv.add_assistant(reply)
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
        """停止计时器、清理流式 task 引用、清空增量缓冲并隐藏动态区。"""
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._stream_task = None
        self.cur_reply = ""
        streaming = self.query_one("#streaming", Static)  # type: ignore[attr-defined]
        streaming.display = False
        streaming.update("")

    def _refocus_input(self) -> None:
        """回合结束后把焦点交还输入框。"""
        from .app import ChatTextArea  # 延迟导入避免循环

        self.query_one(ChatTextArea).focus()  # type: ignore[attr-defined]
