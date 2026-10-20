"""Hook 生命周期事件的 11 个枚举成员与拦截类判定（ch14 F9/G1）。

事件名即 YAML 配置里的字面量（`class Event(str, enum.Enum)` 让
`Event("SessionStart")` 反查直接可用）；拦截类事件（PreToolUse / UserPromptSubmit）
的动作可通过约定信号表达拦截，其余事件只做副作用。
"""

from __future__ import annotations

import enum


class Event(str, enum.Enum):  # noqa: UP042 - plan 技术决策：str+Enum（YAML 字面量直接对应）
    """Agent 生命周期的 11 个固定挂钩时刻（ch14 F9），值即 YAML 事件字面量。"""

    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    SESSION_RESUME = "SessionResume"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    STOP = "Stop"
    PRE_USER_MESSAGE = "PreUserMessage"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    NOTIFICATION = "Notification"


# 拦截类事件：hook 动作可用约定信号（shell exit 2 / http decision=block）表达拦截
BLOCKING_EVENTS: frozenset[Event] = frozenset({Event.PRE_TOOL_USE, Event.USER_PROMPT_SUBMIT})


def is_blocking(event: Event) -> bool:
    """事件是否为拦截类（ch14 G8：拦截类不允许 async hook）。"""
    return event in BLOCKING_EVENTS


def parse_event(s: str) -> Event | None:
    """把 YAML 里的事件字面量解析为 Event；未知事件返回 None（ch14 N1 不抛异常）。"""
    try:
        return Event(s)
    except ValueError:
        return None


__all__ = ["BLOCKING_EVENTS", "Event", "is_blocking", "parse_event"]
