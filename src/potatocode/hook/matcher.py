"""Hook 条件求值：payload 字段路径取值 + 原子条件组合判定（ch14 F11-F15）。

字段路径用 `.` 分隔嵌套（如 `tool_input.command`）；路径不存在按空串处理不报错
（ch14 F13）。匹配器实例在加载期构造、这里只做运行期求值（ch14 F15）。
"""

from __future__ import annotations

import json
from typing import Any

from ..permission.matcher import Matcher
from .rule import AtomCondition, CombineMode, Condition, Payload


def get_by_path(payload: Payload, path: str) -> str:
    """按 `.` 分隔的字段路径从 payload 取值并转为字符串（ch14 F13）。

    路径不存在、中途遇 None 或非 dict 一律返回空串（不报错）；bool/int/float 用
    `str(value)`（True -> "True"，与 N6 的 JSON 输出形态一致）；嵌套 dict/list 转
    `json.dumps(..., sort_keys=True)` 保持稳定形态。
    """
    current: Any = payload
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return ""
        current = current[segment]
        if current is None:
            return ""
    if isinstance(current, str):
        return current
    if isinstance(current, bool | int | float):
        return str(current)
    if isinstance(current, dict | list):
        return json.dumps(current, sort_keys=True)
    return str(current)


def eval_condition(condition: Condition | None, payload: Payload) -> bool:
    """求值一条 hook 条件：None 表示无条件触发（ch14 G3/F11）。

    all_of 要求全部原子条件为 True；any_of 要求至少一个为 True。
    """
    if condition is None:
        return True
    atom_results = (_atom_matches(atom, payload) for atom in condition.atoms)
    if condition.mode is CombineMode.ALL_OF:
        return all(atom_results)
    return any(atom_results)


def _atom_matches(atom: AtomCondition, payload: Payload) -> bool:
    """单个原子条件判定：字段取值后交给加载期构造好的 matcher。"""
    matcher: Matcher = atom.matcher
    return matcher.match(get_by_path(payload, atom.field))


__all__ = ["eval_condition", "get_by_path"]
