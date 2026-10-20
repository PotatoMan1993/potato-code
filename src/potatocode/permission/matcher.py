"""匹配器抽象：权限规则与 Hook 条件共用的四种匹配语义（ch14 F1-F3）。

形态上把 ch08 的单一通配匹配升级为结构化 Matcher——`compile_matcher` 解析
带类型前缀的描述串：

- ``=value``   精确匹配（整串相等）
- ``~regex``   正则匹配（``re.search`` 语义）
- ``!inner``   反向匹配（对内层描述串解析结果取反，支持 ``!=v`` / ``!~re`` / ``!glob``）
- ``value``    glob 通配（缺省类型，沿用 ch08 既有语义保证向后兼容）

每种实现都是不可变的 frozen dataclass，运行期实例可在加载期一次构造、反复复用
（ch14 F15）；Hook 条件与权限规则共用同一套实现，避免两套匹配语义漂移（ch14 N7）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .rule import match_pattern


@runtime_checkable
class Matcher(Protocol):
    """规则匹配的统一接口；四种实现：ExactMatcher / GlobMatcher / RegexMatcher / NotMatcher。"""

    def match(self, s: str) -> bool:
        """判定目标串是否命中本匹配器。"""
        ...

    def __str__(self) -> str:
        """还原为带类型前缀的描述串（调试与 /hooks 输出用）。"""
        ...


@dataclass(frozen=True)
class ExactMatcher:
    """精确匹配：目标串与 value 整串相等才算命中（ch14 F3）。"""

    value: str

    def match(self, s: str) -> bool:
        return s == self.value

    def __str__(self) -> str:
        return f"={self.value}"


@dataclass(frozen=True)
class GlobMatcher:
    """glob 通配匹配：缺省类型，沿用 ch08 既有语义（ch14 F2/F3）。

    is_command=True 时按命令串整串通配（``*`` 可跨空格）；False 时按路径
    分段通配（``**`` 跨段、``*`` 段内）。
    """

    pattern: str
    is_command: bool  # True 走整串通配（命令语义），False 走路径分段

    def match(self, s: str) -> bool:
        return match_pattern(self.pattern, s, path=not self.is_command)

    def __str__(self) -> str:
        return self.pattern


@dataclass(frozen=True)
class RegexMatcher:
    """正则匹配：``re.search`` 语义，目标串任意位置命中即算（ch14 F2）。

    compiled 在加载期一次编译并随实例缓存，运行期零编译开销（ch14 F3）。
    """

    src: str
    compiled: re.Pattern[str]

    def match(self, s: str) -> bool:
        return self.compiled.search(s) is not None

    def __str__(self) -> str:
        return f"~{self.src}"


@dataclass(frozen=True)
class NotMatcher:
    """反向匹配：对内层匹配器取反的一元包装（ch14 F2/F3）。"""

    inner: Matcher

    def match(self, s: str) -> bool:
        return not self.inner.match(s)

    def __str__(self) -> str:
        return f"!{self.inner}"


def compile_matcher(pattern: str, *, is_command: bool) -> Matcher:
    """解析单条匹配描述串，返回对应 Matcher；非法描述串抛 ValueError。

    描述串规则（ch14 F2）：

    - ``=value`` -> ExactMatcher
    - ``~regex`` -> RegexMatcher（正则编译失败转 ValueError）
    - ``!inner`` -> NotMatcher（递归解析 inner，支持 ``!=v`` / ``!~re`` / ``!glob``）
    - ``value``  -> GlobMatcher（无前缀沿用 glob 语义）

    Bash 等命令类工具传 is_command=True 走整串通配；路径类字段传 False。
    """
    if not pattern:
        raise ValueError("empty matcher pattern")
    head, rest = pattern[0], pattern[1:]
    if head == "=":
        return ExactMatcher(rest)
    if head == "~":
        try:
            return RegexMatcher(rest, re.compile(rest))
        except re.error as e:
            raise ValueError(f"invalid regex {rest!r}: {e}") from e
    if head == "!":
        return NotMatcher(compile_matcher(rest, is_command=is_command))
    return GlobMatcher(pattern, is_command)


__all__ = [
    "ExactMatcher",
    "GlobMatcher",
    "Matcher",
    "NotMatcher",
    "RegexMatcher",
    "compile_matcher",
]
