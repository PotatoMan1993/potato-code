"""上下文压缩的生命周期事件：TUI 据此在摘要进行中即时给出状态提示。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CompactPhase(Enum):
    """一次压缩所处的阶段：开始前 / 结束后，自动与紧急各一对。"""

    BEFORE_AUTO = "before_auto"
    AFTER_AUTO = "after_auto"
    BEFORE_EMERGENCY = "before_emergency"
    AFTER_EMERGENCY = "after_emergency"


@dataclass
class CompactEvent:
    """一次压缩的状态事件：BEFORE 阶段只有 phase，AFTER 阶段带前后 token 与错误。"""

    phase: CompactPhase
    before: int = 0  # 压缩前估算 token（BEFORE 阶段为 0）
    after: int = 0  # 压缩后估算 token（BEFORE 阶段为 0）
    err: Exception | None = None  # AFTER 阶段：压缩失败时的异常
