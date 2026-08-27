"""permission 基础类型：权限模式、判定值、工具类别与人在回路三选一。

这些枚举被 permission 子包内各模块及 agent/tui 共用，故独立成文件，
避免 `__init__` 与子模块之间形成循环导入。
"""

from __future__ import annotations

from enum import IntEnum


class Mode(IntEnum):
    """权限模式四档（F5/F7）。按序循环：DEFAULT → ACCEPT_EDITS → PLAN → BYPASS。"""

    DEFAULT = 0  # 只读 Allow / 文件写 Ask / 命令执行 Ask
    ACCEPT_EDITS = 1  # 文件写 Allow / 命令执行 Ask
    PLAN = 2  # 仅只读工具可见（沿用 ch04）；矩阵同 default 作防御兜底
    BYPASS = 3  # 全 Allow（黑名单/沙箱仍拦）

    def __str__(self) -> str:
        return {
            Mode.DEFAULT: "default",
            Mode.ACCEPT_EDITS: "acceptEdits",
            Mode.PLAN: "plan",
            Mode.BYPASS: "bypassPermissions",
        }[self]


class Decision(IntEnum):
    """一次工具调用的判定中间值：Allow / Deny / Ask。"""

    ALLOW = 0
    DENY = 1
    ASK = 2


class Category(IntEnum):
    """工具按副作用分的三类，用于模式兜底矩阵（F5）。"""

    READ = 0  # 只读
    WRITE = 1  # 文件写
    EXEC = 2  # 命令执行


class Outcome(IntEnum):
    """人在回路三选一结果（F8）。"""

    DENY_ONCE = 0  # 拒绝本次
    ALLOW_ONCE = 1  # 允许本次（不留规则）
    ALLOW_FOREVER = 2  # 永久允许（+写本地层文件，精确匹配）


# 人在回路菜单索引 → Outcome 的固定映射（0/1/2 对应菜单三行，F8）
_OUTCOME_BY_INDEX: tuple[Outcome, ...] = (
    Outcome.ALLOW_ONCE,
    Outcome.ALLOW_FOREVER,
    Outcome.DENY_ONCE,
)


def outcome_for_index(index: int) -> Outcome:
    """把待批准菜单的索引（0/1/2）映射为 Outcome。"""
    return _OUTCOME_BY_INDEX[index]


def parse_mode(s: str) -> tuple[Mode, bool]:
    """大小写不敏感识别四档名；未知返回 (Mode.DEFAULT, False)。"""
    key = (s or "").strip().lower()
    mapping = {
        "default": Mode.DEFAULT,
        "acceptedits": Mode.ACCEPT_EDITS,
        "plan": Mode.PLAN,
        "bypasspermissions": Mode.BYPASS,
    }
    if key in mapping:
        return mapping[key], True
    return Mode.DEFAULT, False


__all__ = [
    "Category",
    "Decision",
    "Mode",
    "Outcome",
    "outcome_for_index",
    "parse_mode",
]
