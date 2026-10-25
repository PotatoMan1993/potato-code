"""子 Agent 工具过滤多层防线（ch15 F26-F31）。

五层过滤在 Agent 工具构造子 Agent 时一次性执行（F30），主 Agent 的工具列表
不受任何影响（F31/N1）：全局禁止列表 → 自定义 Agent 追加禁用 → 后台白名单
交集 → 定义黑名单 → 定义白名单。子 Agent 的受限工具集经 `build_sub_registry`
落成独立 Registry，权限分批与执行逻辑零改动。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import Registry

# 任何子 Agent 永远不能用的工具（F26 本期最小集；Agent 工具被剔除即断绝再嵌套）
ALL_AGENT_DISALLOWED_TOOLS: list[str] = ["Agent"]

# 自定义（user / project 来源）Agent 比内置 Agent 追加禁用的工具；本期为空（F27 接口预留）
CUSTOM_AGENT_DISALLOWED_TOOLS: list[str] = []

# 后台 Agent 工具白名单（F28）：只保留基础读写搜索与 Skill 桥接工具；
# 不含 Agent / TaskList / TaskGet / TaskStop / SendMessage 等任何元工具。
ASYNC_AGENT_ALLOWED_TOOLS: list[str] = [
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "bash",
    "LoadSkill",
    "InstallSkill",
]


def is_mcp_or_skill(name: str) -> bool:
    """按命名约定识别动态注册的工具：MCP 工具以 `mcp__` 起头（F28）。

    Skill 桥接工具（LoadSkill / InstallSkill）已显式列入后台白名单，此处只判
    MCP 前缀；skill 斜杠命令本期不注册为独立工具，无需识别。
    """
    return name.startswith("mcp__")


@dataclass
class FilterParams:
    """一次过滤的全部入参（F30）。"""

    all: list[str]  # registry 的全部工具名（按注册顺序）
    source: int  # subagent.Source 的整数值（1+ 为自定义来源，触发追加禁用）
    background: bool  # 是否后台（Fork / run_in_background / 定义强制）
    allowed: list[str] = field(default_factory=list)  # Agent 定义 tools 白名单
    disallowed: list[str] = field(default_factory=list)  # Agent 定义 disallowedTools 黑名单


def apply_agent_tool_filter(p: FilterParams) -> list[str]:
    """按 spec F30 顺序应用五层过滤，返回最终 allowed 列表（保持注册顺序）。"""
    allowed = [name for name in p.all if name not in ALL_AGENT_DISALLOWED_TOOLS]
    if p.source >= 2:  # 非内置来源：追加自定义禁用列表（本期为空，F27）
        allowed = [name for name in allowed if name not in CUSTOM_AGENT_DISALLOWED_TOOLS]
    if p.background:
        background_set = set(ASYNC_AGENT_ALLOWED_TOOLS)
        allowed = [name for name in allowed if name in background_set or is_mcp_or_skill(name)]
    if p.disallowed:
        disallowed = set(p.disallowed)
        allowed = [name for name in allowed if name not in disallowed]
    if p.allowed:
        whitelist = set(p.allowed)
        allowed = [name for name in allowed if name in whitelist]
    return allowed


def build_sub_registry(parent: Registry, allowed: list[str]) -> Registry:
    """按 allowed 名单构造子注册中心（引用同一批工具实例，顺序沿用父注册序，F30）。"""
    whitelist = set(allowed)
    sub = Registry()
    for name in parent.names():
        if name in whitelist:
            tool = parent.get(name)
            if tool is not None:
                sub.register(tool)
    return sub


__all__ = [
    "ALL_AGENT_DISALLOWED_TOOLS",
    "ASYNC_AGENT_ALLOWED_TOOLS",
    "CUSTOM_AGENT_DISALLOWED_TOOLS",
    "FilterParams",
    "apply_agent_tool_filter",
    "build_sub_registry",
    "is_mcp_or_skill",
]
