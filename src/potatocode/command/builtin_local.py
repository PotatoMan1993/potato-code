"""5 条纯本地命令：/help /status /memory /permission /session（ch12 F9/F18-F22）。

只通过 UI 输出信息，不改 App 状态、不进对话历史、不消耗 token；任何会话状态下
都可执行（N3a）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .registry import Registry
from .ui import UI

# /status 六行的 key 顺序固定（F19/N8），列宽取最长 key 对齐
_STATUS_KEYS = ("Mode:", "Tokens:", "Tools:", "Memories:", "Model:", "Directory:")


def make_help_handler(reg: Registry) -> Callable[[UI], Awaitable[None]]:
    """/help 的 handler 工厂：闭包捕获注册中心，输出按字典序的两列对齐清单（F18/N7）。"""

    async def _handler(ui: UI) -> None:
        cmds = reg.visible()
        if not cmds:
            ui.println("没有已注册的命令")
            return
        width = max(len(c.name) for c in cmds)
        lines = [f"/{c.name.ljust(width)}  {c.description}" for c in cmds]
        ui.println("\n".join(lines))

    return _handler


async def handle_status(ui: UI) -> None:
    """/status：六行 key:value，渲染顺序与 F19 列举顺序一致（N8）。"""
    rows = [
        ("Mode:", str(ui.current_mode())),
        ("Tokens:", f"{ui.tokens_in()} in / {ui.tokens_out()} out"),
        ("Tools:", f"{ui.tool_count()} enabled"),
        ("Memories:", f"{len(ui.memory_files())} files"),
        ("Model:", ui.model_name()),
        ("Directory:", ui.cwd()),
    ]
    width = max(len(k) for k in _STATUS_KEYS)
    body = "\n".join(f"{k.ljust(width)}  {v}" for k, v in rows)
    ui.println(f"potatocode Status\n\n{body}")


async def handle_memory(ui: UI) -> None:
    """/memory：列出已加载的记忆文件名，不展开内容（F20）。"""
    files = ui.memory_files()
    if not files:
        ui.println("无已加载的记忆文件")
        return
    ui.println("\n".join(files))


async def handle_permission(ui: UI) -> None:
    """/permission：输出当前权限模式名（与 /status 的 Mode 值同形，F21）。"""
    ui.println(str(ui.current_mode()))


async def handle_session(ui: UI) -> None:
    """/session：输出当前会话标识与存档路径（F22）。"""
    ui.println(f"Session: {ui.session_id()}\nPath: {ui.session_path()}")
