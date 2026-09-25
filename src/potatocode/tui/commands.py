"""TUI 内置命令注册表与分发：以 `/` 开头的输入走命令路径，不发送给 LLM。

命令不属于对话语义，因此命令路径既不写入对话历史、也不发起普通对话请求；
执行结果只通过系统消息渲染到 scrollback（视图层），不进 `conv.messages()`。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING

from textual.widgets import RichLog

from ..agent import CompactEvent, CompactPhase
from ..permission import Mode
from ..prompt import EXECUTE_DIRECTIVE
from .view import notice_block, user_block

if TYPE_CHECKING:
    from ..agent import Agent
    from .app import PotatoCodeApp

# 命令处理器签名：接收 App，异步执行，无返回值
CommandHandler = Callable[..., Awaitable[None]]


def format_compact_notice(ev: CompactEvent) -> str:
    """把压缩事件渲染成系统消息文案（自动 / 紧急 / 手动三条路径共用同一格式）。"""
    if ev.phase is CompactPhase.BEFORE_AUTO:
        return "正在压缩上下文..."
    if ev.phase is CompactPhase.BEFORE_EMERGENCY:
        return "上下文撞墙，自动压缩中..."
    if ev.err is not None:
        return f"压缩失败：{ev.err}"
    return f"已压缩，token 从 {ev.before} 降至 {ev.after}"


def _write_notice(app: PotatoCodeApp, text: str) -> None:
    """把一条系统消息写入 scrollback（仅视图层，不入对话历史）。"""
    app.query_one("#log", RichLog).write(notice_block(text))


async def handle_exit(app: PotatoCodeApp) -> None:
    """`/exit`：取消进行中的本轮并安全退出应用。"""
    await app.action_quit()


async def handle_plan(app: PotatoCodeApp) -> None:
    """`/plan`：切到计划模式（仅只读工具），提示用 /do 继续执行。"""
    app.mode = Mode.PLAN
    app._update_statusbar()
    _write_notice(app, "已进入计划模式（仅只读工具；输入 /do 按计划执行）")


async def handle_do(app: PotatoCodeApp) -> None:
    """`/do`：切回 default 模式，注入执行指令并立即按上文计划开跑一轮。"""
    app.mode = Mode.DEFAULT
    app._update_statusbar()
    app.conv.add_user(EXECUTE_DIRECTIVE)
    app.query_one("#log", RichLog).write(user_block(EXECUTE_DIRECTIVE))
    app._start_turn()


async def handle_compact(app: PotatoCodeApp) -> None:
    """`/compact`：立即提示后转后台任务做一次无条件摘要，完成后回投系统消息。

    摘要耗时以秒计，不能占住 TUI 的消息循环，因此放到独立 task 里；
    该 task 与 Agent 主循环通过会话级锁串行，不会并发改写同一份历史。
    """
    agent = app.ensure_agent()
    if agent is None:
        _write_notice(app, "尚未选定 provider，无法压缩上下文")
        return
    _write_notice(app, format_compact_notice(CompactEvent(phase=CompactPhase.BEFORE_AUTO)))
    app.compact_task = asyncio.create_task(_run_compact(app, agent))


async def _run_compact(app: PotatoCodeApp, agent: Agent) -> None:
    """后台执行手动压缩：捕获异常后按统一格式回投成功 / 失败文案。"""
    before = 0
    after = 0
    err: Exception | None = None
    try:
        before, after = await agent.run_force_compact(app.conv, app.current_defs())
    except Exception as e:
        err = e
    _write_notice(
        app,
        format_compact_notice(
            CompactEvent(phase=CompactPhase.AFTER_AUTO, before=before, after=after, err=err)
        ),
    )


async def handle_unknown(app: PotatoCodeApp, input_: str = "") -> None:
    """未注册命令兜底：提示可用命令列表，不发送给 LLM。"""
    names = " ".join(BUILTIN_COMMANDS)
    _write_notice(app, f"未知命令: {input_}，可用命令: {names}")


# 内置命令注册表：/exit /plan /do 为既有命令迁移而来，/compact 为本章新增
BUILTIN_COMMANDS: dict[str, CommandHandler] = {
    "/exit": handle_exit,
    "/plan": handle_plan,
    "/do": handle_do,
    "/compact": handle_compact,
}


def dispatch_command(input_: str) -> tuple[CommandHandler | None, bool]:
    """判断输入是否走命令路径。

    返回 `(处理器, 是否命令)`：非 `/` 开头返回 `(None, False)`；
    已注册命令返回对应处理器；未注册命令返回兜底处理器（同样不发给 LLM）。
    """
    text = input_.strip()
    if not text.startswith("/"):
        return (None, False)
    handler = BUILTIN_COMMANDS.get(text)
    if handler is None:
        return (partial(handle_unknown, input_=text), True)
    return (handler, True)
