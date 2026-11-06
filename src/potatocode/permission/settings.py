"""配置加载、友好名/类别映射与目标提取（F4/F5/N7；ch14 T4 失败规则 stderr 报告）。"""

from __future__ import annotations

import json
import sys
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from ..llm import ToolCall
from .rule import RuleSet, parse_rule
from .types import Category

logger = logging.getLogger(__name__)


class SettingsError(Exception):
    """权限配置文件格式非法（YAML 解析失败或结构不对）。"""


@dataclass
class PermissionsBlock:
    """单份配置里的 allow / deny 规则串列表。"""

    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)


@dataclass
class Settings:
    """单个 YAML 文件的结构：可选 default_mode + permissions（F4）。"""

    default_mode: str = ""
    permissions: PermissionsBlock = field(default_factory=PermissionsBlock)


# 内部工具名 → 友好名（F3/AC4）
_FRIENDLY: dict[str, str] = {
    "bash": "Bash",
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Edit",
    "glob": "Glob",
    "grep": "Grep",
}

# 只读工具名集合（categorize 的兜底依据）
_READ_TOOLS = {"read_file", "glob", "grep"}

# 文件写工具名集合
_WRITE_TOOLS = {"write_file", "edit_file"}

# 文件类工具（读/写/改/找/搜）——沙箱围栏的对象
_FILE_TOOLS = {"read_file", "write_file", "edit_file", "glob", "grep"}


def friendly_name(internal: str) -> str:
    """内部工具名映射为友好名；未知原样返回。"""
    return _FRIENDLY.get(internal, internal)


def categorize(internal: str, read_only: bool) -> Category:
    """把工具调用归入 READ / WRITE / EXEC 三类（N7 最严）。

    read_only（或名字本身只读）→ READ；文件写 → WRITE；其余（含 bash、未知工具）→ EXEC。
    """
    if read_only or internal in _READ_TOOLS:
        return Category.READ
    if internal in _WRITE_TOOLS:
        return Category.WRITE
    return Category.EXEC


def _parse_input(call: ToolCall) -> dict | None:
    """把 call.input（JSON 字符串）解析为 dict；失败返回 None。"""
    try:
        data = json.loads(call.input) if call.input else {}
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def extract_target(call: ToolCall) -> tuple[str, bool, bool]:
    """提取权限判定所需的目标：返回 (target, is_file, ok)。

    - 文件类（read/write/edit）取 `path`（缺或空 → ok=False）；
    - glob/grep 取 `path` 作为搜索根（空 → "."；pattern/glob 字段不参与沙箱）；
    - bash 取 `command`（is_file=False）；未知工具 → ("", False, False)。
    ok=False 表示 JSON 无法解析或缺必填字段（N7 安全拒绝/兜底）。
    """
    name = call.name
    data = _parse_input(call)
    if data is None:
        # JSON 无法解析：文件类工具按 is_file=True 但 ok=False 交给沙箱判 Deny；
        # bash/未知工具 is_file=False，落到规则→模式兜底（绝不静默放行）。
        return "", name in _FILE_TOOLS, False

    if name in _FILE_TOOLS - {"glob", "grep"}:
        # 读/写/改：path 必填
        path = data.get("path")
        if not isinstance(path, str) or not path.strip():
            return "", True, False
        return path, True, True

    if name in ("glob", "grep"):
        # 找/搜：path 为搜索根，可选（空 → "."）
        path = data.get("path")
        if path is None or not isinstance(path, str) or not path.strip():
            path = "."
        return path, True, True

    if name == "bash":
        command = data.get("command")
        if not isinstance(command, str):
            return "", False, False
        return command, False, True

    # 未知工具
    return "", False, False


def load_settings(path: str) -> Settings:
    """读取单个配置文件：文件不存在→空 Settings；YAML/结构非法→抛 SettingsError（N5）。"""
    p = Path(path)
    if not p.is_file():
        return Settings()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as e:
        raise SettingsError(f"配置文件 YAML 格式错误: {path}: {e}") from e
    if raw is None:
        return Settings()
    if not isinstance(raw, dict):
        raise SettingsError(f"配置文件格式错误（应为映射）: {path}")

    default_mode = raw.get("default_mode") or ""
    if not isinstance(default_mode, str):
        default_mode = ""

    perms = raw.get("permissions") or {}
    if not isinstance(perms, dict):
        raise SettingsError(f"配置文件 permissions 应为映射: {path}")
    allow = perms.get("allow") or []
    deny = perms.get("deny") or []
    if not isinstance(allow, list) or not isinstance(deny, list):
        raise SettingsError(f"配置文件 permissions.allow/deny 应为列表: {path}")

    return Settings(
        default_mode=default_mode,
        permissions=PermissionsBlock(
            allow=[str(x) for x in allow],
            deny=[str(x) for x in deny],
        ),
    )


def to_rule_set(s: Settings) -> RuleSet:
    """把 Settings 转成 RuleSet：allow/deny 各条 parse_rule。

    ch14 F4：解析失败的规则改为 stderr 打印规则原文与原因后跳过（不再静默），
    其余规则正常加载、互不影响。
    """
    rs = RuleSet()
    for text in s.permissions.allow:
        rule, err = parse_rule(text)
        if rule is None:
            logger.error(f'rule {text!r} parse failed: {err}', file=sys.stderr)
            continue
        rs.allow.append(rule)
    for text in s.permissions.deny:
        rule, err = parse_rule(text)
        if rule is None:
            logger.error(f'rule {text!r} parse failed: {err}', file=sys.stderr)
            continue
        rs.deny.append(replace(rule, allow=False))
    return rs


__all__ = [
    "PermissionsBlock",
    "Settings",
    "SettingsError",
    "categorize",
    "extract_target",
    "friendly_name",
    "load_settings",
    "to_rule_set",
]
