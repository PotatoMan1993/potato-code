"""补充消息：system-reminder 标签包裹与规划模式按轮次提醒（F6/F7）。

补充指令以 `<system-reminder>` 标签包裹，注入消息通道而非持久历史，
让模型理解这是系统补充上下文而非用户提问，不针对它直接回复。
"""

from __future__ import annotations


def system_reminder(body: str) -> str:
    """用 `<system-reminder>` 标签包裹补充指令正文（F6/AC8）。"""
    return f"<system-reminder>\n{body}\n</system-reminder>"


# 规划模式完整提醒（自 ch04 的 PLAN_MODE_REMINDER 迁移，F7）
_PLAN_REMINDER_FULL: str = (
    "You are currently in PLAN MODE. You may use ONLY the read-only tools "
    "(read_file, glob, grep) to investigate the codebase. You must NOT write files, "
    "edit files, or run shell commands. Produce a clear, step-by-step plan for the task, "
    "then stop and wait for the user to approve it with /do before doing any work."
)

# 规划模式精简提醒（间隔轮次注入，F7）
_PLAN_REMINDER_CONCISE: str = (
    "Still in PLAN MODE: read-only tools only (read_file, glob, grep); "
    "do not modify files or run commands. Wait for /do approval."
)


def plan_reminder(full: bool) -> str:
    """返回包好标签的规划模式提醒：full=True 完整版、否则精简版（F7）。"""
    return system_reminder(_PLAN_REMINDER_FULL if full else _PLAN_REMINDER_CONCISE)


# /do 注入的用户消息——指示模型按上文已确认的计划开始执行（自 prompt/__init__ 迁移）。
EXECUTE_DIRECTIVE: str = "请按上面的计划开始执行。"
