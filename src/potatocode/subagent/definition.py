"""subagent：SubAgent 机制的数据层（ch15）。

Agent 角色定义（Markdown + YAML frontmatter）、多来源 Catalog 加载与解析校验。
只依赖 permission（Mode / parse_mode），不依赖 agent / tui，避免反向依赖
（agent 层经 agent_tool 消费本包，依赖方向保持单向）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from ..permission import Mode


class Source(IntEnum):
    """Agent 定义的来源层级：数值越大优先级越高，同名高优先级覆盖低优先级（F4/F6）。"""

    BUILTIN = 0  # 随包发布的内置角色（general-purpose / Explore / Plan）
    USER = 1  # 用户级 ~/.potatocode/agents/*.md
    PROJECT = 2  # 项目级 <root>/.potatocode/agents/*.md
    PLUGIN = 3  # 插件级（本期占位恒为空，F8）

    def __str__(self) -> str:
        return {0: "builtin", 1: "user", 2: "project", 3: "plugin"}.get(int(self), "unknown")


@dataclass
class Definition:
    """一个 Agent 角色的完整定义，从 Markdown + YAML frontmatter 解析（F4）。

    frontmatter 声明元数据，正文（去 frontmatter 后的全文）作为子 Agent 的
    system prompt；`tools` / `disallowed_tools` 在 Agent 工具构造子 Agent 时经
    `tool.filter.apply_agent_tool_filter` 组合应用（F29/F30）。
    """

    name: str  # 角色名（frontmatter.name；Fork 路径用 "__fork__" 特殊值）
    description: str  # 一句话描述（Agent 工具 subagent_type 文档与 UI 列表用）
    tools: list[str] = field(default_factory=list)  # 工具白名单；空表示不收窄（F29）
    disallowed_tools: list[str] = field(default_factory=list)  # 工具黑名单（F29）
    model: str = "inherit"  # haiku / sonnet / opus / inherit；本期不实现按名切换 provider
    max_turns: int = 0  # 最大迭代轮数；0 表示沿用全局 MAX_ITERATIONS=25（F4）
    permission_mode: Mode = Mode.DEFAULT  # 子 Agent 启动权限模式（F4）；dontAsk 单独走 dont_ask
    dont_ask: bool = False  # 子 Agent 专属：规则未命中的 Ask 一律自动放行（F4/F12-②）
    background: bool = False  # True 时 Agent 工具忽略 run_in_background、强制后台（F4）
    system_prompt: str = ""  # 正文全文（子 Agent 系统提示）；空 = 继承主系统提示
    file_path: str = ""  # 定义文件路径（builtin 来源为 "builtin:<name>"，调试用）
    source: Source = Source.BUILTIN  # 定义来源（同名覆盖判定 F6）

    def is_fork(self) -> bool:
        """是否 Fork 式定义（Agent 工具 subagent_type 留空时的默认路径，F22）。"""
        return self.name == "__fork__"


__all__ = ["Definition", "Source"]
