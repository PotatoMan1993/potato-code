"""Skill 相关的提示词段：Available Skills 目录（ch13 F11）。

目录文本进系统提示的「可用Skill」槽：启动期注入一次，/skill reload 或安装
skill 后由 TUI 刷新——内容稳定、可进缓存前缀。inline skill 的 SOP 经
toolResult / 注入消息进入对话，不再由本包渲染。prompt 包不反向依赖 skills
包——这里只认轻量的条目类型。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillCatalogItem:
    """Available Skills 清单条目：只含名字与描述（渐进式披露的第一阶段，F11）。"""

    name: str
    description: str


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
