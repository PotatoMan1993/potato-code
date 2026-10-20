"""permission 包：五层防御的权限系统。

前四层（黑名单 / 沙箱 / 规则 / 模式兜底）由 `Engine.check` 承载；
第五层人在回路由 agent 编排驱动（Ask → ApprovalRequest → Outcome）。
对外导出枚举、引擎构造与规则持久化入口。
"""

from __future__ import annotations

from .engine import Engine, mode_fallback, new_engine
from .matcher import (
    ExactMatcher,
    GlobMatcher,
    Matcher,
    NotMatcher,
    RegexMatcher,
    compile_matcher,
)
from .persist import ApprovalError, persist_local_allow
from .types import (
    Category,
    Decision,
    Mode,
    Outcome,
    outcome_for_index,
    parse_mode,
)

__all__ = [
    "ApprovalError",
    "Category",
    "Decision",
    "Engine",
    "ExactMatcher",
    "GlobMatcher",
    "Matcher",
    "Mode",
    "NotMatcher",
    "Outcome",
    "RegexMatcher",
    "compile_matcher",
    "mode_fallback",
    "new_engine",
    "outcome_for_index",
    "parse_mode",
    "persist_local_allow",
]
