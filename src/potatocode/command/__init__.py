"""命令系统：注册中心、分发解析、UI 抽象与 12 条内置命令（ch12）。

命令包是纯领域逻辑，不依赖 Textual；TUI 通过 `CommandMixin` 实现 `UI` 协议，
把分发结果桥接回 App。
"""

from __future__ import annotations

from .builtins import register_builtins
from .command import Command, Handler, Kind
from .dispatch import parse
from .registry import Registry
from .ui import UI, NopUI

__all__ = [
    "Command",
    "Handler",
    "Kind",
    "NopUI",
    "Registry",
    "UI",
    "parse",
    "register_builtins",
]
