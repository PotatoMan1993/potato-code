"""compact：上下文管理的唯一权威入口（两层压缩 + 压缩后恢复）。

对外只暴露 `manage_context` 等窄接口；Agent 主循环与 TUI 通过它们驱动压缩，
不直接触碰内部子模块的实现细节。
"""

from .compact import (
    ManageInput,
    ManageOutput,
    TriggerKind,
    auto_threshold,
    manage_context,
)
from .const import (
    AUTO_SAFETY_MARGIN,
    MANUAL_SAFETY_MARGIN,
    SUMMARY_RESERVE,
)
from .recovery import BOUNDARY_NOTICE, build_recovery_attachment
from .state import (
    CompactCircuitBreaker,
    ContentReplacementState,
    FileReadRecord,
    RecoveryState,
    SessionContext,
    new_session_context,
)
from .token import estimate_tokens, message_chars, usage_anchor

__all__ = [
    "AUTO_SAFETY_MARGIN",
    "BOUNDARY_NOTICE",
    "MANUAL_SAFETY_MARGIN",
    "SUMMARY_RESERVE",
    "CompactCircuitBreaker",
    "ContentReplacementState",
    "FileReadRecord",
    "ManageInput",
    "ManageOutput",
    "RecoveryState",
    "SessionContext",
    "TriggerKind",
    "auto_threshold",
    "build_recovery_attachment",
    "estimate_tokens",
    "manage_context",
    "message_chars",
    "new_session_context",
    "usage_anchor",
]
