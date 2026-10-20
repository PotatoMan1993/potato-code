"""内置命令一次性注册：12 条命令全部收编进注册中心（ch12 G1/F12-F23）。

显式注册（不靠 import 副作用），启动顺序明确、测试可用空 registry。
"""

from __future__ import annotations

from .builtin_local import (
    handle_memory,
    handle_permission,
    handle_session,
    handle_status,
    make_help_handler,
)
from .builtin_prompt import handle_do, handle_review
from .builtin_ui import handle_clear, handle_compact, handle_exit, handle_plan, handle_resume
from .command import Command, Kind
from .registry import Registry


def register_builtins(reg: Registry) -> None:
    """把内置命令按名字注册进 reg；/help 的 handler 工厂捕获 reg 自身。"""
    reg.register(
        Command(
            name="clear",
            description="清空当前会话并开启新会话",
            kind=Kind.UI,
            handler=handle_clear,
        )
    )
    reg.register(
        Command(
            name="compact",
            description="立即压缩当前上下文",
            kind=Kind.UI,
            handler=handle_compact,
        )
    )
    reg.register(
        Command(
            name="do",
            description="退出计划模式，按上面的计划开始执行",
            kind=Kind.PROMPT,
            handler=handle_do,
        )
    )
    reg.register(
        Command(
            name="exit",
            description="退出 PotatoCode",
            kind=Kind.UI,
            handler=handle_exit,
        )
    )
    reg.register(
        Command(
            name="help",
            description="显示所有可用命令",
            kind=Kind.LOCAL,
            handler=make_help_handler(reg),
        )
    )
    # ch14 T21：/hooks 列出已加载 hook（KindLocal，零参数，任何状态可执行）
    from ..tui.hooks import handle_hooks

    reg.register(
        Command(
            name="hooks",
            description="列出已加载的 hook 列表",
            kind=Kind.LOCAL,
            handler=handle_hooks,
        )
    )
    reg.register(
        Command(
            name="memory",
            description="查看已加载的记忆文件",
            kind=Kind.LOCAL,
            handler=handle_memory,
        )
    )
    reg.register(
        Command(
            name="permission",
            description="查看当前权限模式",
            kind=Kind.LOCAL,
            handler=handle_permission,
        )
    )
    reg.register(
        Command(
            name="plan",
            description="切换到计划模式（仅只读工具）",
            kind=Kind.UI,
            handler=handle_plan,
        )
    )
    reg.register(
        Command(
            name="resume",
            description="恢复历史会话",
            kind=Kind.UI,
            handler=handle_resume,
        )
    )
    reg.register(
        Command(
            name="review",
            description="请求 AI 审查当前代码变更",
            kind=Kind.PROMPT,
            handler=handle_review,
        )
    )
    reg.register(
        Command(
            name="session",
            description="查看当前会话信息",
            kind=Kind.LOCAL,
            handler=handle_session,
        )
    )
    reg.register(
        Command(
            name="status",
            description="查看当前状态概览",
            kind=Kind.LOCAL,
            handler=handle_status,
        )
    )
