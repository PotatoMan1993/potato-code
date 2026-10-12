"""命令系统基础类型：执行类型枚举、命令元数据与处理函数签名（ch12 F1/F8）。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ui import UI  # 仅类型引用：Handler 签名依赖 UI 协议（T4 声明）


class Kind(Enum):
    """命令执行类型（F8）：三类行为由 handler 显式表达，Kind 只是元数据。"""

    LOCAL = "local"  # 纯本地：只输出信息，不改 App 状态、不进对话历史
    UI = "ui"  # 影响界面：改运行时状态 / 换会话 / 退出进程，不进对话历史
    PROMPT = "prompt"  # 提示词：注入 user 消息并触发一轮 LLM 回合，进对话历史


# 处理函数签名：只依赖 UI 抽象，不持有 TUI 具体类型（F33/F35）
Handler = Callable[["UI"], Awaitable[None]]


@dataclass(slots=True)
class Command:
    """一条内置命令的完整元数据（F1）。"""

    name: str  # 不带 "/" 前缀，全小写，全局唯一
    description: str  # 一句话描述，/help 与补全菜单共用
    kind: Kind
    handler: Handler
    aliases: list[str] = field(default_factory=list)  # 不带 "/" 前缀，全小写，全局唯一
    hidden: bool = False  # True 时 /help 与补全菜单都不显示，但 dispatcher 仍可命中（F28）
