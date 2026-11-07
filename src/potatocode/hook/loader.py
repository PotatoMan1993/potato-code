"""Hook 配置加载器：扫描两层 YAML、字段校验、matcher 编译、合并（ch14 F6-F8/N1）。

所有加载错误一律 stderr 输出后继续（跳过出错规则 / 整个坏文件），绝不阻断
potatocode 启动（ch14 N1/N9）。两层（项目级 + 用户级）规则叠加合并，跨文件
同名 hook 冲突时保留先到者、跳过后到者（ch14 F7）。
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import yaml

from ..permission.matcher import NotMatcher, compile_matcher
from .engine import Engine
from .event import is_blocking, parse_event
from .rule import (
    Action,
    ActionType,
    AtomCondition,
    CombineMode,
    Condition,
    HttpAction,
    PromptAction,
    Rule,
    ShellAction,
    SubagentAction,
)

logger = logging.getLogger(__name__)


def _report_error(message: str) -> None:
    """加载期错误：同时写 session.log 与 stderr，调用方随后跳过该文件/规则（N1/N9）。"""
    logger.error(message)
    print(message, file=sys.stderr)


# 有效的 match.type 字面量（ch14 F14）
_MATCH_TYPES = {"exact", "glob", "regex", "not"}

# 时长串格式：`30s` / `5m` / `2h` / 纯数字秒（ch14 F8 timeout）
_DURATION_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)([smh]?)$")
_DURATION_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, "": 1.0}


def load(project_root: str | Path) -> Engine:
    """扫描项目级与用户级 hooks.yaml，构造规则全部合法的 Hook 引擎（ch14 F6-F8）。

    文件不存在跳过；存在但解析失败 / 顶层结构非法时 stderr 提示后整文件跳过
    （ch14 N9）。所有错误不抛异常，引擎始终可用。
    """
    project_root_path = Path(project_root)
    candidate_paths = [
        project_root_path / ".potatocode" / "hooks.yaml",
        Path.home() / ".potatocode" / "hooks.yaml",
    ]
    rules: list[Rule] = []
    sources: list[str] = []
    seen_names: set[str] = set()
    for hook_file in candidate_paths:
        if not hook_file.is_file():
            continue
        sources.append(str(hook_file))
        raw_rules = _read_hook_file(hook_file)
        if raw_rules is None:
            continue
        for idx, raw_rule in enumerate(raw_rules):
            compiled, err = _compile_rule(str(hook_file), idx, raw_rule)
            if compiled is None or err is not None:
                hook_name = (
                    raw_rule.get("name") if isinstance(raw_rule, dict) else None
                ) or f"#{idx}"
                _report_error(f'hook "{hook_name}": {err}, skipped')
                continue
            if compiled.name in seen_names:
                # 同名 hook 冲突：保留先到者、跳过后到者（ch14 F7）
                _report_error(
                    f'hook "{compiled.name}": duplicate name across files, '
                    f"skipped later rule from {compiled.source}"
                )
                continue
            seen_names.add(compiled.name)
            rules.append(compiled)
    return Engine(rules, sources)


def _read_hook_file(hook_file: Path) -> list | None:
    """读取单个 hooks.yaml，返回顶层 hooks 数组；坏文件 stderr 提示后返回 None（ch14 N9）。"""
    try:
        raw = yaml.safe_load(hook_file.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as e:
        _report_error(f"hooks.yaml parse failed: {hook_file}: {e}")
        return None
    if raw is None:
        return []
    if not isinstance(raw, dict) or not isinstance(raw.get("hooks"), list):
        _report_error(
            f"hooks.yaml structure invalid (expected top-level `hooks:` list): {hook_file}"
        )
        return None
    return raw["hooks"]


def _compile_rule(source: str, idx: int, raw: object) -> tuple[Rule | None, str | None]:
    """把单条 YAML hook 对象编译为 Rule；返回 (None, 错误描述) 表示校验失败。"""
    if not isinstance(raw, dict):
        return None, "hook entry must be a mapping"
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, "missing required field `name`"

    event_value = raw.get("event")
    if not isinstance(event_value, str):
        return None, f'hook "{name}": missing required field `event`'
    event = parse_event(event_value)
    if event is None:
        return None, f'hook "{name}": unknown event "{event_value}"'

    condition, cond_err = _compile_condition(name, raw.get("if"))
    if cond_err is not None:
        return None, f'hook "{name}": {cond_err}'

    action, action_err = _compile_action(name, raw.get("action"))
    if action_err is not None or action is None:
        return None, f'hook "{name}": {action_err or "missing action"}'

    only_once = raw.get("only_once", False)
    if not isinstance(only_once, bool):
        return None, f'hook "{name}": `only_once` must be a boolean'

    asyncio_mode = raw.get("async", False)
    if not isinstance(asyncio_mode, bool):
        return None, f'hook "{name}": `async` must be a boolean'
    if asyncio_mode and is_blocking(event):
        # 拦截类事件不允许 async——异步无法表达拦截信号（ch14 G8/F28）
        return None, f'hook "{name}": async not allowed for blocking events'

    timeout_raw = raw.get("timeout", "30s")
    timeout_s = _parse_duration(timeout_raw)
    if timeout_s is None:
        return None, f'hook "{name}": invalid timeout {timeout_raw!r} (expected like 30s / 5m / 2)'

    return (
        Rule(
            name=name,
            event=event,
            action=action,
            condition=condition,
            only_once=only_once,
            asyncio_mode=asyncio_mode,
            timeout_s=timeout_s,
            source=source,
        ),
        None,
    )


def _compile_condition(name: str, raw: object) -> tuple[Condition | None, str | None]:
    """编译 `if:` 条件对象；缺省返回 (None, None) 表示无条件（ch14 F11-F14）。"""
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, "`if` must be a mapping"
    has_all = "all_of" in raw
    has_any = "any_of" in raw
    if has_all and has_any:
        # 顶层混用两种组合按加载错误处理（ch14 F11/G5）
        return None, "`if` cannot contain both `all_of` and `any_of`"
    if not has_all and not has_any:
        return None, "`if` must contain exactly one of `all_of` / `any_of`"
    mode = CombineMode.ALL_OF if has_all else CombineMode.ANY_OF
    raw_atoms = raw.get("all_of" if has_all else "any_of")
    if not isinstance(raw_atoms, list) or not raw_atoms:
        return None, f"`if.{mode.value}` must be a non-empty list"

    atoms: list[AtomCondition] = []
    for raw_atom in raw_atoms:
        atom, atom_err = _compile_atom(raw_atom)
        if atom_err is not None:
            return None, f"`if.{mode.value}` atom invalid: {atom_err}"
        assert atom is not None
        atoms.append(atom)
    return Condition(mode=mode, atoms=atoms), None


def _compile_atom(raw_atom: object) -> tuple[AtomCondition | None, str | None]:
    """编译单个原子条件（field + match）（ch14 F12-F14）。"""
    if not isinstance(raw_atom, dict):
        return None, "atom must be a mapping with `field` and `match`"
    field_name = raw_atom.get("field")
    if not isinstance(field_name, str) or not field_name:
        return None, "atom missing `field`"
    raw_match = raw_atom.get("match")
    if not isinstance(raw_match, dict):
        return None, f"atom `{field_name}` missing `match` mapping"
    match_type = raw_match.get("type")
    if match_type not in _MATCH_TYPES:
        return None, f"atom `{field_name}` has unknown match type {match_type!r}"

    # hook 条件的 matcher 一律作用于 payload 字段值：glob 统一按路径分段语义
    # （is_command=False），想对 shell 串做整串通配应改用 regex（ch14 T8 决策）。
    is_command = False
    try:
        if match_type == "exact":
            matcher = compile_matcher(f"={raw_match.get('value', '')}", is_command=is_command)
        elif match_type == "glob":
            value = raw_match.get("value")
            if not isinstance(value, str) or not value:
                return None, f"atom `{field_name}` glob match requires non-empty `value`"
            matcher = compile_matcher(value, is_command=is_command)
        elif match_type == "regex":
            value = raw_match.get("value")
            if not isinstance(value, str):
                return None, f"atom `{field_name}` regex match requires string `value`"
            matcher = compile_matcher(f"~{value}", is_command=is_command)
        else:  # not：对内层原子条件取反，内层不支持再嵌 not（ch14 F14/N7）
            inner_raw = raw_match.get("inner")
            if not isinstance(inner_raw, dict):
                return None, f"atom `{field_name}` not-match requires `inner` mapping"
            inner_match_type = inner_raw.get("type")
            if inner_match_type not in _MATCH_TYPES - {"not"}:
                return None, f"atom `{field_name}` inner match type {inner_match_type!r} invalid"
            inner, inner_err = _compile_atom({"field": field_name, "match": inner_raw})
            if inner_err is not None or inner is None:
                return None, inner_err or "invalid inner match"
            matcher = NotMatcher(inner.matcher)
    except ValueError as e:
        return None, f"atom `{field_name}` matcher compile failed: {e}"
    return AtomCondition(field=field_name, matcher=matcher), None


def _compile_action(name: str, raw: object) -> tuple[Action | None, str | None]:
    """编译 `action:` 动作对象；type 枚举与各类型子字段必填（ch14 F16/F26）。"""
    if not isinstance(raw, dict):
        return None, "missing required field `action`"
    action_type = raw.get("type")
    if action_type not in {t.value for t in ActionType}:
        return None, f"unknown action type {action_type!r}"
    action_kind = ActionType(action_type)

    if action_kind is ActionType.SHELL:
        command = raw.get("command")
        if not isinstance(command, str) or not command:
            return None, "shell action requires non-empty `command`"
        return Action(type=action_kind, shell=ShellAction(command=command)), None
    if action_kind is ActionType.PROMPT:
        text = raw.get("text")
        if not isinstance(text, str) or not text:
            return None, "prompt action requires non-empty `text`"
        return Action(type=action_kind, prompt=PromptAction(text=text)), None
    if action_kind is ActionType.HTTP:
        url = raw.get("url")
        if not isinstance(url, str) or not url:
            return None, "http action requires non-empty `url`"
        method = raw.get("method", "POST")
        if not isinstance(method, str) or not method:
            return None, "http action `method` must be a non-empty string"
        headers_raw = raw.get("headers", {})
        if not isinstance(headers_raw, dict):
            return None, "http action `headers` must be a mapping"
        headers = {str(k): str(v) for k, v in headers_raw.items()}
        body = raw.get("body")
        if body is not None and not isinstance(body, str):
            return None, "http action `body` must be a string template"
        return (
            Action(
                type=action_kind,
                http=HttpAction(url=url, method=method, headers=headers, body=body),
            ),
            None,
        )
    # subagent：本期占位，仅校验字段完整（ch14 F26）
    agent_name = raw.get("agent_name")
    prompt_text = raw.get("prompt")
    if not isinstance(agent_name, str) or not agent_name:
        return None, "subagent action requires non-empty `agent_name`"
    if not isinstance(prompt_text, str) or not prompt_text:
        return None, "subagent action requires non-empty `prompt`"
    return (
        Action(
            type=action_kind, subagent=SubagentAction(agent_name=agent_name, prompt=prompt_text)
        ),
        None,
    )


def _parse_duration(raw: object) -> float | None:
    """解析时长串为秒数：`30s` / `5m` / `2h` / 纯数字秒；非法返回 None（ch14 F8）。"""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int | float):
        seconds = float(raw)
        return seconds if seconds >= 0 else None
    if not isinstance(raw, str):
        return None
    duration_match = _DURATION_PATTERN.match(raw.strip())
    if duration_match is None:
        return None
    number_text, unit = duration_match.group(1), duration_match.group(2)
    return float(number_text) * _DURATION_UNIT_SECONDS[unit]


__all__ = ["load"]
