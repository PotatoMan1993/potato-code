"""人在回路「永久放行」的规则生成与本地层持久化（F8）。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ..llm import ToolCall
from .matcher import GlobMatcher
from .rule import Rule
from .sandbox import relpath_for
from .settings import SettingsError, extract_target, friendly_name, load_settings

if TYPE_CHECKING:
    from .engine import Engine


class ApprovalError(Exception):
    """人在回路 / 规则持久化相关错误。"""


def escape_glob(s: str) -> str:
    """转义命令串里的 glob 元字符（`*`、`\\`），防止生成精确规则时被泛化匹配。"""
    return s.replace("\\", "\\\\").replace("*", "\\*")


def rule_for(engine: Engine, call: ToolCall) -> tuple[Rule, str, bool]:
    """据一次调用生成**精确**规则（内存 Rule + YAML 串），不做泛化。

    bash → `Bash(<escaped command>)`；文件类 → `Write/Read(...)(<relpath>)`；
    解析失败 / 未知 → (Rule("","",False), "", False)。
    ch14：内存 Rule 改持结构化 matcher——bash 串经 escape_glob 后按命令整串
    glob（与写盘串重新解析的语义一致），文件路径按路径分段 glob。
    """
    friendly = friendly_name(call.name)
    target, is_file, ok = extract_target(call)
    if not ok or not target:
        return Rule("", None, False, ""), "", False

    if is_file:
        rel = relpath_for(engine.root, target)
        if not rel:
            return Rule("", None, False, ""), "", False
        rule = Rule(
            tool=friendly, matcher=GlobMatcher(rel, is_command=False), allow=True, raw=rel
        )
        return rule, f"{friendly}({rel})", True

    escaped = escape_glob(target)
    rule = Rule(
        tool=friendly,
        matcher=GlobMatcher(escaped, is_command=True),
        allow=True,
        raw=escaped,
    )
    return rule, f"{friendly}({escaped})", True


def persist_local_allow(engine: Engine, call: ToolCall) -> None:
    """把精确 allow 规则写入本地层配置并同步内存（F8，跨会话生效）。

    异常向上抛，由调用方（agent）捕获后只记日志不阻断执行。
    """
    rule, text, ok = rule_for(engine, call)
    if not ok:
        raise ApprovalError(f"无法为调用生成规则: {call.name}")

    try:
        settings = load_settings(engine.local_path)
    except SettingsError as e:
        raise ApprovalError(f"读取本地配置失败: {e}") from e

    if text not in settings.permissions.allow:
        settings.permissions.allow.append(text)
        engine.local.allow.append(rule)  # 同步内存

    d: dict = {}
    if settings.default_mode:
        d["default_mode"] = settings.default_mode
    d["permissions"] = {"allow": settings.permissions.allow, "deny": settings.permissions.deny}

    p = Path(engine.local_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")


__all__ = ["ApprovalError", "escape_glob", "persist_local_allow", "rule_for"]
