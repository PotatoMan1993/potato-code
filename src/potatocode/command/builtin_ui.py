"""5 条影响界面命令：/exit /plan /compact /resume /clear（ch12 F10/F12-F17）。

不向对话历史追加 user 消息；idle 守护在 dispatch_slash 按 Kind 统一做，
handler 内对 /compact /resume 再挡一次（直接调用时的兜底）。
"""

from __future__ import annotations

from ..permission import Mode
from .ui import UI


async def handle_exit(ui: UI) -> None:
    """/exit：通知 UI 安全退出（取消进行中的回合与后台任务后再退出，N12）。"""
    ui.quit()


async def handle_plan(ui: UI) -> None:
    """/plan：切到计划模式（仅只读工具）。"""
    ui.set_mode(Mode.PLAN)
    ui.println("已切换到 PLAN 模式")


async def handle_compact(ui: UI) -> None:
    """/compact：手动触发一次上下文压缩。"""
    if not ui.idle():
        ui.error("请等待当前任务完成")
        return
    ui.force_compact()


async def handle_resume(ui: UI) -> None:
    """/resume：打开历史会话列表（仅 idle 可用，沿用 ch09 约束）。"""
    if not ui.idle():
        ui.error("请等待当前任务完成")
        return
    ui.open_resume_menu()


async def handle_clear(ui: UI) -> None:
    """/clear：结束当前会话、开启新会话（F17）。

    inline skill 的 SOP 现在随对话历史进会话存档，开新会话自然全部丢弃，
    不再需要单独清理已激活 skill 的状态。
    ch14：clear_and_new_session 改为 async，内部串接 SessionEnd/SessionStart 派发。
    """
    await ui.clear_and_new_session()
    ui.println("已清空当前会话，开启新会话")
