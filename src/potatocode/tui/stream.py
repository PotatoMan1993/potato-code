"""Agent Loop 消费与计时：`_consume_agent_events` 消费 agent 事件流驱动渲染。

以 mixin 形式混入 PotatoCodeApp（见 app.py）。mixin 引用的属性在 App 上定义，
此处仅在类型检查期声明，便于静态检查。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from rich.console import Group, RenderableType
from rich.text import Text
from textual.timer import Timer
from textual.widgets import RichLog, Static

from ..agent import NOTICE_CANCELLED, Agent, Mode, Phase, ToolEvent
from .view import (
    SessionState,
    ToolDisplay,
    assistant_block,
    error_block,
    notice_block,
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
    """一轮 Agent Loop 的消费与收尾逻辑。"""

    # —— 以下属性由 PotatoCodeApp 提供（仅类型声明，无运行时赋值）——
    state: SessionState
    provider: Provider | None
    conv: Conversation
    cur_reply: str
    turn_start: float
    mode: Mode
    iter: int
    usage_in: int
    usage_out: int
    cur_tools: list[ToolDisplay]
    turn_cancel: asyncio.Event | None
    _tool_registry: Registry
    _timer: Timer | None
    _stream_task: asyncio.Task | None

    if TYPE_CHECKING:

        def _update_statusbar(self) -> None: ...

    async def _consume_agent_events(self) -> None:
        """消费 agent 事件流：文本进动态区，工具行/结果摘要/提示进 scrollback。

        分派顺序：err → tool → usage → notice → iter → done → text（F3/F6）。
        """
        assert self.provider is not None
        assert self.turn_cancel is not None
        agent = Agent(self.provider, self._tool_registry)
        try:
            async for ev in agent.run(self.conv, self.mode, self.turn_cancel):
                if ev.err is not None:
                    self._finish_with_error(ev.err)
                    return
                if ev.tool is not None:
                    self._handle_tool_event(ev.tool)
                if ev.usage is not None:
                    # 会话累计计费用量（每轮 input+output 各自累加，F8/AC11）
                    self.usage_in += ev.usage.input
                    self.usage_out += ev.usage.output
                    self._update_statusbar()
                if ev.notice:
                    self.query_one("#log", RichLog).write(notice_block(ev.notice))  # type: ignore[attr-defined]
                if ev.iter > 0:
                    self.iter = ev.iter
                    self._refresh_streaming_view()
                if ev.done:
                    self._finish_turn(self.cur_reply)
                    return
                if ev.text:
                    self.cur_reply += ev.text
                    self._refresh_streaming_view()
            # generator 未 yield done 即终结：取消路径（或防御性兜底）
            if self.turn_cancel.is_set():
                self._finish_cancelled()
            else:
                self._finish_turn(self.cur_reply)
        except asyncio.CancelledError:
            raise  # App 退出时取消本轮
        except Exception as e:
            self._finish_with_error(e)

    def _handle_tool_event(self, te: ToolEvent) -> None:
        """分派工具事件：START 追加执行指示；END FIFO 弹首写工具行+结果摘要。

        agent 保证 PHASE_START 与 PHASE_END 都按调用序发出，结束序 == 入队序，
        弹首即对应工具，重名工具也不会错位（F5/N3）。
        """
        log = self.query_one("#log", RichLog)  # type: ignore[attr-defined]
        if te.phase == Phase.START:
            # 首个工具前先把已流式的 preamble 文本定型提交（N3 顺序）
            if self.cur_reply and not self.cur_tools:
                log.write(preamble_block(self.cur_reply))
                self.cur_reply = ""
            self.cur_tools.append(ToolDisplay(te.name, te.args))
        else:
            td = self.cur_tools.pop(0) if self.cur_tools else ToolDisplay(te.name, te.args) # agent层会按照模型输出的工具调用的顺序来上报各个工具的执行预览和执行结果
            log.write(tool_line(td.name, td.args))
            log.write(tool_result_summary(te.result, te.is_error))
        self._refresh_streaming_view()

    def _tick(self) -> None:
        """定时器回调：流式期间每 0.1s 刷新动画与秒数。"""
        if self.state is SessionState.STREAMING:
            self._refresh_streaming_view()

    def _refresh_streaming_view(self) -> None:
        """重绘动态区：流式正文 + 执行中工具行（可多个）/ "Imagining… (Ns · 第 N 轮)"。"""
        widget = self.query_one("#streaming", Static)  # type: ignore[attr-defined]
        if self.state is not SessionState.STREAMING:
            widget.display = False
            return
        elapsed = time.monotonic() - self.turn_start
        spinner = _SPINNER[int(elapsed * 10) % len(_SPINNER)]
        waiting: RenderableType
        if self.cur_tools:
            # 并发批可同时有多个工具在跑：逐行渲染（N2 执行指示）
            waiting = Group(
                *(
                    Text(f"{spinner} ● {t.name}({t.args}) Running…", style="dim italic")
                    for t in self.cur_tools
                )
            )
        else:
            round_info = f" · 第 {self.iter} 轮" if self.iter > 0 else ""
            waiting = Text(
                f"{spinner} Imagining… ({int(elapsed)}s{round_info})", style="dim italic"
            )
        if self.cur_reply:
            widget.update(Group(Text(f"● {self.cur_reply}"), waiting))
        else:
            widget.update(waiting)
        widget.display = True

    def _finish_turn(self, reply: str) -> None:
        """正常结束：最终答复 markdown 定型追加到对话区，回到 IDLE。

        会话历史由 agent 维护（已回灌 assistant/tool 回合），此处不重复记账。
        """
        elapsed = time.monotonic() - self.turn_start
        self._stop_turn()
        log = self.query_one("#log", RichLog)  # type: ignore[attr-defined]
        log.write(assistant_block(reply, elapsed))
        self.state = SessionState.IDLE
        self._refocus_input()

    def _finish_cancelled(self) -> None:
        """取消结束：写灰色提示、丢弃未定型的增量文本，回到 IDLE。

        历史一致性已由 agent 收尾（补「已取消」工具结果与 assistant 尾巴，F6）。
        """
        self._stop_turn()
        log = self.query_one("#log", RichLog)  # type: ignore[attr-defined]
        log.write(notice_block(NOTICE_CANCELLED))
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
        """停止计时器、清理本轮状态（增量缓冲/工具指示/取消事件），隐藏动态区。

        `mode` 与 `usage_in/usage_out` 跨轮保持，不在此清零。
        """
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._stream_task = None
        self.cur_reply = ""
        self.cur_tools = []
        self.iter = 0
        self.turn_cancel = None
        streaming = self.query_one("#streaming", Static)  # type: ignore[attr-defined]
        streaming.display = False
        streaming.update("")

    def _refocus_input(self) -> None:
        """回合结束后把焦点交还输入框。"""
        from .app import ChatTextArea  # 延迟导入避免循环

        self.query_one(ChatTextArea).focus()  # type: ignore[attr-defined]
