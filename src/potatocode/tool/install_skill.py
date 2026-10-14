"""InstallSkill 工具：把用户发来的 skill URL 安装到用户级目录（ch13 F19/T12）。

write 类别：写盘 + 网络下载，正常受权限模式约束。安装完成后自动 reload skill
目录并触发宿主回调（重建斜杠命令 + 刷新 Agent 目录），无需重启即可使用。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx

from ..skills.install import InstallSkillError, install_skill
from ..skills.loader import SkillLoader
from . import Result

logger = logging.getLogger(__name__)

# 模型看到的工具名
TOOL_NAME_INSTALL_SKILL = "InstallSkill"


class InstallSkillTool:
    """InstallSkill 工具：URL → 用户级 skill 目录，装完即热加载。"""

    def __init__(self, loader: SkillLoader) -> None:
        self._loader = loader
        # 宿主回调：安装成功后重建斜杠命令并刷新 Available Skills 目录（T12）
        self._on_installed: Callable[[], None] | None = None

    def set_on_installed(self, on_installed: Callable[[], None]) -> None:
        """注入安装完成回调（cli 启动期接 TUI 的 rebind_skill_commands）。"""
        self._on_installed = on_installed

    def name(self) -> str:
        return TOOL_NAME_INSTALL_SKILL

    def description(self) -> str:
        return (
            "从 URL 安装一个 skill 到用户级目录（~/.potatocode/skills/）。"
            "支持 skills.sh、github.com tree、raw.githubusercontent.com 三种地址；"
            "用户发来 skill 链接并希望安装时调用本工具。安装完成后自动重新加载，"
            "无需重启即可通过斜杠命令或 LoadSkill 使用。"
        )

    def read_only(self) -> bool:
        return False  # 写盘 + 网络：受权限模式约束（write 类别）

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "skill 来源 URL（skills.sh / github.com tree / raw 直链）",
                },
            },
            "required": ["url"],
        }

    async def execute(self, args: str) -> Result:
        """安装 → reload → 触发宿主回调；所有失败都包成结构化错误结果（F9）。"""
        try:
            data = json.loads(args) if args and args.strip() else {}
        except ValueError:
            return Result(content="参数不是合法 JSON，需要 {\"url\": \"...\"}", is_error=True)
        source_url = data.get("url") if isinstance(data, dict) else None
        if not isinstance(source_url, str) or not source_url.strip():
            return Result(content="缺少参数 url（skill 来源 URL）", is_error=True)

        try:
            skill_name = await install_skill(source_url.strip(), self._loader.user_dir)
        except (InstallSkillError, ValueError) as install_error:
            return Result(content=f"安装失败: {install_error}", is_error=True)
        except httpx.HTTPError as network_error:
            return Result(content=f"网络错误: {network_error}", is_error=True)

        # 安装成功：热重载目录 + 通知宿主重建命令（回调失败不回滚安装，N1）
        self._loader.reload()
        if self._on_installed is not None:
            try:
                self._on_installed()
            except Exception as callback_error:
                logger.warning("skill 安装后的命令重建回调失败: %s", callback_error)
        return Result(
            content=(
                f"Skill '{skill_name}' 安装成功，已重新加载 skill 目录。"
                f"可通过 /{skill_name} 命令或 LoadSkill 工具使用。"
            )
        )
