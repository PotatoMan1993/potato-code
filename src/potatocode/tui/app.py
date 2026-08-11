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

from .. import __version__
from ..config import ProviderConfig
from ..conversation import Conversation
from ..llm import Provider, new_provider
from ..prompt import render_banner
from .select import ProviderSelect
from .stream import StreamMixin
from .view import SessionState, statusbar_content, user_block


class ChatSubmitted(Message):
    """用户在输入框按 Enter 提交时投递的消息。"""

    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__()


class ChatTextArea(TextArea):
    """聊天输入框：Enter 提交，Alt+Enter 换行，Ctrl+C 退出。

    TextArea 默认在 `_on_key` 中直接把 enter 作为换行插入并 stop 事件，
    binding 无法覆盖，因此在 `_on_key` 里拦截（官方文档推荐的扩展方式）。
    """

    BINDINGS = [
        # 覆盖 TextArea 自带的 ctrl+c→复制，保证全局 Ctrl+C 退出
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    async def _on_key(self, event: events.Key) -> None:
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

    async def action_quit(self) -> None:
        """Ctrl+C：委托给 App 的安全退出（取消进行中的流）。"""
        await self.app.action_quit()


class PotatoCodeApp(StreamMixin, App):
    """PotatoCode 主应用。

    完成的消息（用户输入 / 助手回复 / 错误 / banner）持久追加到 RichLog，
    可滚动回看；动态区仅含「正在流式的回复 + 输入框 + 状态栏」。
    """

    TITLE = "PotatoCode"

    CSS = """
    #chat {
        height: 1fr;
    }
    #log {
        height: 1fr;
    }
    #streaming {
        height: auto;
        max-height: 60%;
        padding: 0 2;
        display: none;
    }
    #input-row {
        height: auto;
        max-height: 12;
        margin: 0 1;
    }
    #prompt {
        width: 2;
        color: $accent;
        text-style: bold;
        padding: 1 0 0 1;
    }
    #input {
        border: round $accent;
        height: auto;
        max-height: 10;
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
        Binding("ctrl+c", "quit", "Quit"),
    ]

    def __init__(self, providers: list[ProviderConfig]) -> None:
        super().__init__()
        self.providers = providers
        self.provider: Provider | None = None
        self.conv = Conversation()
        self.state = SessionState.IDLE
        self.cur_reply = ""
        self.turn_start = 0.0
        self._stream_task: asyncio.Task | None = None
        self._timer: Timer | None = None

    # ────────────────────────── 布局 ──────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical(id="chat"):
            yield RichLog(id="log", wrap=True, auto_scroll=True)
            yield Static(id="streaming")
            with Horizontal(id="input-row"):
                yield Static("❯", id="prompt")
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
        log.write(Text(render_banner(__version__, os.getcwd()), style="cyan"))
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
        """刷新底部状态栏：左侧 provider 名、右侧模型名。"""
        bar = self.query_one("#statusbar", Static)
        if self.provider is None:
            bar.update(statusbar_content(None, None))
        else:
            bar.update(statusbar_content(self.provider.name, self.provider.model))

    # ────────────────────── 事件处理 ──────────────────────

    def on_option_list_option_selected(self, event: ProviderSelect.OptionSelected) -> None:
        """选择界面按 Enter 选定 provider。"""
        if self.state is not SessionState.SELECTING:
            return
        select = self.query_one(ProviderSelect)
        cfg = select.selected_provider(event.option_index)
        self._activate_provider(cfg)

    async def on_chat_submitted(self, event: ChatSubmitted) -> None:
        """处理输入框提交：/exit 退出；IDLE 时发起一轮流式对话。"""
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

        input_box.clear()
        self.conv.add_user(text)
        self.query_one("#log", RichLog).write(user_block(text))
        self.cur_reply = ""
        self.turn_start = time.monotonic()
        self.state = SessionState.STREAMING
        self._refresh_streaming_view()
        self._stream_task = asyncio.create_task(self._consume_stream())
        self._timer = self.set_interval(0.1, self._tick)

    # ────────────────────── 退出 ──────────────────────

    async def action_quit(self) -> None:
        """安全退出：先取消进行中的流式 task，再退出应用（还原终端）。"""
        if self._stream_task is not None and not self._stream_task.done():
            self._stream_task.cancel()
        self.exit()
