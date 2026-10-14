"""LoadSkill 工具：Agent 意图触发入口——按需把 skill 的 SOP 钉到环境上下文（ch13 F10/N2）。

read-only 类别：不弹权限提示（N2）、Plan 模式可用。工具结果只带简短确认，不返回
完整 SOP（避免 tool_result 膨胀；完整内容经 env 段的 Active Skills 注入，F10）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..skills.loader import SkillLoader
from . import Result

if TYPE_CHECKING:
    from ..agent import Agent

logger = logging.getLogger(__name__)

# 模型看到的工具名
TOOL_NAME_LOAD_SKILL = "LoadSkill"


class LoadSkill:
    """LoadSkill 工具：loader 与 agent 经注入器在启动期接上（先建工具、后建 Agent）。"""

    def __init__(self) -> None:
        self._loader: SkillLoader | None = None
        # agent 以 getter 形式持有：Agent 在 provider 选定后才构造（TUI 惰性初始化），
        # 传 getter 可保证换 provider 重建 Agent 后引用依然有效
        self._agent_getter: Callable[[], Agent | None] | None = None

    def set_loader(self, loader: SkillLoader) -> None:
        """注入 SkillLoader（cli 启动期调用，T8）。"""
        self._loader = loader

    def set_agent(self, agent: Agent | Callable[[], Agent | None]) -> None:
        """注入主 Agent 实例或其 getter（TUI 启动期调用；getter 兼容惰性构造，T7）。"""
        if callable(agent):
            self._agent_getter = agent
        else:
            static_agent = agent  # 静态实例包成 getter，统一后续取用路径

            def _static_agent_getter() -> Agent:
                return static_agent

            self._agent_getter = _static_agent_getter

    def _current_agent(self) -> Agent | None:
        """取当前主 Agent；getter 异常时按未初始化处理（不向模型抛异常）。"""
        if self._agent_getter is None:
            return None
        try:
            return self._agent_getter()
        except Exception as getter_error:  # 防御：getter 只读，异常吞掉按 None 处理
            logger.warning("LoadSkill 取 Agent 失败: %s", getter_error)
            return None

    def name(self) -> str:
        return TOOL_NAME_LOAD_SKILL

    def description(self) -> str:
        return (
            "按需激活（加载）一个 Skill 的完整 SOP。当用户的请求匹配环境上下文里"
            "「Available Skills」清单中的某个 skill 时调用本工具，把该 skill 的标准"
            "操作流程钉到环境上下文中，随后的轮次你就能看到并严格遵循它执行。"
        )

    def read_only(self) -> bool:
        return True  # 只读类别：Plan 模式可用、不弹权限提示（N2）、可并发

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "要激活的 skill 名称"},
            },
            "required": ["name"],
        }

    async def execute(self, args: str) -> Result:
        """激活 skill：loader.get 取最新版（热重载）→ agent.activate_skill 钉 SOP（F10）。"""
        try:
            data = json.loads(args) if args and args.strip() else {}
        except ValueError:
            return Result(content="参数不是合法 JSON，需要 {\"name\": \"...\"}", is_error=True)
        skill_name = data.get("name") if isinstance(data, dict) else None
        if not isinstance(skill_name, str) or not skill_name.strip():
            return Result(content="缺少参数 name（要激活的 skill 名称）", is_error=True)
        skill_name = skill_name.strip()

        loader = self._loader
        agent = self._current_agent()
        if loader is None or agent is None:
            return Result(content="LoadSkill not properly initialized", is_error=True)

        skill = loader.get(skill_name)  # 每次重读源文件，磁盘修改即时生效（F4/F17）
        if skill is None:
            available_names = ", ".join(loader.names()) or "（当前没有任何已加载的 skill）"
            return Result(
                content=f"unknown skill: {skill_name}（可用: {available_names}）",
                is_error=True,
            )

        agent.activate_skill(skill.name, skill.prompt_body)
        return Result(
            content=f"Skill '{skill.name}' activated. SOP pinned to environment context."
        )
