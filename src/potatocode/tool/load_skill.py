"""LoadSkill 工具：Agent 意图触发入口（ch13 F10/N2）。

read-only 类别：不弹权限提示（N2）、Plan 模式可用。mode 决定交付方式：
inline 把 SOP 正文（$ARGUMENTS 已渲染）直接作为工具结果返回，模型当轮即见；
fork 只负责后台启动子 Agent（executor.spawn_fork，结果经宿主回流主对话），
工具结果仅报告启动状态——不等待、不占工具超时预算。

宿主依赖 skills 包的 `SkillHost` 窄协议而非 TUI 具体类型：tool 包不反向依赖
TUI，避免 tool ↔ tui 循环导入（TUI App 经结构化类型满足协议）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..skills.executor import SkillExecutor, SkillHost
from ..skills.loader import SkillLoader
from ..skills.parser import substitute_arguments
from . import Result

if TYPE_CHECKING:
    from ..agent import Agent

logger = logging.getLogger(__name__)

# 模型看到的工具名
TOOL_NAME_LOAD_SKILL = "LoadSkill"


class LoadSkill:
    """LoadSkill 工具：loader / executor / host / agent 经注入器在启动期接上。"""

    def __init__(self) -> None:
        self._loader: SkillLoader | None = None
        self._executor: SkillExecutor | None = None
        self._host: SkillHost | None = None
        # agent 以 getter 形式持有：Agent 在 provider 选定后才构造（TUI 惰性初始化），
        # 传 getter 可保证换 provider 重建 Agent 后引用依然有效
        self._agent_getter: Callable[[], Agent | None] | None = None

    def set_loader(self, loader: SkillLoader) -> None:
        """注入 SkillLoader（cli 启动期调用，T8）。"""
        self._loader = loader

    def set_executor(self, executor: SkillExecutor) -> None:
        """注入 SkillExecutor（fork 分支后台启动子 Agent 用，cli 启动期调用）。"""
        self._executor = executor

    def set_host(self, host: SkillHost) -> None:
        """注入宿主（TUI App 启动期调用；fork 结果经宿主回流主对话）。"""
        self._host = host

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
            "按需激活（加载）一个 Skill。当用户的请求匹配「可用 Skill」清单中的某个"
            " skill 时调用本工具。inline 模式的 skill 会把完整 SOP 作为工具结果返回，"
            "随后请严格遵循它执行；fork 模式的 skill 会在后台启动子 Agent 独立执行，"
            "完成后结果自动回流到对话，无需等待。"
        )

    def read_only(self) -> bool:
        return True  # 只读类别：Plan 模式可用、不弹权限提示（N2）、可并发

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "要激活的 skill 名称"},
                "args": {
                    "type": "string",
                    "description": "传给 skill 的参数（替换 SOP 中的 $ARGUMENTS 占位符）；无需参数时省略",
                },
            },
            "required": ["name"],
        }

    async def execute(self, args: str) -> Result:
        """激活 skill：loader.get 取最新版（热重载）→ 按 mode 交付（F4/F17）。"""
        try:
            data = json.loads(args) if args and args.strip() else {}
        except ValueError:
            return Result(content="参数不是合法 JSON，需要 {\"name\": \"...\"}", is_error=True)
        skill_name = data.get("name") if isinstance(data, dict) else None
        if not isinstance(skill_name, str) or not skill_name.strip():
            return Result(content="缺少参数 name（要激活的 skill 名称）", is_error=True)
        skill_name = skill_name.strip()
        skill_args = data.get("args") if isinstance(data, dict) else ""
        if not isinstance(skill_args, str):
            skill_args = ""

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

        rendered_body = substitute_arguments(skill.prompt_body, skill_args)
        if not skill.is_fork():
            # inline：SOP 直接进 toolResult，模型当轮即见、随后轮次留在历史里
            return Result(content=f"Skill '{skill.name}' activated.\n\n{rendered_body}")

        # fork：后台启动子 Agent（不 await），结果经宿主回流主对话；
        # 不在工具内等待，因此不受 30s 工具超时约束
        if self._executor is None or self._host is None:
            return Result(content="LoadSkill not properly initialized", is_error=True)
        self._executor.spawn_fork(skill, skill_args, self._host)
        return Result(
            content=(
                f"Skill '{skill.name}' 已在后台启动（fork 模式）：子 Agent 正在独立执行"
                "该 SOP，完成后结果会自动回流到对话中。无需等待，先向用户说明已启动即可。"
            )
        )
