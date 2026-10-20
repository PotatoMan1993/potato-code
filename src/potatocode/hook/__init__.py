"""hook 包：Agent 生命周期挂钩系统（ch14 G1-G9）。

对外暴露事件枚举、数据结构与加载入口；引擎分派、动作执行细节见各子模块。
hook 依赖 permission.Matcher 做条件匹配，permission 不反向依赖本包（无循环）。
"""

from __future__ import annotations

from .engine import DispatchResult, Engine
from .event import BLOCKING_EVENTS, Event, is_blocking, parse_event
from .loader import load
from .rule import Action, ActionType, Condition, Payload, Rule

__all__ = [
    "BLOCKING_EVENTS",
    "Action",
    "ActionType",
    "Condition",
    "DispatchResult",
    "Engine",
    "Event",
    "Payload",
    "Rule",
    "is_blocking",
    "load",
    "parse_event",
]
