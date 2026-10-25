"""Agent 定义文件解析：frontmatter 分离、字段校验与降级（ch15 F4/F7/N4）。

与 skills/parser.py 的 frontmatter 分离逻辑同构但独立实现（plan 技术决策：避免
抽公共包导致两包耦合，且两者字段语义不同）。校验策略分两档：

- 必填项缺失（name / description）、frontmatter 结构错误 → 抛 `AgentParseError`，
  由上层决定跳过该文件还是终止启动（内置来源必须 fail-fast，N4）；
- 取值非法（未知 model / 未知 permissionMode）→ stderr 警告并降级为默认值，
  文件仍然可用（AC22）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

from .definition import Definition, Source

# 角色名合法性：大小写字母开头，仅含字母 / 数字 / 连字符 / 下划线，1-32 字符（F4）
AGENT_NAME_REGEX = re.compile(r"^[A-Za-z][A-Za-z0-9\-_]{0,31}$")

# model 合法取值（F4）；未知取值警告后降级为 inherit（AC22）
VALID_MODELS: frozenset[str] = frozenset({"inherit", "haiku", "sonnet", "opus"})

# frontmatter 开闭定界符（必须独占一行，与 skills 约定一致）
FRONTMATTER_DELIMITER = "---"


class AgentParseError(Exception):
    """Agent 定义文件解析失败：frontmatter 缺失 / 未闭合 / YAML 非法 / 必填项缺失。"""


def parse_frontmatter_and_body(raw: str) -> tuple[dict, str]:
    """把定义文件原文拆成 (frontmatter 字典, 正文)。

    首行必须是独占一行的 `---`，到下一个独占行的 `---` 之间是 YAML frontmatter，
    其余是正文（子 Agent 系统提示）。结构错误一律抛 `AgentParseError`。
    """
    lines = raw.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        raise AgentParseError("frontmatter 缺失：文件必须以独占一行的 '---' 开头")
    closing_line_index = -1
    for index in range(1, len(lines)):
        if lines[index].strip() == FRONTMATTER_DELIMITER:
            closing_line_index = index
            break
    if closing_line_index < 0:
        raise AgentParseError("frontmatter 未闭合：缺少收尾的 '---' 行")
    frontmatter_text = "\n".join(lines[1:closing_line_index])
    body = "\n".join(lines[closing_line_index + 1 :]).strip("\n")
    try:
        meta = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as yaml_error:
        raise AgentParseError(f"frontmatter YAML 解析失败: {yaml_error}") from yaml_error
    if not isinstance(meta, dict):
        raise AgentParseError("frontmatter 顶层必须是键值映射（dict）")
    return meta, body


def _warn_invalid_value(field: str, value: object, fallback: str, file_path: str) -> None:
    """字段取值非法的 stderr 警告（降级不阻断，AC22）。"""
    print(
        f"subagent {file_path}: unknown {field} {value!r}, defaulting to {fallback}",
        file=sys.stderr,
    )


def parse_definition(data: bytes, file_path: str, source: Source) -> Definition:
    """把定义文件字节解析为 Definition（F4）。

    必填项缺失抛 `AgentParseError`；model / permissionMode 取值非法时 stderr
    警告并降级（model→inherit、permissionMode→default，AC22）。
    """
    meta, body = parse_frontmatter_and_body(data.decode("utf-8-sig"))

    raw_name = meta.get("name")
    name = "" if raw_name is None else str(raw_name).strip()
    if not name or not AGENT_NAME_REGEX.fullmatch(name):
        raise AgentParseError(
            f"name 非法（需字母开头，仅含字母/数字/连字符/下划线，1-32 字符）: {name!r}"
        )
    raw_description = meta.get("description")
    description = "" if raw_description is None else str(raw_description).strip()
    if not description:
        raise AgentParseError("frontmatter 缺少 description 字段或其不是非空字符串")

    tools = [str(t) for t in (meta.get("tools") or [])]
    disallowed_tools = [str(t) for t in (meta.get("disallowedTools") or [])]

    model = str(meta.get("model") or "inherit").strip()
    if model not in VALID_MODELS:
        _warn_invalid_value("model", model, "inherit", file_path)
        model = "inherit"

    try:
        max_turns = int(meta.get("maxTurns") or 0)
    except (TypeError, ValueError):
        _warn_invalid_value("maxTurns", meta.get("maxTurns"), "0", file_path)
        max_turns = 0

    # permissionMode：dontAsk 是子 Agent 专属档（单独识别），其余复用四档 parse_mode
    from ..permission import Mode, parse_mode

    permission_mode_str = str(meta.get("permissionMode") or "").strip()
    dont_ask = False
    permission_mode = Mode.DEFAULT
    if permission_mode_str:
        if permission_mode_str == "dontAsk":
            dont_ask = True
        else:
            mode, ok = parse_mode(permission_mode_str)
            if ok:
                permission_mode = mode
            else:
                _warn_invalid_value("permissionMode", permission_mode_str, "default", file_path)

    background = bool(meta.get("background") or False)

    return Definition(
        name=name,
        description=description,
        tools=tools,
        disallowed_tools=disallowed_tools,
        model=model,
        max_turns=max_turns,
        permission_mode=permission_mode,
        dont_ask=dont_ask,
        background=background,
        system_prompt=body,
        file_path=file_path,
        source=source,
    )


def parse_file(path: Path | str, source: Source) -> Definition:
    """从磁盘读取并解析一个 Agent 定义文件。读盘失败 / 解析失败抛 `AgentParseError`。"""
    file_path = Path(path)
    try:
        data = file_path.read_bytes()
    except OSError as os_error:
        raise AgentParseError(f"读取定义文件失败: {file_path}: {os_error}") from os_error
    return parse_definition(data, str(file_path), source)


__all__ = [
    "AGENT_NAME_REGEX",
    "VALID_MODELS",
    "AgentParseError",
    "parse_definition",
    "parse_file",
    "parse_frontmatter_and_body",
]
