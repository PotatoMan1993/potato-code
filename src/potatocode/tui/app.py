"""PotatoCode TUI 主应用：状态机（SELECTING/IDLE/STREAMING）、布局与交互。"""

from __future__ import annotations

import asyncio
import os
import time

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.timer import Timer
from textual.widgets import RichLog, Static, TextArea

from ..agent import ApprovalRequest
from ..config import ProviderConfig
from ..conversation import Conversation
from ..llm import Provider, new_provider
from ..permission import Mode, Outcome
from ..permission.engine import Engine
from ..mcp import Manager
from ..prompt import EXECUTE_DIRECTIVE, render_banner
from ..tool import Registry
from .select import ProviderSelect
from .stream import StreamMixin
from .view import SessionState, ToolDisplay, notice_block, statusbar_content, user_block


def next_mode(mode: Mode) -> Mode:
    """循环切换权限模式：DEFAULT → ACCEPT_EDITS → PLAN → BYPASS → DEFAULT（F7）。"""
    return Mode((int(mode) + 1) % 4)


class ChatSubmitted(Message):
    """用户在输入框按 Enter 提交时投递的消息。"""

    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__()


class ChatTextArea(TextArea):
    """聊天输入框：Enter 提交，Alt+Enter 换行，Ctrl+C 取消本轮/退出。

    TextArea 默认在 `_on_key` 中直接把 enter 作为换行插入并 stop 事件，
    binding 无法覆盖，因此在 `_on_key` 里拦截（官方文档推荐的扩展方式）。
    """

    BINDINGS = [
        # 覆盖 TextArea 自带的 ctrl+c→复制；流式态取消本轮、空闲态退出（F7）
        Binding("ctrl+c", "cancel_or_quit", "Quit", show=False),
    ]

    async def _on_key(self, event: events.Key) -> None:
        app = self.app
        if isinstance(app, PotatoCodeApp):
            if app.state is SessionState.APPROVING:
                # 人在回路菜单键：↑↓/回车/数字键/y/n 都交给 update_approving 分派
                app.update_approving(event.key)
                event.stop()
                event.prevent_default()
                return
            if event.key == "shift+tab" and app.state is SessionState.IDLE:
                event.stop()
                event.prevent_default()
                app.action_cycle_mode()
                return
        if event.key == "enter":
            # Enter：提交当前文本（拦截默认换行）
            event.stop()
            event.prevent_default()
            self.post_message(ChatSubmitted(self.text))
            return
        if event.key == "alt+enter":
            # Alt+Enter：多行编辑，插入换行
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        await super()._on_key(event)

    def action_cancel_or_quit(self) -> None:
        """Ctrl+C：委托给 App——流式态取消本轮，空闲态退出程序（F7/AC10）。"""
        app = self.app
        assert isinstance(app, PotatoCodeApp)
        app.action_cancel_or_quit()


