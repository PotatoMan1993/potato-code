"""危险命令黑名单（F1/N1）。

内置一组高危命令正则，在命令执行类工具真正执行前硬拦截。
这一层是**启发式防御而非完备保证**，明确不追求穷尽所有危险命令；
且**不可被任何配置、规则或模式（含 bypassPermissions）放开**——没有任何加载入口，
模块级常量直接参与 Engine 判定。
"""

from __future__ import annotations

import re

# 内置高危命令正则（不可配置，N1）。命中即 Deny，先于其余四层。
_BLACKLIST: tuple[re.Pattern[str], ...] = (     # TODO 各个命令的作用
    # 递归强删根/家目录（及其变体 rm -rf /、rm -fr ~、rm -rf $HOME、rm -rf /*）
    re.compile(r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|~|\$HOME|/\*|--no-preserve-root)"),
    # dd 写入块设备（of=/dev/...）
    re.compile(r"\bdd\s+.*\bof=/dev/"),
    # fork 炸弹 :(){ :|:& };:
    re.compile(r":\(\)\s*\{.*\|.*&\s*\}"),
    # 格式化文件系统（mkfs.ext4 等）
    re.compile(r"\bmkfs\."),
    # 重定向覆盖磁盘设备（> /dev/sda 等）
    re.compile(r">\s*/dev/(sd|hd|nvme|disk)"),
    # 递归开放根目录写权限（chmod -R 777 /）
    re.compile(r"\bchmod\s+-R\s+0?777\s+/"),
)


def hits_blacklist(command: str) -> bool:
    """命令串命中任一黑名单正则即返回 True。"""
    return any(p.search(command) for p in _BLACKLIST)


__all__ = ["hits_blacklist"]
