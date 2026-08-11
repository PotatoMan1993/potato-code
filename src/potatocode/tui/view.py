"""TUI 视图层：各状态的渲染拼装——状态栏、用户块、markdown 定型、错误样式。"""

from __future__ import annotations

from enum import Enum

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text


class SessionState(Enum):
    """会话状态机。"""

    SELECTING = "selecting"  # 多 provider 时的选择界面
    IDLE = "idle"  # 等待用户输入
    STREAMING = "streaming"  # 等待/接收模型流（loading + 计时）


def statusbar_content(name: str | None, model: str | None) -> RenderableType:
    """底部状态栏：左侧 provider 名称、右侧模型名，两端对齐。"""
    grid = Table.grid(expand=True)
    grid.add_column(justify="left", ratio=1)
    grid.add_column(justify="right", ratio=1)
    grid.add_row(
        Text(name or "PotatoCode", style="bold cyan"),
        Text(model or "请选择 provider", style="dim"),
    )
    return grid


def user_block(text: str) -> RenderableType:
    """用户输入块（提交定型后追加到对话区）。"""
    return Group(Text(""), Text(f"● {text}", style="bold green"))


def assistant_block(reply: str, elapsed: float) -> RenderableType:
    """助手回复块：markdown 定型渲染 + 本轮总耗时。"""
    body: RenderableType = Markdown(reply) if reply.strip() else Text("（空回复）", style="dim")
    return Group(
        Text(""),
        Text("●", style="bold magenta"),
        body,
        Text(f"⏱ {elapsed:.1f}s", style="dim"),
    )


def error_block(err: Exception) -> RenderableType:
    """错误块：可区分样式（红色），程序不退出。"""
    return Group(
        Text(""),
        Text(f"✗ {type(err).__name__}: {err}", style="bold red"),
    )
