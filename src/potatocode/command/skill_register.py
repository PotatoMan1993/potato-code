"""把 skill 目录清单注册为斜杠命令（ch13 F12/F14）。

每个 skill 一条 `/ <name>` 命令，描述末尾标注 `[skill]`；mode 决定运行时分支：
inline 在主对话内执行（钉 SOP + 注入触发消息开跑本轮），fork 后台子 Agent 跑完后
把结果作为 assistant 消息回流主对话。模块级 `_REGISTERED_SKILL_NAMES` 跟踪本会话
已注册的 skill 命令，reload 时先清旧再建新，保证幂等。
"""

from __future__ import annotations

import asyncio
import logging

from ..prompt import SkillCatalogItem, render_skills_catalog
from ..skills.executor import SkillExecutor
from ..skills.loader import SkillLoader
from ..skills.parser import SkillDef
from .command import Command, Handler, Kind
from .registry import Registry
from .ui import UI

logger = logging.getLogger(__name__)

# 本会话已注册为斜杠命令的 skill 名（reload 时据此清旧，避免重复注册冲突）
_REGISTERED_SKILL_NAMES: set[str] = set()

# skill 命令描述的统一后缀（/help 与补全菜单里标识 skill 来源）
SKILL_COMMAND_DESCRIPTION_SUFFIX = " [skill]"

# inline skill 激活后注入的触发消息：SOP 已钉在 env 段，这里只负责开跑本轮
INLINE_SKILL_TRIGGER_TEXT = (
    "已激活上述 skill，请严格按环境上下文 Active Skills 段中的 SOP 推进任务。"
)


def register_skill_commands(reg: Registry, loader: SkillLoader, executor: SkillExecutor) -> None:
    """把 loader 清单里的每个 skill 注册成 `/ <name>` 命令（F12/F14）。

    调用前先摘除上一轮注册的 skill 命令（reload 幂等）；skill 与已有命令同名时
    skill 版本优先覆盖旧 handler（F14，unregister 后重注册）。
    """
    for stale_skill_name in sorted(_REGISTERED_SKILL_NAMES):
        reg.unregister(stale_skill_name)
    _REGISTERED_SKILL_NAMES.clear()
    for skill in loader.all():
        reg.unregister(skill.name)  # F14：摘除同名旧命令（可能是内置命令），skill 优先
        _REGISTERED_SKILL_NAMES.add(skill.name)
        if skill.is_fork():
            command_kind = Kind.UI  # fork 走后台任务，不直接注入消息，但要 idle 守护
            handler = _make_fork_skill_handler(skill.name, loader, executor)
        else:
            command_kind = Kind.PROMPT  # inline 注入 user 消息并触发回合
            handler = _make_inline_skill_handler(skill.name, loader, executor)
        reg.register(
            Command(
                name=skill.name,
                description=f"{skill.description}{SKILL_COMMAND_DESCRIPTION_SUFFIX}",
                kind=command_kind,
                handler=handler,
            )
        )


def build_skill_catalog_text(loader: SkillLoader) -> str:
    """把 loader 清单渲染成 Available Skills 目录文本（env 段注入用，F11）。"""
    catalog_items = [
        SkillCatalogItem(name=skill_name, description=skill_description)
        for skill_name, skill_description in loader.get_catalog()
    ]
    return render_skills_catalog(catalog_items)


def _make_inline_skill_handler(
    skill_name: str,
    loader: SkillLoader,
    executor: SkillExecutor,
) -> Handler:
    """inline skill 命令 handler：钉 SOP → 注入触发消息开跑本轮（F12/spec 5.4）。"""

    async def _handler(ui: UI) -> None:
        skill = loader.get(skill_name)  # 每次重读磁盘，热更新即时生效（F17）
        if skill is None:
            ui.error(f"skill '{skill_name}' 已不存在（可尝试 /skill reload）")
            return
        if ui.current_agent() is None:
            ui.error("尚未选定 provider，无法执行 skill")
            return
        executor.execute_inline(skill, "", ui)
        ui.inject_and_send(f"/{skill_name}", INLINE_SKILL_TRIGGER_TEXT)

    return _handler


def _make_fork_skill_handler(
    skill_name: str,
    loader: SkillLoader,
    executor: SkillExecutor,
) -> Handler:
    """fork skill 命令 handler：后台任务跑子 Agent，结果回流主对话（F12/checklist）。"""

    async def _handler(ui: UI) -> None:
        skill = loader.get(skill_name)
        if skill is None:
            ui.error(f"skill '{skill_name}' 已不存在（可尝试 /skill reload）")
            return
        if ui.current_agent() is None:
            ui.error("尚未选定 provider，无法执行 skill")
            return
        fork_task = asyncio.create_task(_run_fork(skill, ui, executor))
        executor.track_fork_task(fork_task)  # 持引用防后台任务被回收

    return _handler


async def _run_fork(skill: SkillDef, ui: UI, executor: SkillExecutor) -> None:
    """后台执行 fork 子 Agent，把最终文本作为 assistant 消息回流主对话（F7）。

    execute_fork 内部已把异常包成错误文本；这里再兜一层防御，失败走 ui.error。
    """
    try:
        final_text = await executor.execute_fork(skill, "", ui)
    except Exception as fork_error:  # 防御：回流路径自身出错时给出可读提示
        logger.exception("fork skill '%s' 异常", skill.name)
        ui.error(f"fork skill '{skill.name}' 失败: {type(fork_error).__name__}: {fork_error}")
        return
    ui.append_assistant_message(final_text)


__all__ = [
    "build_skill_catalog_text",
    "register_skill_commands",
]
