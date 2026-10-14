"""skills 包：Skill 能力包的数据、加载、执行与远程安装（ch13）。

对外窄接口：
- 数据与解析：SkillDef / SkillParseError / parse_frontmatter / parse_skill_file /
  substitute_arguments
- 加载：SkillLoader（两级搜索 + 热重载 + 来源标识）
- 执行：SkillExecutor（inline / fork）、SYSTEM_TOOL_NAMES
- 安装：parse_skill_url / install_skill / InstallSkillError
"""

from __future__ import annotations

from .executor import SYSTEM_TOOL_NAMES, SkillExecutor, SkillHost
from .install import InstallSkillError, install_skill, parse_skill_url
from .loader import PROJECT_SKILLS_DIR, USER_SKILLS_DIR, SkillLoader
from .parser import (
    DEFAULT_SKILL_CONTEXT,
    DEFAULT_SKILL_MODE,
    SKILL_NAME_PATTERN,
    SkillDef,
    SkillParseError,
    parse_frontmatter,
    parse_skill_file,
    substitute_arguments,
)

__all__ = [
    "DEFAULT_SKILL_CONTEXT",
    "DEFAULT_SKILL_MODE",
    "PROJECT_SKILLS_DIR",
    "SKILL_NAME_PATTERN",
    "SYSTEM_TOOL_NAMES",
    "InstallSkillError",
    "SkillDef",
    "SkillExecutor",
    "SkillHost",
    "SkillLoader",
    "SkillParseError",
    "USER_SKILLS_DIR",
    "install_skill",
    "parse_frontmatter",
    "parse_skill_file",
    "parse_skill_url",
    "substitute_arguments",
]
