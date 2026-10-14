"""SKILL.md 解析：frontmatter 分离、元数据校验与 $ARGUMENTS 渲染（ch13 F1/F5）。

Skill 的磁盘形态有两种：单个 `*.md` 文件，或 `<目录>/SKILL.md`（可另带 references/
等资源）。两者共享同一份解析逻辑——YAML frontmatter（两行 `---` 之间）声明元数据，
其余正文是给 Agent 看的 SOP（标准操作流程）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# skill 名合法性：小写字母开头，只含小写字母 / 数字 / 连字符（F1）
SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9\-]*$")

# mode 合法取值：inline 主对话内执行，fork 独立子 Agent 隔离执行
VALID_SKILL_MODES: frozenset[str] = frozenset({"inline", "fork"})
# context 合法取值（仅 fork 生效）：子 Agent 携带多少主对话上下文
VALID_SKILL_CONTEXTS: frozenset[str] = frozenset({"full", "recent", "none"})

DEFAULT_SKILL_MODE = "inline"
DEFAULT_SKILL_CONTEXT = "full"

# frontmatter 开闭定界符（必须独占一行）
FRONTMATTER_DELIMITER = "---"


class SkillParseError(Exception):
    """SKILL.md 解析失败：frontmatter 缺失 / 未闭合 / YAML 非法 / 元数据校验不过。"""


@dataclass
class SkillDef:
    """一个已解析的 Skill 能力包：元数据 + SOP 正文 + 磁盘位置（F1）。"""

    name: str
    description: str
    prompt_body: str  # 去 frontmatter 后的正文（SOP 本体）
    mode: str = DEFAULT_SKILL_MODE  # inline | fork
    model: str = ""  # fork 期望使用的模型（预留：本期 fork 沿用主 provider，T5）
    context: str = DEFAULT_SKILL_CONTEXT  # full | recent | none，仅 fork 生效
    source_path: str = ""  # SKILL.md 绝对路径（热重载按此重读，F4/F17）
    is_directory: bool = False  # True=目录型（<目录>/SKILL.md + 资源）

    def is_fork(self) -> bool:
        """是否 fork 模式（决定命令注册时的运行时分支，F12）。"""
        return self.mode == "fork"


def parse_frontmatter(raw: str) -> tuple[dict, str]:
    """把 SKILL.md 原文拆成 (frontmatter 字典, SOP 正文)（F1/T1）。

    格式约定：首行必须是独占一行的 `---`，到下一个独占行的 `---` 之间是 YAML
    frontmatter，其余是正文。开闭缺失、YAML 非法或顶层不是字典都抛 SkillParseError。
    """
    lines = raw.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        raise SkillParseError("frontmatter 缺失：文件必须以独占一行的 '---' 开头")
    closing_line_index = -1
    for index in range(1, len(lines)):
        if lines[index].strip() == FRONTMATTER_DELIMITER:
            closing_line_index = index
            break
    if closing_line_index < 0:
        raise SkillParseError("frontmatter 未闭合：缺少收尾的 '---' 行")
    frontmatter_text = "\n".join(lines[1:closing_line_index])
    prompt_body = "\n".join(lines[closing_line_index + 1 :]).strip("\n")
    try:
        meta = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as yaml_error:
        raise SkillParseError(f"frontmatter YAML 解析失败: {yaml_error}") from yaml_error
    if not isinstance(meta, dict):
        raise SkillParseError("frontmatter 顶层必须是键值映射（dict）")
    return meta, prompt_body


def _validate_meta(meta: dict) -> None:
    """校验 frontmatter 元数据的必填项与取值范围（T1）；不合法抛 SkillParseError。"""
    name = meta.get("name")
    if not name:
        raise SkillParseError("frontmatter 缺少 name 字段")
    if not isinstance(name, str) or not SKILL_NAME_PATTERN.fullmatch(name):
        raise SkillParseError(
            f"name 非法（需小写字母开头，仅含小写字母/数字/连字符）: {name!r}"
        )
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillParseError("frontmatter 缺少 description 字段或其不是非空字符串")
    mode = meta.get("mode") or DEFAULT_SKILL_MODE
    if mode not in VALID_SKILL_MODES:
        raise SkillParseError(f"mode 取值非法（{sorted(VALID_SKILL_MODES)}）: {mode!r}")
    context = meta.get("context") or DEFAULT_SKILL_CONTEXT
    if context not in VALID_SKILL_CONTEXTS:
        raise SkillParseError(f"context 取值非法（{sorted(VALID_SKILL_CONTEXTS)}）: {context!r}")


def parse_skill_file(path: Path | str, is_directory: bool = False) -> SkillDef:
    """解析一个 SKILL.md 文件为 SkillDef（F1/F4）。

    `is_directory` 由调用方（SkillLoader）按磁盘布局传入：目录型 skill 的
    source_path 指向其中的 SKILL.md。文件不存在 / 读盘失败 / 解析失败抛
    SkillParseError，由上层决定跳过还是回退缓存。
    """
    skill_path = Path(path)
    try:
        raw = skill_path.read_text(encoding="utf-8")
    except OSError as os_error:
        raise SkillParseError(f"读取 SKILL.md 失败: {skill_path}: {os_error}") from os_error
    meta, prompt_body = parse_frontmatter(raw)
    _validate_meta(meta)
    return SkillDef(
        name=str(meta["name"]),
        description=str(meta["description"]),
        prompt_body=prompt_body,
        mode=str(meta.get("mode") or DEFAULT_SKILL_MODE),
        model=str(meta.get("model") or ""),
        context=str(meta.get("context") or DEFAULT_SKILL_CONTEXT),
        source_path=str(skill_path.resolve()),
        is_directory=is_directory,
    )


def substitute_arguments(prompt_body: str, args: str) -> str:
    """把 SOP 正文中的 `$ARGUMENTS` 占位符替换为调用参数（F5）。

    没有占位符时原样返回——参数交互通过 skill 激活后的后续轮次对话补充。
    """
    if "$ARGUMENTS" not in prompt_body:
        return prompt_body
    return prompt_body.replace("$ARGUMENTS", args)
