"""Skill 相关的提示词段：Available Skills 目录 + Active Skills SOP（ch13 F11/T6）。

两段都拼进 environment block（每轮重建、不进缓存前缀）：目录可被 /skill reload
或 InstallSkill 改变，已激活 SOP 更是逐轮变化，放环境段避免反复击穿稳定块缓存；
同时也避免 prompt 包反向依赖 skills 包——这里只认轻量的条目类型。
"""

from __future__ import annotations

from dataclasses import dataclass

from .environment import gather_environment


@dataclass(frozen=True)
class SkillCatalogItem:
    """Available Skills 清单条目：只含名字与描述（渐进式披露的第一阶段，F11）。"""

    name: str
    description: str


@dataclass(frozen=True)
class ActiveSkillEntry:
    """一条已激活的 skill：名字 + 激活那一刻的 SOP 正文。"""

    name: str
    body: str


def render_skills_catalog(items: list[SkillCatalogItem]) -> str:
    """渲染 Available Skills 段：条目列表 + LoadSkill 调用指引（F11/T8）。

    条目为空返回空串（装配时整段省略，不留空标题）。
    """
    if not items:
        return ""
    lines = [f"- {item.name}: {item.description}" for item in items]
    return (
        "## Available Skills\n\n"
        "You can use the following Skills:\n\n"
        + "\n".join(lines)
        + "\n\n"
        "If the user's request matches a Skill, call LoadSkill to activate it."
    )


def render_active_skills_block(entries: list[ActiveSkillEntry]) -> str:
    """渲染 Active Skills 段：每条 skill 一个 `### Skill:` 子段（T6）。

    条目为空返回空串；/clear 清空后本段自然消失（F18）。
    """
    if not entries:
        return ""
    sections = [f"### Skill: {entry.name}\n\n{entry.body.strip()}".strip() for entry in entries]
    return "## Active Skills\n\n" + "\n\n".join(sections)


def build_environment_context(
    version: str,
    model: str,
    skill_catalog: str = "",
    active_entries: list[ActiveSkillEntry] | None = None,
) -> str:
    """完整 environment 段：环境信息 + Available Skills 目录 + Active Skills（T6）。

    顺序按 T6 约定：先 skill_catalog 段落，再 `## Active Skills`；各段为空自动跳过。
    主循环每轮迭代重建环境段时调用本函数，实现 SOP 钉到 env 的能力（spec 5.3）。
    """
    parts: list[str] = []
    env_text = gather_environment(version, model).render()
    if env_text:
        parts.append(env_text)
    if skill_catalog:
        parts.append(skill_catalog)
    active_block = render_active_skills_block(active_entries or [])
    if active_block:
        parts.append(active_block)
    return "\n\n".join(parts)
