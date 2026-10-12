"""TUI 视图层：各状态的渲染拼装——状态栏、用户块、markdown 定型、工具行、错误样式。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from ..agent import CompactEvent, CompactPhase
from ..llm import ROLE_ASSISTANT, ROLE_TOOL, ROLE_USER, Message
from ..permission import Mode

# 结果摘要在界面上的行数上限（N5：与回灌给模型的截断相互独立）
_RESULT_SUMMARY_LINES = 8

# 回放历史时工具调用参数的展示宽度上限（超长参数压成一行并截断）
_TOOL_ARGS_WIDTH = 80


class SessionState(Enum):
    """会话状态机。"""

    SELECTING = "selecting"  # 多 provider 时的选择界面
    IDLE = "idle"  # 等待用户输入
    STREAMING = "streaming"  # 等待/接收模型流（loading + 计时）
    APPROVING = "approving"  # 等待用户在人在回路菜单上做选择（F8）
    RESUMING = "resuming"  # /resume 的历史会话选择界面（ch11 F19）


@dataclass
class ToolDisplay:
    """进行中的工具调用展示信息（渲染执行指示用）。"""

    name: str
    args: str


def _fmt_tokens(n: int) -> str:
    """紧凑数字：1234 → 1.2k（状态栏用量展示，AC11）。"""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def statusbar_content(
    mode: Mode,
    model: str | None,
    usage_in: int = 0,
    usage_out: int = 0,
) -> RenderableType:
    """底部状态栏：左侧常驻显示当前权限模式（取代 provider 名）、右侧模型名 + 累计用量。"""
    label, style = {
        Mode.DEFAULT: ("DEFAULT", "green"),
        Mode.ACCEPT_EDITS: ("ACCEPT EDITS", "green"),
        Mode.PLAN: ("PLAN", "yellow"),
        Mode.BYPASS: ("BYPASS", "red"),
    }[mode]

    left = Text()
    left.append(f"⏵⏵ {label}", style=f"bold {style}")

    right = Text()
    if model:
        # 会话累计计费 token（每轮输入/输出分别累加，F8）+ 模型名
        right.append(
            f"{model} ● ↑{_fmt_tokens(usage_in)} ↓{_fmt_tokens(usage_out)} tokens", style="dim"
        )
    else:
        right.append("请选择 provider", style="red")

    grid = Table.grid(expand=True)
    grid.add_column(justify="left", ratio=1)
    grid.add_column(justify="right", ratio=1)
    grid.add_row(left, right)
    return grid


def approval_block(req, cursor: int) -> RenderableType:
    """人在回路待批准块（F8）：工具名 + 参数 + 触发原因 + 三选菜单（光标高亮）。"""
    title = Text()
    title.append("● ", style="bold cyan")
    title.append(f"{req.name}({req.args})", style="bold")

    labels = ["允许本次", "永久允许（写入本地配置）", "拒绝本次"]
    menu = Text()
    for idx, label in enumerate(labels):
        prefix = "> " if idx == cursor else "  "
        style = "bold green" if idx == cursor else "dim"
        menu.append(f"{prefix}{idx + 1}. {label}", style=style)
        if idx < len(labels) - 1:
            menu.append("\n")

    return Group(
        title,
        Text(f"  {req.reason}", style="dim"),
        Text(""),
        Text("是否继续?", style="bold"),
        menu,
        Text("↑↓ 选择 · 回车确认 · Esc 取消", style="dim"),
    )


def user_block(text: str) -> RenderableType:
    """用户输入块（提交定型后追加到对话区）。"""
    return Group(Text(""), Text(f"> {text}", style="bold magenta"))


def assistant_block(reply: str, elapsed: float) -> RenderableType:
    """助手回复块：markdown 定型渲染 + 本轮总耗时。"""
    reply = "● " + reply
    body: RenderableType = Markdown(reply) if reply.strip() else Text("（空回复）", style="dim")
    return Group(
        Text(""),
        body,
        Text(f"⏱ {elapsed:.1f}s", style="dim"),
    )


def preamble_block(text: str) -> RenderableType:
    """工具调用前的助手文本（preamble）：markdown 定型，不带耗时行。"""
    if not text.strip():
        return Text("")
    return Markdown("● " + text)


def tool_line(name: str, args: str) -> RenderableType:
    """工具行：`● name(args)`，Claude Code 风格（F8/AC11）。"""
    line = Text()
    line.append("● ", style="bold cyan")
    line.append(f"{name}({args})", style="bold")
    return line


def tool_result_summary(result: str, is_error: bool) -> RenderableType:
    """工具结果摘要：每行 `⎿ ` 前缀，灰/红样式，界面截断 ~8 行（F8/N5）。"""
    style = "bold red" if is_error else "dim"
    if not result.strip():
        body = Text("  ⎿ （无输出）", style=style)
    else:
        lines = result.splitlines()
        shown = lines[:_RESULT_SUMMARY_LINES]
        if len(lines) > _RESULT_SUMMARY_LINES:
            shown = shown + ["…"]
        body = Text("\n".join(f"  ⎿ {ln}" for ln in shown), style=style)
    return body


def error_block(err: Exception) -> RenderableType:
    """错误块：可区分样式（红色），程序不退出。"""
    return Group(
        Text(""),
        Text(f"✗ {type(err).__name__}: {err}", style="bold red"),
    )


def notice_block(text: str) -> RenderableType:
    """系统提示块（灰色）：模式切换 / 停止原因 / 取消等，仅展示不入历史。"""
    return Group(
        Text(""),
        Text(text, style="dim italic"),
    )


def error_notice_block(msg: str) -> RenderableType:
    """错误提示块（红色）：命令路径的可读失败信息，仅展示不入历史。"""
    return Group(
        Text(""),
        Text(f"✗ {msg}", style="bold red"),
    )


def format_compact_notice(ev: CompactEvent) -> str:
    """把压缩事件渲染成系统消息文案（自动 / 紧急 / 手动三条路径共用同一格式）。"""
    if ev.phase is CompactPhase.BEFORE_AUTO:
        return "正在压缩上下文..."
    if ev.phase is CompactPhase.BEFORE_EMERGENCY:
        return "上下文撞墙，自动压缩中..."
    if ev.err is not None:
        return f"压缩失败：{ev.err}"
    return f"已压缩，token 从 {ev.before} 降至 {ev.after}"


def format_tool_args(raw: str) -> str:
    """把工具调用的原始参数压成一行并限宽（回放历史用，避免长参数撑爆 scrollback）。"""
    flat = " ".join(raw.split())
    if len(flat) <= _TOOL_ARGS_WIDTH:
        return flat
    return flat[: _TOOL_ARGS_WIDTH - 1] + "…"


def history_blocks(msgs: list[Message]) -> list[RenderableType]:
    """把恢复出来的历史消息渲染成回放块（/resume 进场时重绘对话区）。

    与实时渲染（stream.py）保持同一视觉语言：用户块、助手正文、工具行 + 结果摘要。
    助手回合在历史中以「preamble（带 tool_calls）」与「最终答复」两种形态出现，
    这里如实回放：带工具调用的回合只出正文 + 工具行，不带耗时行（耗时是实时信息，
    历史存档里没有）。
    """
    blocks: list[RenderableType] = []
    for msg in msgs:
        if msg.role == ROLE_USER:
            blocks.append(user_block(msg.content))
        elif msg.role == ROLE_ASSISTANT:
            if msg.content.strip():
                blocks.append(preamble_block(msg.content))
            for call in msg.tool_calls:
                blocks.append(tool_line(call.name, format_tool_args(str(call.input))))
        elif msg.role == ROLE_TOOL:
            for result in msg.tool_results:
                blocks.append(tool_result_summary(result.content, result.is_error))
    return blocks


def write_notice(app, text: str) -> None:
    """把一条系统消息追加到 scrollback（仅视图层，不进对话历史）。

    命令路径、会话恢复、取消提示共用同一出口，文案样式保持一致。
    """
    from textual.widgets import RichLog

    app.query_one("#log", RichLog).write(notice_block(text))
