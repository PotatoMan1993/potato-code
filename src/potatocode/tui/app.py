"""PotatoCode TUI 主应用：状态机（SELECTING/IDLE/STREAMING）、布局与交互。"""

from __future__ import annotations

import asyncio
import logging
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

from ..agent import Agent, ApprovalRequest
from ..agent.runtime import SessionRuntime, new_runtime
from ..command import Registry as CommandRegistry
from ..command import register_builtins
from ..command.builtin_skill import register_skill_manage_command
from ..command.skill_register import register_skill_commands
from ..compact import sessions_root
from ..config import ProviderConfig, effective_context_window
from ..conversation import Conversation
from ..hook import Engine as HookEngine
from ..hook import Event as HookEvent
from ..hook import Payload as HookPayload
from ..hook import Rule as HookRule
from ..llm import Provider, ToolDefinition, new_provider
from ..mcp import Manager
from ..memory import Manager as MemoryManager
from ..permission import Mode, Outcome
from ..permission.engine import Engine
from ..prompt import render_banner
from ..session import SessionInfo, Writer
from ..skills.executor import SkillExecutor
from ..skills.loader import SkillLoader
from ..subagent import Catalog as SubagentCatalog
from ..task import Manager as TaskManager
from ..tool import Registry
from ..tool.load_skill import LoadSkill
from .commands import CommandMixin
from .complete import CompletionMenu, CompletionMixin
from .resume import ResumeSelect, do_resume_session
from .select import ProviderSelect
from .stream import StreamMixin
from .tasks import TasksMixin
from .view import (
    SessionState,
    ToolDisplay,
    error_notice_block,
    notice_block,
    statusbar_content,
    user_block,
)

logger = logging.getLogger(__name__)


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
            if app.completion.active and app.handle_completion_key(event.key):
                # 补全菜单激活：↑↓/Tab/回车/ESC 由菜单消费（ch12 F29-F32）
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


