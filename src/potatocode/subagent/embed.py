"""内置 Agent 定义的随包加载：`importlib.resources` 读取 builtin/*.md（ch15 F5/F32）。

打包成 wheel 后仍能读取（hatch 默认把包目录下全部文件打进 wheel）；解析失败
视为代码 bug 立即抛出——内置定义损坏是发布事故，启动期 fail-fast（N4）。
"""

from __future__ import annotations

from importlib.resources import files

from .definition import Definition, Source
from .parser import parse_definition

# 内置定义所在的包内目录
_BUILTIN_PACKAGE = "potatocode.subagent.builtin"


def builtin_definitions() -> list[Definition]:
    """解析全部内置角色定义，按 name 升序返回（F32）。

    任何单文件解析失败都直接 raise（N4：内置定义是代码的一部分，坏定义即 bug）。
    """
    pkg = files(_BUILTIN_PACKAGE)
    definitions: list[Definition] = []
    for entry in pkg.iterdir():
        if not entry.name.endswith(".md"):
            continue
        definitions.append(
            parse_definition(entry.read_bytes(), f"builtin:{entry.name}", Source.BUILTIN)
        )
    definitions.sort(key=lambda definition: definition.name)
    return definitions


__all__ = ["builtin_definitions"]
