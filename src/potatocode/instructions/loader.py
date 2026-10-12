"""项目指令加载器：三层 POTATOCODE.md 合并 + `@include` 展开。

三层优先级（高在前，模型优先遵循）：

1. `<project_root>/POTATOCODE.md` —— 项目级
2. `<project_root>/.potatocode/POTATOCODE.md` —— 项目配置级
3. `~/.potatocode/POTATOCODE.md` —— 用户级

每一层缺失、读不了、是二进制都只跳过该层，绝不阻断启动（N5）；层与层之间用空行
分隔。`@include` 支持嵌套（限 5 层）、环路检测与路径逃逸检测，三种异常都保留原
`@include` 行并就地追加一条 HTML 注释警告，让模型和用户都能看出哪里被跳过了。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 独占一行的 @include 引用：`@include <相对路径>`
INCLUDE_PATTERN = re.compile(r"^@include[ \t]+(.+?)\s*$")

# 嵌套深度上限：POTATOCODE.md 本身算第 1 层
MAX_DEPTH = 5

# 二进制探测窗口（前 512 字节出现 NUL 即视为不可读）
BINARY_PROBE_BYTES = 512

# 各类跳过场景的警告文案（同时用于注释与测试断言）
DEPTH_WARNING = "@include 超过最大嵌套深度，已跳过"
CYCLE_WARNING = "@include 检测到环路，已跳过"
ESCAPE_WARNING = "@include 路径超出允许范围，已跳过"
BINARY_WARNING = "@include 二进制文件不可读，已跳过"


def _warning(text: str, path: str) -> str:
    """把跳过原因渲染成一行 HTML 注释（Markdown 里不显示，但对模型可见）。"""
    return f"<!-- {text}: {path} -->"


def _within(path: str, boundary: str) -> bool:
    """解析后的绝对路径是否仍在根边界之内（Windows 跨盘符返回 False）。"""
    try:
        root = os.path.realpath(boundary)
        return os.path.commonpath([os.path.realpath(path), root]) == root
    except ValueError:  # 不同盘符：commonpath 无法比较，一律视为越界
        return False


@dataclass
class Loader:
    """三层 POTATOCODE.md 的加载与 @include 展开。

    `max_depth` 从 1 起算：顶层 POTATOCODE.md 是第 1 层，被它 include 的文件是第 2 层。
    """

    project_root: str
    user_home: str = ""
    max_depth: int = MAX_DEPTH
    # 三类跳过的原始路径清单（调试与测试观察用）
    skipped: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.user_home:
            self.user_home = os.path.expanduser("~")

    def sources(self) -> list[tuple[str, str]]:
        """返回 `(文件路径, 根边界)` 列表，顺序即优先级顺序。"""
        root = os.path.realpath(self.project_root) if self.project_root else os.getcwd()
        potatocode_dir = os.path.join(root, ".potatocode")
        return [
            (os.path.join(root, "POTATOCODE.md"), root),
            (os.path.join(potatocode_dir, "POTATOCODE.md"), root),
            (
                os.path.join(self.user_home, ".potatocode", "POTATOCODE.md"),
                os.path.join(self.user_home, ".potatocode"),
            ),
        ]

    def load(self) -> str:
        """加载三层指令并按优先级拼接；全部缺失时返回空字符串。"""
        parts: list[str] = []
        for path, boundary in self.sources():
            text = self._load_file(path, boundary, 1, set())
            if text.strip():
                parts.append(text.strip())
        return "\n\n".join(parts)

    def _load_file(self, path: str, boundary: str, depth: int, visited: set[str]) -> str:
        """读取单个文件并展开其 `@include`；任何异常都降级为「该层为空」。"""
        try:
            real = os.path.realpath(path)
        except OSError:  # 路径本身非法：当作缺失
            return ""
        if real in visited:
            self.skipped.append(path)
            return _warning(CYCLE_WARNING, path)
        if not _within(real, boundary):
            self.skipped.append(path)
            return _warning(ESCAPE_WARNING, path)

        try:
            data = Path(real).read_bytes()
        except OSError:  # 不存在或读不了：静默跳过（F6）
            return ""
        if data[:BINARY_PROBE_BYTES].find(b"\x00") >= 0:
            self.skipped.append(path)
            return _warning(BINARY_WARNING, path)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:  # 非法 UTF-8 同样按二进制处理
            self.skipped.append(path)
            return _warning(BINARY_WARNING, path)

        visited.add(real)  # 只对「本次真的要展开」的文件记账，缺失文件不占位
        return self._expand(text, os.path.dirname(real), boundary, depth, visited)

    def _expand(
        self,
        text: str,
        base_dir: str,
        boundary: str,
        depth: int,
        visited: set[str],
    ) -> str:
        """逐行展开 `@include`：独占行才替换，其余行原样保留。"""
        out: list[str] = []
        for line in text.splitlines():
            match = INCLUDE_PATTERN.match(line)
            if match is None:
                out.append(line)
                continue
            raw = match.group(1).strip()
            if depth + 1 > self.max_depth:
                # 超深：保留原文不展开，紧跟一行警告注释（F3）
                out.append(line)
                out.append(_warning(DEPTH_WARNING, raw))
                self.skipped.append(raw)
                continue
            target = os.path.join(base_dir, raw)  # 相对当前文件所在目录解析
            out.append(self._load_file(target, boundary, depth + 1, visited))
        return "\n".join(out)


__all__ = [
    "BINARY_WARNING",
    "CYCLE_WARNING",
    "DEPTH_WARNING",
    "ESCAPE_WARNING",
    "INCLUDE_PATTERN",
    "MAX_DEPTH",
    "Loader",
]
