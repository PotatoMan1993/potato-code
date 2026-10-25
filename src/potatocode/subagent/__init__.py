"""subagent 包：Agent 角色定义、解析与多来源 Catalog（ch15）。

对外导出 Definition / Source / Catalog / load_catalog / builtin_definitions。
本包只依赖 permission，不依赖 agent / tui（agent 经 agent_tool 单向消费本包）。
"""

from __future__ import annotations

from .catalog import USER_AGENTS_DIR, Catalog, load_catalog
from .definition import Definition, Source
from .embed import builtin_definitions
from .parser import AGENT_NAME_REGEX, AgentParseError, parse_definition, parse_file

__all__ = [
    "AGENT_NAME_REGEX",
    "USER_AGENTS_DIR",
    "AgentParseError",
    "Catalog",
    "Definition",
    "Source",
    "builtin_definitions",
    "load_catalog",
    "parse_definition",
    "parse_file",
]
