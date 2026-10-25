"""Agent 定义目录（Catalog）：多来源加载、同名按来源优先级覆盖（ch15 F4-F8）。

加载顺序 builtin → user → project（plugin 本期恒为空，F8），后加载的优先级更高，
同名定义覆盖先加载者；`resolve` 恒返回最高优先级版本（F6）。
单文件解析失败仅 stderr 警告并跳过，不阻断启动（F7）；内置定义失败则 fail-fast（N4）。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from ..permission import Mode
from .definition import Definition, Source
from .embed import builtin_definitions
from .parser import AgentParseError, parse_file

# 用户级定义目录（~/.potatocode/agents）
USER_AGENTS_DIR = Path.home() / ".potatocode" / "agents"


class Catalog:
    """已加载 Agent 定义的目录：name → 最高优先级 Definition（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._defs: dict[str, Definition] = {}  # name -> 最高优先级定义
        self._by_source: dict[Source, list[Definition]] = {}  # 各来源副本（展示 / 调试用）

    def _add_all(self, defs: list[Definition], source: Source) -> None:
        """批量收录一个来源的定义：同名覆盖（调用方按低→高优先级顺序加载，F6）。"""
        with self._lock:
            for definition in defs:
                self._defs[definition.name] = definition
                self._by_source.setdefault(source, []).append(definition)

    def resolve(self, name: str) -> Definition | None:
        """按名取最高优先级定义；未命中返回 None（F6）。"""
        with self._lock:
            return self._defs.get(name)

    def list(self) -> list[Definition]:
        """全部定义，按 name 升序（Agent 工具描述渲染用）。"""
        with self._lock:
            return sorted(self._defs.values(), key=lambda definition: definition.name)

    def list_by_source(self, source: Source) -> list[Definition]:
        """按来源列出该层加载过的全部定义（含被覆盖的旧版本，调试用）。"""
        with self._lock:
            return list(self._by_source.get(source, []))

    def fork_definition(self) -> Definition:
        """Fork 路径用的临时定义（F22）：name="__fork__"，system prompt 留空继承主提示。

        tools / disallowed_tools 均为空——Fork 子 Agent 工具集继承父，Agent 工具
        故意保留（保 prompt cache 一致性），靠调用深度标记阻断再嵌套（F24）。
        """
        return Definition(
            name="__fork__",
            description="Fork-based subagent",
            model="inherit",
            max_turns=0,  # 0 = 沿用全局 MAX_ITERATIONS
            permission_mode=Mode.DEFAULT,
        )


def _load_from_dir(directory: Path, source: Source) -> list[Definition]:
    """扫描目录下全部 *.md 定义；目录不存在返回空，单文件失败警告跳过（F7）。"""
    if not directory.is_dir():
        return []
    definitions: list[Definition] = []
    for path in sorted(directory.glob("*.md")):
        try:
            definitions.append(parse_file(path, source))
        except AgentParseError as parse_error:
            print(f"subagent {path}: {parse_error}，已跳过", file=sys.stderr)
    return definitions


def load_catalog(
    root: str,
    user_dir: Path | str | None = None,
    project_dir: Path | str | None = None,
) -> Catalog:
    """加载三层 Agent 定义目录，返回非 None Catalog（无定义时也为空目录，F5/F6）。

    顺序 builtin → user → project，后加载者覆盖同名（F6）；`user_dir` /
    `project_dir` 供测试注入（缺省 ~/.potatocode/agents 与 <root>/.potatocode/agents）。
    """
    catalog = Catalog()
    catalog._add_all(builtin_definitions(), Source.BUILTIN)
    catalog._add_all(
        _load_from_dir(Path(user_dir) if user_dir is not None else USER_AGENTS_DIR, Source.USER),
        Source.USER,
    )
    catalog._add_all(
        _load_from_dir(
            Path(project_dir) if project_dir is not None else Path(root) / ".potatocode/agents",
            Source.PROJECT,
        ),
        Source.PROJECT,
    )
    return catalog


__all__ = ["Catalog", "USER_AGENTS_DIR", "load_catalog"]
