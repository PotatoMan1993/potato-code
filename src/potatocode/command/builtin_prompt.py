"""2 条提示词命令：/do /review（ch12 F11/F14/F23）。

向对话历史追加一条固定文本的 user 消息并立即触发 LLM 回合；该消息与真实用户
消息走同一条追加与存档持久化路径（N3），对 LLM 不可区分。
"""

from __future__ import annotations

from ..permission import Mode
from ..prompt import EXECUTE_DIRECTIVE
from .ui import UI

# /review 注入的固定审查请求文案（不读 git diff、不收集外部上下文，F23）
REVIEW_DIRECTIVE: str = (
    "请审查当前上下文中的代码变更与已读取的文件，指出潜在 bug、可读性问题和可简化处。"
)


async def handle_do(ui: UI) -> None:
    """/do：先切回默认模式，再注入执行指令并立即开跑一轮。"""
    ui.set_mode(Mode.DEFAULT)
    ui.inject_and_send("/do", EXECUTE_DIRECTIVE)


async def handle_review(ui: UI) -> None:
    """/review：注入一条代码审查请求并立即触发回合。"""
    ui.inject_and_send("/review", REVIEW_DIRECTIVE)
