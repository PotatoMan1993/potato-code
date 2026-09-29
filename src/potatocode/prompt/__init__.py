"""prompt 包：系统提示模块化装配、环境信息、补充消息与启动横幅。

对外导出（F1/F2/F6/F7）：
- Module / fixed_modules / optional_modules / assemble_system / build_system_prompt
- Environment / gather_environment
- system_reminder / plan_reminder / EXECUTE_DIRECTIVE
- BANNER / render_banner（启动横幅，保留 ch02/ch03 行为）
"""

from __future__ import annotations

from .environment import Environment, gather_environment
from .modules import Module, fixed_modules, optional_modules
from .reminder import EXECUTE_DIRECTIVE, plan_reminder, system_reminder


def assemble_system(mods: list[Module]) -> str:
    """按 priority 升序稳定排序、跳过空 content、以空行连接成完整系统提示（F1/AC1/AC2）。"""
    ordered = sorted(mods, key=lambda m: m.priority)
    parts = [m.content for m in ordered if m.content]
    return "\n\n".join(parts)


def build_system_prompt(instructions: str = "", memory: str = "") -> str:
    """装配稳定系统提示：固定模块 + 可选槽（空槽跳过，不留多余空行）（F1/F43）。

    `instructions` 为项目指令文本（priority 80）、`memory` 为长期记忆索引
    （priority 100）；两者为空时行为与 ch08 完全一致。
    """
    return assemble_system(fixed_modules() + optional_modules(instructions, memory))


# ASCII PotatoCode
BANNER: str = r"""
    ██████╗  ██████╗ ████████╗ █████╗ ████████╗ ██████╗ 
    ██╔══██╗██╔═══██╗╚══██╔══╝██╔══██╗╚══██╔══╝██╔═══██╗
    ██████╔╝██║   ██║   ██║   ███████║   ██║   ██║   ██║
    ██╔═══╝ ██║   ██║   ██║   ██╔══██║   ██║   ██║   ██║
    ██║     ╚██████╔╝   ██║   ██║  ██║   ██║   ╚██████╔╝
    ╚═╝      ╚═════╝    ╚═╝   ╚═╝  ╚═╝   ╚═╝    ╚═════╝ 
"""


def render_banner(version: str, cwd: str) -> str:
    """拼装启动横幅：ASCII 图案 + 应用名与版本号 + 当前工作目录 + 就绪提示行。"""
    return (
        f"{BANNER.rstrip()}\n"
        f"\n"
        f"  PotatoCode v{version}\n"
        f"  cwd: {cwd}\n"
        f"\n"
        f"  Ready. 输入消息开始对话（Enter 发送 · Alt+Enter 换行 · "
        f"/plan 计划模式 · /do 执行 · /exit 退出）\n"
    )


__all__ = [
    "BANNER",
    "Environment",
    "EXECUTE_DIRECTIVE",
    "Module",
    "assemble_system",
    "build_system_prompt",
    "fixed_modules",
    "gather_environment",
    "optional_modules",
    "plan_reminder",
    "render_banner",
    "system_reminder",
]