class PotatoCodeApp(CompletionMixin, CommandMixin, TasksMixin, StreamMixin, App):
    """PotatoCode 主应用。

    完成的消息（用户输入 / 助手回复 / 错误 / banner）持久追加到 RichLog，
    可滚动回看；动态区仅含「正在流式的回复 + 输入框 + 状态栏」。
    ch15：负责后台任务通知与子 Agent 审批升级两个消费协程的宿主。
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
    #completion {
        height: auto;
        max-height: 12;
        padding: 0 2;
        width: 1fr;
        display: none;
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
    #resume-screen {
        height: auto;
        margin: 2 4;
        display: none;
    }
    #resume-title {
        text-style: bold;
    }
    #resume-hint {
        color: $text-muted;
        margin-bottom: 1;
    }
    #resume-list {
        height: auto;
        max-height: 80%;
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
        manager: Manager | None = None,
        runtime: SessionRuntime | None = None,
        writer: Writer | None = None,
        mem_mgr: MemoryManager | None = None,
        instruction_text: str = "",
        memory_text: str = "",
        sessions_dir: str = "",
        workspace: str = "",
        skill_loader: SkillLoader | None = None,
        skill_executor: SkillExecutor | None = None,
        load_skill_tool: LoadSkill | None = None,
        skill_catalog_text: str = "",
        hook_engine: HookEngine | None = None,
        task_mgr: TaskManager | None = None,
        subagent_catalog: SubagentCatalog | None = None,
    ) -> None:
        super().__init__()
        self.providers = providers
        self._version = version
        # 注意：不能用 `_registry`——Textual App 内部已占用该名字（AppRegistry）
        self._tool_registry = registry
        # 斜杠命令注册中心：与工具注册中心相互独立（ch12 G1/G4）
        cmd_reg = CommandRegistry()
        register_builtins(cmd_reg)  # 名字/别名冲突在启动期立即抛错（F2）
        # ch13 Skill 系统：loader / executor 由 cli 装配后传入；未注入时 skill 功能关闭
        # （单测与脚本场景）。注册顺序：先内置命令，skill 命令随后——同名时 skill
        # 优先覆盖旧 handler（F14）。
        self.skill_loader = skill_loader
        self.skill_executor = skill_executor
        self._load_skill_tool = load_skill_tool
        self._skill_catalog_text = skill_catalog_text
        self._pending_fork_texts: list[str] = []  # fork 结果暂存（回合进行中不可写历史）
        if skill_loader is not None and skill_executor is not None:
            register_skill_commands(cmd_reg, skill_loader, skill_executor)
            register_skill_manage_command(
                cmd_reg, skill_loader, on_reload=self.rebind_skill_commands
            )
        if load_skill_tool is not None:
            # 注入 getter：Agent 惰性构造 / 换 provider 重建后引用依然有效；
            # host 注入 self（App 实现 SkillHost 窄协议，fork 结果经此回流）
            load_skill_tool.set_agent(self.ensure_agent)
            load_skill_tool.set_host(self)
        self.cmd_registry: CommandRegistry = cmd_reg
        self.completion = CompletionMenu()
        self.engine = engine  # 权限引擎（前四层），模式切换只影响后续兜底裁决
        self.hook_engine = hook_engine  # ch14 Hook 引擎：生命周期事件分派（未注入时无 hook）
        self.manager = manager  # mcp manager
        self.provider: Provider | None = None
        # 跨轮持有的长生命周期状态：压缩账本、文件追踪、熔断计数、用量锚点
        self.runtime = runtime if runtime is not None else new_runtime()
        self.agent: Agent | None = None  # 常驻 Agent，选定 provider 后构造一次
        self.compact_task: asyncio.Task | None = None  # 后台手动压缩任务
        # 会话存档与长期记忆（ch11）：未注入时保持无持久化的旧行为（测试与脚本场景）
        self.workspace = workspace or os.getcwd()
        self.sessions_dir = sessions_dir or sessions_root(self.workspace)
        self.writer = writer
        self.mem_mgr = mem_mgr
        self._instruction_text = instruction_text
        self._memory_text = memory_text
        self.conv = self._new_conversation([])
        self.state = SessionState.IDLE
        self.cur_reply = ""
        self.turn_start = 0.0
        self.mode: Mode = engine.start_mode()  # 当前权限模式，Shift+Tab /plan /do 切换，跨轮保持
        # ch14：hook 引擎同时挂在 runtime 上（reset_for_new_session 重置 only_once 用）
        self.runtime.hook_engine = self.hook_engine
        self.iter = 0  # 当前迭代轮次（进度显示，F9）
        self.usage_in = 0  # 会话累计输入 token（F8）
        self.usage_out = 0  # 会话累计输出 token（F8）
        self.cur_tools: list[ToolDisplay] = []  # 执行中工具指示（并发批可多个，F5/N2）
        self.turn_cancel: asyncio.Event | None = None  # 本轮取消事件（F7）
        self.pending: ApprovalRequest | None = None  # 待批准请求（APPROVING 态）
        self.approve_cursor: int = 0  # 待批准菜单光标（0/1/2）
        # ch15：主 Agent 与子 Agent 升级弹窗共用一把菜单锁，同一时刻最多一个弹窗；
        # 弹窗结束恢复到进入前的状态（主 Agent=STREAMING，后台子 Agent=IDLE）
        self.approval_menu_lock = asyncio.Lock()
        self._approval_prev_state: SessionState = SessionState.IDLE
        # ch15 后台任务管理器与子 Agent 目录：cli 注入；未注入时相关协程不启动
        self.task_mgr = task_mgr
        self.subagent_catalog = subagent_catalog
        self._task_consumers: list[asyncio.Task] = []  # 持引用防消费协程被回收
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
            yield Static(id="completion")
        with Vertical(id="select-screen"):
            yield Static("选择一个 provider：", id="select-title")
            yield ProviderSelect(self.providers, id="select")
        with Vertical(id="resume-screen"):
            yield Static("选择要恢复的会话：", id="resume-title")
            yield Static("", id="resume-hint")
            yield ResumeSelect(id="resume-list")
        yield Static(id="statusbar")

    async def on_mount(self) -> None:
        """启动：写 banner；单 provider 直进对话，多 provider 进选择界面。

        ch14：末尾派发 SessionStart——env context 装配完毕、首条 user 消息进入之前（F9）。
        """
        log = self.query_one("#log", RichLog)
        title = Text()
        title.append(render_banner(self._version, self.workspace), style="cyan")
        if self.manager is not None:  # 未注入 manager 的场景（单测）不展示 MCP 统计
            title.append(
                f"\n  Connected to {len(self.manager._sessions)} MCP server(s)", style="dim"
            )
            title.append(f", {len(self.manager._tools)} tools registered", style="dim")
        log.write(title)
        self._update_statusbar()

        if len(self.providers) == 1:
            self._activate_provider(self.providers[0])
        else:
            self._enter_selecting()
        await self._dispatch_session_start()

        # ch15：后台任务通知与子 Agent 审批升级的消费协程（task_mgr 未注入时不启动）
        if self.task_mgr is not None:
            self._task_consumers.append(asyncio.create_task(self._consume_task_done()))
            self._task_consumers.append(asyncio.create_task(self._consume_subagent_approvals()))

    @property
    def main_agent(self) -> Agent | None:
        """当前主 Agent（ch15 T29：Agent 工具经 getter 回填 parent 引用）。"""
        return self.agent

    # ────────────────────── 状态切换 ──────────────────────

    def _enter_selecting(self) -> None:
        """进入 provider 选择界面（隐藏对话区，仅显示列表）。"""
        self.state = SessionState.SELECTING
        self.query_one("#chat").display = False
        self.query_one("#select-screen").display = True
        self.query_one(ProviderSelect).focus()

    def _activate_provider(self, cfg: ProviderConfig) -> None:
        """选定 provider：构造适配器、注入上下文窗口、更新状态栏、切回对话界面。"""
        self.provider = new_provider(cfg)
        self.agent = None  # 换 provider 后重建 Agent（上下文窗口随之更新）
        self.runtime.context_window = effective_context_window(cfg)
        if self.writer is not None:  # 首行落盘带的模型名（ch11 F11）
            self.writer.set_model(cfg.model)
        if self.mem_mgr is not None:  # 记忆更新的 provider 与主会话一致（ch11 F37）
            self.mem_mgr.set_provider(self.provider, cfg.model)
        self.state = SessionState.IDLE
        self.query_one("#select-screen").display = False
        self.query_one("#chat").display = True
        self._update_statusbar()
        self.query_one(ChatTextArea).focus()

    def current_model(self) -> str:
        """当前模型名（未选定 provider 时为空串）。"""
        return self.provider.model if self.provider is not None else ""

    def current_defs(self) -> list[ToolDefinition]:
        """按当前权限模式取本轮工具定义（与主循环同一规则）。"""
        if self.mode is Mode.PLAN:
            return self._tool_registry.read_only_definitions()
        return self._tool_registry.definitions()

    def ensure_agent(self) -> Agent | None:
        """惰性构造常驻 Agent：选定 provider 后构造一次，跨轮复用同一实例。

        Agent 不再是每轮新建——压缩账本、用量锚点等状态挂在 Agent 持有的
        `runtime` 上，逐轮重建会让决策冻结与熔断器失效。
        """
        if self.agent is not None:
            return self.agent
        if self.provider is None:
            return None
        self.agent = Agent(
            self.provider,
            self._tool_registry,
            self.engine,
            self._version,
            runtime=self.runtime,
            memory_manager=self.mem_mgr,
            instruction_text=self._instruction_text,
            memory_text=self._memory_text,
            skill_catalog=self._skill_catalog_text,
            hook_engine=self.hook_engine,
        )
        return self.agent

    # ────────────────── ch14 Hook：生命周期事件派发（TUI 侧 5 个事件） ──────────────────

    def _hook_base_payload(self, event: HookEvent) -> HookPayload:
        """hook payload 的通用字段（ch14 F10）；TUI 侧事件由这里构造。"""
        return {
            "event": event.value,
            "session_id": self.runtime.session.session_id,
            "cwd": self.workspace,
            "mode": self.mode.name.lower(),
        }

    async def _dispatch_session_start(self) -> None:
        """SessionStart：启动 / /clear 新会话后派发；注入 prompt 进 reminder 队列（F9/F33）。"""
        if self.hook_engine is None:
            return
        result = await self.hook_engine.dispatch(
            HookEvent.SESSION_START, self._hook_base_payload(HookEvent.SESSION_START)
        )
        if result.injected_prompts:
            self.runtime.append_reminders(result.injected_prompts)

    async def _dispatch_session_end(self) -> None:
        """SessionEnd：/clear、/resume 离开旧会话前派发（F9）。"""
        if self.hook_engine is None:
            return
        await self.hook_engine.dispatch(
            HookEvent.SESSION_END, self._hook_base_payload(HookEvent.SESSION_END)
        )

    async def _dispatch_session_resume(self) -> None:
        """SessionResume：/resume 恢复完成后、首条 user 消息进入之前派发（F9）。"""
        if self.hook_engine is None:
            return
        result = await self.hook_engine.dispatch(
            HookEvent.SESSION_RESUME, self._hook_base_payload(HookEvent.SESSION_RESUME)
        )
        if result.injected_prompts:
            self.runtime.append_reminders(result.injected_prompts)

    def hook_rules(self) -> list[HookRule]:
        """/hooks 查询：已加载 hook 规则列表（未注入引擎时为空，ch14 F34）。"""
        return self.hook_engine.rules if self.hook_engine is not None else []

    def hook_sources(self) -> list[str]:
        """/hooks 查询：hook 配置加载来源文件列表（ch14 F34）。"""
        return self.hook_engine.sources if self.hook_engine is not None else []

    # ────────────────────── 会话恢复（ch11） ──────────────────────

    def _new_conversation(self, msgs: list) -> Conversation:
        """按当前 writer 构造 Conversation（无 writer 时不挂持久化回调）。"""
        if self.writer is None:
            return Conversation.from_messages(list(msgs))
        return Conversation.from_messages(
            list(msgs), on_append=self.writer.on_append, on_replace=self.writer.on_replace
        )

    def enter_resume_mode(self, items: list[SessionInfo]) -> None:
        """进入会话选择界面（隐藏对话区，仅显示列表）。"""
        select = self.query_one(ResumeSelect)
        select.set_sessions(items)
        self.state = SessionState.RESUMING
        self.query_one("#chat").display = False
        self.query_one("#resume-screen").display = True
        select.focus()
        self.update_resume_hint()

    def leave_resume_mode(self) -> None:
        """退出会话选择界面，回到空闲态。"""
        self.query_one("#resume-screen").display = False
        self.query_one("#chat").display = True
        self.state = SessionState.IDLE
        self._refocus_input()

    def update_resume_hint(self) -> None:
        """刷新列表下方的提示行（总数 / 过滤结果数 / 当前搜索词）。"""
        select = self.query_one(ResumeSelect)
        parts = [f"{select.shown_count}/{select.total} 个会话"]
        if select.filter_text:
            parts.append(f"搜索：{select.filter_text}")
        parts.append("↑↓ 选择 · 输入过滤 · Enter 恢复 · Esc 取消")
        self.query_one("#resume-hint", Static).update(" · ".join(parts))

    async def cancel_resume(self) -> None:
        """Esc：放弃恢复，当前会话保持不变。"""
        if self.state is not SessionState.RESUMING:
            return
        self.leave_resume_mode()

    async def commit_resume(self, index: int | None) -> None:
        """Enter：恢复选中的会话（恢复期间不接受新的输入）。"""
        if self.state is not SessionState.RESUMING:
            return
        info = self.query_one(ResumeSelect).session_at(index)
        if info is None:
            return
        self.query_one("#resume-list").disabled = True  # 防重复触发
        try:
            await do_resume_session(self, info)
        finally:
            self.query_one("#resume-list").disabled = False
            self.leave_resume_mode()

    def on_unmount(self) -> None:
        """退出时收尾：关闭会话存档文件句柄（F16）。"""
        if self.writer is not None:
            self.writer.close()

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
        """选择界面按 Enter 选定 provider。

        会话恢复列表是另一个 OptionList，其按键由 ResumeSelect 自行处理（F19/F46），
        这里只认 provider 选择界面。
        """
        if self.state is not SessionState.SELECTING:
            return
        select = self.query_one(ProviderSelect)
        cfg = select.selected_provider(event.option_index)
        self._activate_provider(cfg)

    async def on_chat_submitted(self, event: ChatSubmitted) -> None:
        """处理输入框提交：斜杠输入走命令分发，其余作为用户消息发起一轮对话（ch12 F3）。"""
        text = event.text.strip()
        input_box = self.query_one(ChatTextArea)
        # 任何提交都先关闭补全菜单（F30/F32）
        self.completion.hide()
        self._refresh_completion()
        if not text:
            input_box.clear()
            return

        if await self.dispatch_slash(text):
            input_box.clear()
            return

        if self.state is not SessionState.IDLE or self.provider is None:
            # 流式期间 / 未选定 provider：不接受新提交（保留已输入内容）
            return

        # ch14 UserPromptSubmit：写入对话历史之前派发，可拦截（F9/G6/F32）
        if self.hook_engine is not None:
            submit_result = await self.hook_engine.dispatch(
                HookEvent.USER_PROMPT_SUBMIT,
                self._hook_base_payload(HookEvent.USER_PROMPT_SUBMIT) | {"prompt": text},
            )
            if submit_result.blocked:
                # 拦截：错误块显示拒绝原因，输入框内容保留等用户重新编辑（F32）
                self.query_one("#log", RichLog).write(
                    error_notice_block(
                        f"[hook {submit_result.blocking_hook_name}] {submit_result.reason}"
                    )
                )
                self.query_one(ChatTextArea).focus()
                return
            if submit_result.injected_prompts:
                self.runtime.append_reminders(submit_result.injected_prompts)

        input_box.clear()
        self.conv.add_user(text)
        self.query_one("#log", RichLog).write(user_block(text))
        self._start_turn()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """输入框内容变化 → 同步补全菜单（ch12 F24/F26/F32a）。"""
        if event.text_area.id == "input":
            self._sync_completion_from_input()

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
        self.state = self._approval_prev_state  # ch15：恢复进入弹窗前的状态
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