class PotatoCodeApp(StreamMixin, App):
    """PotatoCode 主应用。

    完成的消息（用户输入 / 助手回复 / 错误 / banner）持久追加到 RichLog，
    可滚动回看；动态区仅含「正在流式的回复 + 输入框 + 状态栏」。
    """

    TITLE = "PotatoCode"

    CSS = """
    #chat {
        height: 1fr;
        width: 1fr;
    }
    #log {
        height: 1fr;
        width: 1fr;
    }
    #streaming {
        height: auto;
        max-height: 60%;
        padding: 0 2;
        display: none;
        width: 1fr;
    }
    #input-row {
        height: auto;
        max-height: 12;
        # margin: 0 1;
        width: 1fr;
        background: $surface;
    }
    #prompt {
        text-style: bold;
        border: round $accent;
        border-left: none;
        border-right: none;
        height: auto;
        max-height: 10;
        width: 2;
        # color: $accent;
    }
    #input {
        border: round $accent;
        border-left: none;
        border-right: none;
        height: auto;
        max-height: 10;
        width: 1fr;
    }
    #select-screen {
        height: auto;
        margin: 2 4;
        display: none;
    }
    #select-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #select {
        height: auto;
        border: round $accent;
    }
    #statusbar {
        dock: bottom;
        height: 1;
        background: $surface;
        padding: 0 1;
    }
    """

    BINDINGS = [
        # 流式态取消本轮（不退出）；空闲态退出程序（F7/AC10）
        Binding("ctrl+c", "cancel_or_quit", "Quit"),
        # Esc：仅流式态有意义——取消本轮
        Binding("escape", "cancel_turn", "Cancel turn", show=False),
    ]

    def __init__(
        self,
        providers: list[ProviderConfig],
        version: str,
        registry: Registry,
        engine: Engine,
        manager: Manager,
    ) -> None:
        super().__init__()
        self.providers = providers
        self._version = version
        # 注意：不能用 `_registry`——Textual App 内部已占用该名字（AppRegistry）
        self._tool_registry = registry
        self.engine = engine  # 权限引擎（前四层），模式切换只影响后续兜底裁决
        self.manager = manager  # mcp manager
        self.provider: Provider | None = None
        self.conv = Conversation()
        self.state = SessionState.IDLE
        self.cur_reply = ""
        self.turn_start = 0.0
        self.mode: Mode = engine.start_mode()  # 当前权限模式，Shift+Tab /plan /do 切换，跨轮保持
        self.iter = 0  # 当前迭代轮次（进度显示，F9）
        self.usage_in = 0  # 会话累计输入 token（F8）
        self.usage_out = 0  # 会话累计输出 token（F8）
        self.cur_tools: list[ToolDisplay] = []  # 执行中工具指示（并发批可多个，F5/N2）
        self.turn_cancel: asyncio.Event | None = None  # 本轮取消事件（F7）
        self.pending: ApprovalRequest | None = None  # 待批准请求（APPROVING 态）
        self.approve_cursor: int = 0  # 待批准菜单光标（0/1/2）
        self._stream_task: asyncio.Task | None = None
        self._timer: Timer | None = None

    # ────────────────────────── 布局 ──────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical(id="chat"):
            yield RichLog(id="log", wrap=True, auto_scroll=True)
            yield Static(id="streaming")
            with Horizontal(id="input-row"):
                yield Static(">", id="prompt")
                yield ChatTextArea(
                    id="input",
                    placeholder="Send a message...",
                    soft_wrap=True,
                )
        with Vertical(id="select-screen"):
            yield Static("选择一个 provider：", id="select-title")
            yield ProviderSelect(self.providers, id="select")
        yield Static(id="statusbar")

    def on_mount(self) -> None:
        """启动：写 banner；单 provider 直进对话，多 provider 进选择界面。"""
        log = self.query_one("#log", RichLog)
        title = Text()
        title.append(render_banner(self._version, os.getcwd()), style="cyan")
        title.append(f"\n  Connected to {len(self.manager._sessions)} MCP server(s)", style="dim")
        title.append(f", {len(self.manager._tools)} tools registered", style="dim")
        log.write(title)
        self._update_statusbar()

        if len(self.providers) == 1:
            self._activate_provider(self.providers[0])
        else:
            self._enter_selecting()

    # ────────────────────── 状态切换 ──────────────────────

    def _enter_selecting(self) -> None:
        """进入 provider 选择界面（隐藏对话区，仅显示列表）。"""
        self.state = SessionState.SELECTING
        self.query_one("#chat").display = False
        self.query_one("#select-screen").display = True
        self.query_one(ProviderSelect).focus()

    def _activate_provider(self, cfg: ProviderConfig) -> None:
        """选定 provider：构造适配器、更新状态栏、切回对话界面。"""
        self.provider = new_provider(cfg)
        self.state = SessionState.IDLE
        self.query_one("#select-screen").display = False
        self.query_one("#chat").display = True
        self._update_statusbar()
        self.query_one(ChatTextArea).focus()

    def _update_statusbar(self) -> None:
        """刷新底部状态栏：左侧常驻权限模式、右侧模型名 + 累计用量。"""
        bar = self.query_one("#statusbar", Static)
        model = self.provider.model if self.provider is not None else None
        bar.update(
            statusbar_content(
                self.mode,
                model,
                usage_in=self.usage_in,
                usage_out=self.usage_out,
            )
        )

    # ────────────────────── 事件处理 ──────────────────────

    def on_option_list_option_selected(self, event: ProviderSelect.OptionSelected) -> None:
        """选择界面按 Enter 选定 provider。"""
        if self.state is not SessionState.SELECTING:
            return
        select = self.query_one(ProviderSelect)
        cfg = select.selected_provider(event.option_index)
        self._activate_provider(cfg)

    async def on_chat_submitted(self, event: ChatSubmitted) -> None:
        """处理输入框提交：/exit /plan /do 命令；IDLE 时发起一轮流式对话。"""
        text = event.text.strip()
        input_box = self.query_one(ChatTextArea)

        if text == "/exit":
            await self.action_quit()
            return
        if not text:
            input_box.clear()
            return
        if self.state is not SessionState.IDLE or self.provider is None:
            # 流式期间 / 未选定 provider：不接受新提交（保留已输入内容）
            return

        if text == "/plan":
            # 进入计划模式：仅只读工具 + 计划态系统后缀（F10）
            input_box.clear()
            self.mode = Mode.PLAN
            self.query_one("#log", RichLog).write(
                notice_block("已进入计划模式（仅只读工具；输入 /do 按计划执行）")
            )
            self._update_statusbar()
            return
        if text == "/do":
            # 切回 default 模式，注入执行指令立即按上文计划开跑（/do 本身不入历史）
            input_box.clear()
            self.mode = Mode.DEFAULT
            self._update_statusbar()
            self.conv.add_user(EXECUTE_DIRECTIVE)
            self.query_one("#log", RichLog).write(user_block(EXECUTE_DIRECTIVE))
            self._start_turn()
            return

        input_box.clear()
        self.conv.add_user(text)
        self.query_one("#log", RichLog).write(user_block(text))
        self._start_turn()

    def _start_turn(self) -> None:
        """启动一轮 Agent Loop：建取消事件、起消费 task、进 STREAMING 态。"""
        self.cur_reply = ""
        self.cur_tools = []
        self.iter = 0
        self.turn_start = time.monotonic()
        self.turn_cancel = asyncio.Event()
        self.state = SessionState.STREAMING
        self._refresh_streaming_view()
        self._stream_task = asyncio.create_task(self._consume_agent_events())
        self._timer = self.set_interval(0.1, self._tick)

    # ────────────────────── 取消与退出 ──────────────────────

    def action_cancel_or_quit(self) -> None:
        """Ctrl+C：流式/待批准态取消本轮（不退出程序）；空闲态退出程序（F7/AC10）。"""
        if self.state is SessionState.APPROVING:
            self._cancel_approval()
        elif self.state is SessionState.STREAMING and self.turn_cancel is not None:
            self.turn_cancel.set()
        else:
            self.exit()

    def action_cancel_turn(self) -> None:
        """Esc：流式/待批准态取消本轮；其余状态忽略（F7/AC10）。"""
        if self.state is SessionState.APPROVING:
            self._cancel_approval()
        elif self.state is SessionState.STREAMING and self.turn_cancel is not None:
            self.turn_cancel.set()

    def _cancel_approval(self) -> None:
        """人在回路等待中取消本轮（N4）：先兜底解开 agent 等待，再触发取消收尾。"""
        if self.pending is not None:
            self.pending.respond.set_result(Outcome.DENY_ONCE)
        self.pending = None
        self.state = SessionState.STREAMING
        if self.turn_cancel is not None:
            self.turn_cancel.set()
        self._refresh_streaming_view()

    def action_cycle_mode(self) -> None:
        """Shift+Tab：循环切换权限模式（仅 idle 态生效），跨轮保持（F7）。"""
        if self.state is not SessionState.IDLE:
            return
        self.mode = next_mode(self.mode)
        self._update_statusbar()
        self.query_one("#log", RichLog).write(notice_block(f"已切换到 {self.mode} 模式"))

    async def action_quit(self) -> None:
        """安全退出：先取消进行中的流式 task，再退出应用（还原终端）。"""
        if self._stream_task is not None and not self._stream_task.done():
            self._stream_task.cancel()
        self.exit()
