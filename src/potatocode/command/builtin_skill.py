"""/skill 管理命令：list / info <name> / reload（ch13 F13）。

KindLocal：纯本地查询与目录重扫，不改对话历史、不消耗 token。reload 后通过宿主
注入的回调重建 skill 斜杠命令并刷新 Agent 的 Available Skills 目录。
"""

from __future__ import annotations

from collections.abc import Callable

from ..skills.loader import SkillLoader
from .command import ArgsHandler, Command, Kind
from .registry import Registry
from .ui import UI

# /skill 无参数 / 未知子命令时的用法提示
SKILL_USAGE_TEXT = "用法: /skill list | /skill info <name> | /skill reload"


async def _skill_usage_fallback(ui: UI) -> None:
    """/skill 零参数注册位（accepts_args 命令仍需一个 handler 字段）：输出用法。"""
    ui.println(SKILL_USAGE_TEXT)


def make_skill_manage_args_handler(
    loader: SkillLoader,
    on_reload: Callable[[], None],
) -> ArgsHandler:
    """闭包捕获 loader 与 reload 回调（回调由宿主注入，重建命令注册 + 刷新 catalog）。"""

    async def _handler(ui: UI, args: str) -> None:
        pieces = args.split()
        subcommand = pieces[0].lower() if pieces else ""
        if not subcommand or subcommand == "help":
            ui.println(SKILL_USAGE_TEXT)
            return
        if subcommand == "list":
            _print_skill_list(ui, loader)
            return
        if subcommand == "info":
            if len(pieces) < 2:
                ui.error("用法: /skill info <name>")
                return
            _print_skill_info(ui, loader, pieces[1])
            return
        if subcommand == "reload":
            loader.reload()
            on_reload()
            ui.println(f"已重新加载 {len(loader.names())} 个 skill")
            return
        ui.error(f"未知子命令: {subcommand}（可用: list / info <name> / reload）")

    return _handler


def _print_skill_list(ui: UI, loader: SkillLoader) -> None:
    """/skill list：逐行 `名字 描述 [来源]`（T9 格式），无 skill 时提示查找路径。"""
    names = loader.names()
    if not names:
        ui.println(
            "没有已加载的 skill（查找路径: 项目 .potatocode/skills/ 与 ~/.potatocode/skills/）"
        )
        return
    lines = [
        f"  {name:<20} {description}  [{loader.get_source_label(name)}]"
        for name, description in loader.get_catalog()
    ]
    ui.println("\n".join(lines))


def _print_skill_info(ui: UI, loader: SkillLoader, skill_name: str) -> None:
    """/skill info <name>：完整 frontmatter 字段 + 文件路径 + 目录型标记（F13）。"""
    skill = loader.get(skill_name)
    if skill is None:
        ui.error(f"未知 skill: {skill_name}")
        return
    lines = [
        f"name: {skill.name}",
        f"description: {skill.description}",
        f"mode: {skill.mode}",
        f"model: {skill.model or '-'}",
        f"context: {skill.context}",
        f"directory: {'yes' if skill.is_directory else 'no'}",
        f"path: {skill.source_path}",
    ]
    ui.println("\n".join(lines))


def register_skill_manage_command(
    reg: Registry,
    loader: SkillLoader,
    on_reload: Callable[[], None],
) -> None:
    """注册 /skill 管理命令（KindLocal、接受参数，F13）。"""
    reg.register(
        Command(
            name="skill",
            description="管理 skill：list | info <name> | reload",
            kind=Kind.LOCAL,
            handler=_skill_usage_fallback,
            accepts_args=True,
            args_handler=make_skill_manage_args_handler(loader, on_reload),
        )
    )


__all__ = [
    "make_skill_manage_args_handler",
    "register_skill_manage_command",
]
