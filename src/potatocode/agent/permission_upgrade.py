"""子 Agent 审批升级回调类型（ch15 F12-③/F13）。

子 Agent 工具调用走到 Ask 时，把 ApprovalRequest 交给 `approval_upgrader` 升级
到父 TUI 弹窗；返回 `(Outcome, ok)`——ok=True 表示升级方已拿到用户裁决，
ok=False 表示升级失败（无消费者 / 队列满），调用方回退到默认 emit 路径。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ..permission import Outcome
from . import ApprovalRequest

# 升级回调：入参为待批准请求（respond Future 由调用方创建并持有）
ApprovalUpgrader = Callable[[ApprovalRequest], Awaitable[tuple[Outcome, bool]]]

__all__ = ["ApprovalUpgrader"]
