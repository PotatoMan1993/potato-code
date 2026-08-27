"""路径沙箱（F2/N2）：把文件类工具的读写限定在项目根目录内。

先解析符号链接、再做前缀判断，防止用软链接指向项目外目录绕过；
对尚不存在的目标（新建文件、含未创建中间目录）回退到最近已存在祖先再解析。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import Engine


def resolve_root(root: str) -> str:
    """规整项目根：展开 ~ 并解析为绝对路径（含符号链接）。

    失败（如路径不存在）抛 FileNotFoundError，由 new_engine 捕获降级。
    """
    return str(Path(root).expanduser().resolve(strict=True))


def eval_symlinks_or_ancestor(abs_path: str) -> str:
    """解析符号链接（N2）：存在则 resolve；不存在则回退到最近已存在祖先再拼回剩余段。

    返回规整后的绝对路径（字符串），供前缀判断。
    """
    p = Path(abs_path)
    missing: list[str] = []
    existing = p
    # 逐级向上找到最近一个「已存在」的祖先（跟随软链接判定存在性）
    while not existing.exists():
        missing.append(existing.name)
        parent = existing.parent
        if parent == existing:  # 到达根，无路可退
            break
        existing = parent
    try:
        resolved = existing.resolve(strict=True)
    except (OSError, RuntimeError):
        # 符号链接环 / 解析异常：退化为不解析符号链接的绝对路径（尽力而为）
        return os.path.abspath(abs_path)
    if missing:
        return str(resolved.joinpath(*reversed(missing)))
    return str(resolved)


def sandbox_ok(engine: Engine, path: str) -> bool:
    """判断文件目标是否落在项目根内。

    空 path 视为根本身；相对路径相对根解析；绝对路径直接判定。
    用 pathlib / os.sep，不硬编码 `/`。
    """
    root = engine.root
    if not path:
        target = root
    elif os.path.isabs(path):
        target = path
    else:
        target = os.path.join(root, path)

    resolved = eval_symlinks_or_ancestor(target)
    return resolved == root or resolved.startswith(root + os.sep)


def relpath_for(root: str, target: str) -> str:
    """把文件目标规整为项目相对 slash 路径（用于规则匹配与规则生成）。"""
    if not target:
        return ""
    if os.path.isabs(target):
        p = target
    else:
        p = os.path.join(root, target)
    rel = os.path.relpath(p, root)
    return rel.replace(os.sep, "/")


__all__ = ["eval_symlinks_or_ancestor", "relpath_for", "resolve_root", "sandbox_ok"]
