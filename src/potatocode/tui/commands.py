"""命令系统与 TUI 的桥接层：App 实现 UI Protocol + 斜杠分发入口（ch12 G3/F33-F35）。

命令逻辑本体在 `potatocode.command` 包（纯领域逻辑，不依赖 Textual）；本模块以
mixin 形式把 App 的状态与能力按 UI Protocol 的形态暴露给命令 handler，并承担
idle 守护与未命中提示等分发规则。旧的内联 handler 注册表已收编进 command 包。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from textual.widgets import RichLog

from ..agent import CompactEvent, CompactPhase
from ..command import Kind, parse
from ..command.registry import Registry as CommandRegistry
from ..compact import new_session_context
from ..permission import Mode
from ..session import Writer
from .resume import begin_resume
from .view import (
    SessionState,
    error_notice_block,
    format_compact_notice,
    notice_block,
    user_block,
)

if TYPE_CHECKING:
    from ..agent import Agent
    from ..llm import Provider
    from ..memory import Manager as MemoryManager
    from ..tool import Registry as ToolRegistry

logger = logging.getLogger(__name__)

# 未命中提示的固定引导文案：/help 是唯一的命令入口引导（N7）
UNKNOWN_HINT = "输入 /help 查看可用命令"


class CommandMixin:
    """PotatoCodeApp 的命令 mixin：UI Protocol 全部方法 + `dispatch_slash` 入口。"""

    # —— 以下属性由 PotatoCodeApp 提供（仅类型声明，无运行时赋值）——
    mode: Mode
    usage_in: int
    usage_out: int
    iter: int
    state: SessionState
    provider: Provider | None
    agent: Agent | None
    conv: object  # Conversation
    writer: Writer | None
    mem_mgr: MemoryManager | None
    runtime: object  # SessionRuntime
    workspace: str
    compact_task: asyncio.Task | None
    _tool_registry: ToolRegistry
    cmd_registry: CommandRegistry

    if TYPE_CHECKING:

        def _update_statusbar(self) -> None: ...

        def _new_conversation(self, msgs: list) -> object: ...

        def _start_turn(self) -> None: ...

        def current_model(self) -> str: ...

        def current_defs(self) -> list: ...

        def ensure_agent(self) -> Agent | None: ...

        async def action_quit(self) -> None: ...

    # ────────────────── UI Protocol：输出 ──────────────────

    def println(self, msg: str) -> None:
        """系统提示样式输出（notice 样式，不进对话历史）。"""
        self.query_one("#log", RichLog).write(notice_block(msg))  # type: ignore[attr-defined]

    def error(self, msg: str) -> None:
        """错误样式输出（红色，可读失败信息）。"""
        self.query_one("#log", RichLog).write(error_notice_block(msg))  # type: ignore[attr-defined]

    # ────────────────── UI Protocol：权限模式 ──────────────────

    def current_mode(self) -> Mode:
        return self.mode

    def set_mode(self, m: Mode) -> None:
        """切换权限模式；状态栏在下一帧渲染生效（N2）。"""
        self.mode = m
        self._update_statusbar()

    # ────────────────── UI Protocol：提示词注入 ──────────────────

    def inject_and_send(self, display_label: str, preset_prompt: str) -> None:
        """追加一条 user 消息并立即触发 LLM 回合（F11/N3）。

        preset_prompt 与真实用户消息走同一条 conv 追加 + JSONL 存档路径；
        scrollback 只展示 display_label。
        """
        if self.provider is None:
            self.error("尚未选定 provider，无法开始对话")
            return
        self.conv.add_user(preset_prompt)
        self.query_one("#log", RichLog).write(user_block(display_label))  # type: ignore[attr-defined]
        self._start_turn()

    # ────────────────── UI Protocol：只读查询 ──────────────────

    def tokens_in(self) -> int:
        return self.usage_in

    def tokens_out(self) -> int:
        return self.usage_out

    def model_name(self) -> str:
        return self.provider.model if self.provider is not None else ""

    def cwd(self) -> str:
        return self.workspace

    def tool_count(self) -> int:
        return self._tool_registry.count()

    def memory_files(self) -> list[str]:
        if self.mem_mgr is None:  # 未注入记忆管理器的场景（单测）
            return []
        project, user = self.mem_mgr.list_files()
        return [*project, *user]

    def session_path(self) -> str:
        return self.writer.path if self.writer is not None else ""

    def session_id(self) -> str:
        return self.runtime.session.session_id

    def idle(self) -> bool:
        return self.state is SessionState.IDLE

    # ────────────────── UI Protocol：影响界面动作 ──────────────────

    def quit(self) -> None:
        """安全退出：先取消进行中的流式 task 与后台任务，再退出应用（N12）。"""
        asyncio.create_task(self.action_quit())

    def force_compact(self) -> None:
        """手动压缩：提示后转后台任务执行，完成后回投系统消息（沿用 ch08 行为）。"""
        agent = self.ensure_agent()
        if agent is None:
            self.println("尚未选定 provider，无法压缩上下文")
            return
        self.println(format_compact_notice(CompactEvent(phase=CompactPhase.BEFORE_AUTO)))
        self.compact_task = asyncio.create_task(self._run_force_compact(agent))

    async def _run_force_compact(self, agent: Agent) -> None:
        """后台执行手动压缩：捕获异常后按统一格式回投成功 / 失败文案。"""
        before = 0
        after = 0
        err: Exception | None = None
        try:
            before, after = await agent.run_force_compact(self.conv, self.current_defs())
        except Exception as e:
            err = e
        self.query_one("#log", RichLog).write(  # type: ignore[attr-defined]
            notice_block(
                format_compact_notice(
                    CompactEvent(phase=CompactPhase.AFTER_AUTO, before=before, after=after, err=err)
                )
            )
        )

    def open_resume_menu(self) -> None:
        """打开历史会话列表（ch09 的扫描与进场逻辑原样复用）。"""
        asyncio.create_task(begin_resume(self))

    def clear_and_new_session(self) -> None:
        """/clear：关旧存档 → 开新会话 → 重建 conv 与 runtime → 清零计数与界面（F17/N9）。

        旧 JSONL 保留在磁盘上，/resume 列表仍能看到旧会话。
        """
        if self.writer is not None:
            self.writer.close()
        try:
            new_ctx = new_session_context(self.workspace)
        except Exception as e:
            self.error(f"开启新会话失败：{type(e).__name__}: {e}")
            return
        try:
            new_writer = Writer(new_ctx.session_dir, model=self.current_model())
        except Exception as e:
            self.error(f"打开会话存档失败：{type(e).__name__}: {e}")
            return
        self.writer = new_writer
        # 旧 writer 的 hook 已失效，必须重建 conv 才能挂上新 writer
        self.conv = self._new_conversation([])
        self.runtime.reset_for_new_session(new_ctx)
        self.iter = 0
        self.usage_in = 0
        self.usage_out = 0
        self._update_statusbar()
        try:
            self.query_one("#log", RichLog).clear()  # type: ignore[attr-defined]
        except Exception:
            logger.exception("清空对话区失败")

    # ────────────────── 斜杠分发 ──────────────────

    async def dispatch_slash(self, text: str) -> bool:
        """分发一条已 strip 的输入；返回 True 表示已按命令处理（含未命中提示）。

        非 `/` 开头返回 False，上层送 Agent（F3）；未命中输出 /help 引导（F6）；
        UI/PROMPT 类命令仅 idle 可执行（N3a），/exit 沿用既有行为任何时候都允许。
        """
        name, is_slash = parse(text)
        if not is_slash:
            return False
        cmd = self.cmd_registry.lookup(name) if name else None
        if cmd is None:
            # 退化输入（纯 / 或带参数尾巴）不回显名字，避免出现悬空斜杠
            if name:
                self.println(f"未知命令: /{name}，{UNKNOWN_HINT}")
            else:
                self.println(f"未知命令，{UNKNOWN_HINT}")
            return True
        if cmd.name != "exit" and cmd.kind is not Kind.LOCAL and not self.idle():
            self.error("请等待当前任务完成")
            return True
        try:
            await cmd.handler(self)
        except Exception as exc:
            logger.exception("命令 /%s 执行失败", cmd.name)
            self.error(f"{type(exc).__name__}: {exc}")
        return True
