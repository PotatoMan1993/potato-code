"""task 包：SubAgent 后台任务管理与操控工具（ch15）。

对外导出 Manager / BackgroundTask / Status / PartialState 与 4 个内置工具。
本包不反向依赖 tui；对 agent 仅类型引用（TYPE_CHECKING）与实例方法调用，
避免 agent_tool ↔ task 循环导入。
"""

from __future__ import annotations

from .manager import (
    BackgroundTask,
    Manager,
    PartialState,
    Status,
    TaskBusy,
    TaskError,
    TaskNotFound,
)
from .tools import SendMessageTool, TaskGetTool, TaskListTool, TaskStopTool

__all__ = [
    "BackgroundTask",
    "Manager",
    "PartialState",
    "SendMessageTool",
    "Status",
    "TaskBusy",
    "TaskError",
    "TaskGetTool",
    "TaskListTool",
    "TaskNotFound",
    "TaskStopTool",
]
