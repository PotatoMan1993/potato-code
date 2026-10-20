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
from ..command import Kind
from ..command.dispatch import parse_with_args
from ..command.registry import Registry as CommandRegistry
from ..command.skill_register import build_skill_catalog_text, register_skill_commands
from ..compact import new_session_context
from ..permission import Mode
from ..session import Writer
from .resume import begin_resume
from .view import (
    SessionState,
    assistant_block,
    error_notice_block,
    format_compact_notice,
    notice_block,
    user_block,
)

if TYPE_CHECKING:
    from ..agent import Agent
    from ..llm import Provider
    from ..memory import Manager as MemoryManager
    from ..permission.engine import Engine
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
    # ch13 Skill 系统：loader / executor 由 cli 装配后传入；fork 结果暂存队列
    skill_loader: object  # SkillLoader | None
    skill_executor: object  # SkillExecutor | None
    _pending_fork_texts: list[str]
    # ch14 Hook 引擎：由 cli 装配后传入（未注入时 hook 功能关闭）
    hook_engine: object  # HookEngine | None

    if TYPE_CHECKING:

        def _update_statusbar(self) -> None: ...

        def _new_conversation(self, msgs: list) -> object: ...

        def _start_turn(self) -> None: ...

        def current_model(self) -> str: ...

        def current_defs(self) -> list: ...

        def ensure_agent(self) -> Agent | None: ...

        async def _dispatch_session_start(self) -> None: ...

        async def _dispatch_session_end(self) -> None: ...

        async def _dispatch_session_resume(self) -> None: ...

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

    # ────────────────── UI Protocol：Hook 查询（ch14 T21） ──────────────────

    def hook_rules(self) -> list:
        """已加载 hook 规则列表（实现在 PotatoCodeApp；/hooks 命令用）。"""
        return self.hook_engine.rules if self.hook_engine is not None else []

    def hook_sources(self) -> list[str]:
        """hook 配置加载来源文件列表（/hooks 命令用）。"""
        return self.hook_engine.sources if self.hook_engine is not None else []

    # ────────────────── UI Protocol：Skill 支持（ch13） ──────────────────

    def current_agent(self) -> Agent | None:
        """当前主 Agent（未选定 provider 时为 None）：/clear 钩子与 skill 命令用。"""
        return self.ensure_agent()

    def append_assistant_message(self, text: str) -> None:
        """fork skill 结果回流：追加 assistant 回合 + scrollback 展示（plan fork 分支）。

        回合进行中历史正被主循环改写，此时追加会插进 assistant↔tool 配对中间破坏
        历史合法性，因此先暂存，当前回合收尾时再补写（_flush_pending_skill_results）。
        """
        if self.state is SessionState.IDLE:
            self._append_skill_result_now(text)
        else:
            self._pending_fork_texts.append(text)

    def _append_skill_result_now(self, text: str) -> None:
        """立即把一条 fork 结果写入 conv 历史 + scrollback（idle 态才可直接调用）。"""
        self.conv.add_assistant(text)
        self.query_one("#log", RichLog).write(assistant_block(text, 0.0))  # type: ignore[attr-defined]

    def _flush_pending_skill_results(self) -> None:
        """回合收尾后补写暂存的 fork 结果，保持 conv 与 scrollback 顺序一致。"""
        while self._pending_fork_texts:
            pending_text = self._pending_fork_texts.pop(0)
            self._append_skill_result_now(pending_text)

    def skill_deliver_result(self, text: str) -> None:
        """SkillHost 协议：fork 结果回流（复用 append_assistant_message 的暂存语义）。"""
        self.append_assistant_message(text)

    def rebind_skill_commands(self) -> None:
        """skill 目录变化后重建斜杠命令并刷新 Agent 的 Available Skills 目录。

        InstallSkill 安装完成与 /skill reload 都走这里：命令清单、补全菜单、
        env 段的目录文本三者同步更新。
        """
        if self.skill_loader is None or self.skill_executor is None:
            return
        register_skill_commands(self.cmd_registry, self.skill_loader, self.skill_executor)  # type: ignore[arg-type]
        agent = self.ensure_agent()
        if agent is not None:
            agent.set_skill_catalog(build_skill_catalog_text(self.skill_loader))  # type: ignore[arg-type]

    # ────────────────── SkillHost 协议（skills 包 fork 执行的宿主能力面） ──────────────────

    def skill_agent(self) -> Agent | None:
        return self.ensure_agent()

    def skill_provider(self) -> Provider | None:
        return self.provider

    def skill_tool_registry(self) -> ToolRegistry:
        return self._tool_registry

    def skill_engine(self) -> Engine:
        return self.engine

    def skill_version(self) -> str:
        return self._version

    def skill_conversation(self) -> object:
        return self.conv

    def skill_runtime(self) -> object:
        return self.runtime

    def skill_workspace(self) -> str:
        return self.workspace

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

    async def clear_and_new_session(self) -> None:
        """/clear：关旧存档 → 开新会话 → 重建 conv 与 runtime → 清零计数与界面（F17/N9）。

        旧 JSONL 保留在磁盘上，/resume 列表仍能看到旧会话。
        ch14：重建前派发 SessionEnd（旧会话），重建后派发 SessionStart（新会话，F9）。
        """
        await self._dispatch_session_end()
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
        await self._dispatch_session_start()

    # ────────────────── 斜杠分发 ──────────────────

    async def dispatch_slash(self, text: str) -> bool:
        """分发一条已 strip 的输入；返回 True 表示已按命令处理（含未命中提示）。

        非 `/` 开头返回 False，上层送 Agent（F3）；未命中输出 /help 引导（F6）；
        UI/PROMPT 类命令仅 idle 可执行（N3a），/exit 沿用既有行为任何时候都允许。
        ch13 扩展：命令声明 accepts_args 时可携带参数尾巴，由 args_handler 消费；
        其余命令携带参数尾巴仍按未命中处理（F7 行为不回归）。
        """
        name, args, is_slash = parse_with_args(text)
        if not is_slash:
            return False
        cmd = self.cmd_registry.lookup(name) if name else None
        if cmd is not None and args and not cmd.accepts_args:
            cmd = None  # 携带参数尾巴但命令不接受参数：按未命中处理（F7）
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
            if cmd.accepts_args and cmd.args_handler is not None:
                await cmd.args_handler(self, args)
            else:
                await cmd.handler(self)
        except Exception as exc:
            logger.exception("命令 /%s 执行失败", cmd.name)
            self.error(f"{type(exc).__name__}: {exc}")
        return True
