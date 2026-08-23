"""TUI 视图层：各状态的渲染拼装——状态栏、用户块、markdown 定型、工具行、错误样式。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

# 结果摘要在界面上的行数上限（N5：与回灌给模型的截断相互独立）
_RESULT_SUMMARY_LINES = 8


class SessionState(Enum):
    """会话状态机。"""

    SELECTING = "selecting"  # 多 provider 时的选择界面
    IDLE = "idle"  # 等待用户输入
    STREAMING = "streaming"  # 等待/接收模型流（loading + 计时）


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
    name: str | None,
    model: str | None,
    plan_mode: bool = False,
    usage_in: int = 0,
    usage_out: int = 0,
) -> RenderableType:
    """底部状态栏：左侧 provider 名（计划模式附 PLAN 徽标）、右侧累计用量 + 模型名。"""
    left = Text()
    if plan_mode:
        left.append("⏵⏵ plan mode on", style="bold green")
    else:
        left.append("⏵⏵ accept edits on", style="bold green")
    if model:
        left.append(f" ● {model}", style="cyan")
    else:
        left.append(" ● 请选择 provider", style="red")
    left.append(f" ● {name or 'PotatoCode'}", style="dim")

    right = Text()
    if model:
        # 会话累计计费 token（每轮输入/输出分别累加，F8）
        right.append(f"↑{_fmt_tokens(usage_in)} ↓{_fmt_tokens(usage_out)} tokens", style="dim")
    grid = Table.grid(expand=True)
    grid.add_column(justify="left", ratio=1)
    grid.add_column(justify="right", ratio=1)
    grid.add_row(left, right)
    return grid


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
