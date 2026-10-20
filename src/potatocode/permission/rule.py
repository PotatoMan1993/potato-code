"""规则与匹配（F3/ch14 F1-F5）：以「工具名(模式)」声明 allow/deny。

模式段在 ch14 升级为结构化 Matcher（见 matcher.py）：``=精确`` / ``~正则`` /
``!反向`` 前缀 + 缺省 glob，四种语义共用一套实现；pattern 为空表示匹配该工具
全部调用。Allow/Deny 判定语义与 ch08 一致，仅匹配表达力扩展。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .types import Decision

if TYPE_CHECKING:
    from .matcher import Matcher


@dataclass
class Rule:
    """单条规则：友好工具名 + 匹配器；matcher 为 None 表示匹配该工具全部调用。"""

    tool: str  # 友好名：Bash/Read/Write/Edit/Glob/Grep
    matcher: Matcher | None  # None 表示全匹配（原空 pattern 语义）
    allow: bool  # True=allow, False=deny
    raw: str  # 原始模式描述串，仅供错误日志与调试展示


@dataclass
class RuleSet:
    """一份规则集：allow 与 deny 两列（F4 同层 deny 优先 allow）。"""

    allow: list[Rule] = field(default_factory=list)
    deny: list[Rule] = field(default_factory=list)

    def find(self, friendly: str, target: str) -> Rule | None:
        """返回命中规则：先 deny 再 allow；未命中返回 None。"""
        for r in self.deny:
            if r.tool == friendly and match_rule(r, target):
                return r
        for r in self.allow:
            if r.tool == friendly and match_rule(r, target):
                return r
        return None

    def match(self, friendly: str, target: str) -> tuple[Decision, bool]:
        """先 deny 再 allow；返回 (Allow|Deny, 命中?)。"""
        r = self.find(friendly, target)
        if r is None:
            return Decision.ALLOW, False
        return (Decision.DENY if not r.allow else Decision.ALLOW), True


def match_rule(r: Rule, target: str) -> bool:
    """单条规则匹配：全匹配规则恒 True，其余交给 matcher（ch14 T3）。"""
    if r.matcher is None:
        return True
    return r.matcher.match(target)


def parse_rule(s: str) -> tuple[Rule | None, str | None]:
    """解析 `Tool(pattern)` 或 `Tool` 为 Rule（ch14 T3）。

    pattern 支持四种类型前缀（=精确 / ~正则 / !反向 / 缺省 glob），由
    `compile_matcher` 统一编译；空 pattern 仍表示全匹配（matcher=None）。

    成功返回 (Rule, None)；失败返回 (None, 错误描述)——调用方负责 stderr 报告
    并跳过该条（ch14 F4），不再静默。
    """
    s = (s or "").strip()
    if not s:
        return None, "规则串为空"
    pattern = ""
    if s.endswith(")"):
        idx = s.find("(")
        if idx <= 0:
            return None, f"括号不配对: {s!r}"
        tool = s[:idx].strip()
        pattern = s[idx + 1 : -1].strip()
        if not tool:
            return None, f"工具名为空: {s!r}"
    elif "(" in s or ")" in s:
        # 括号不配对
        return None, f"括号不配对: {s!r}"
    else:
        tool = s.strip()
        if not tool:
            return None, "规则串为空"

    if not pattern:
        return Rule(tool=tool, matcher=None, allow=True, raw=""), None
    try:
        from .matcher import compile_matcher

        # 命令类工具（Bash）走整串通配，其余按路径分段（与 ch08 语义一致）
        matcher = compile_matcher(pattern, is_command=(tool == "Bash"))
    except ValueError as e:
        return None, str(e)
    return Rule(tool=tool, matcher=matcher, allow=True, raw=pattern), None


def _glob_regex(pattern: str) -> str:
    """把 glob 模式编译为正则：`*` → `.*`；`\\` 转义下一字符为字面；其余字面。"""
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\" and i + 1 < n:
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if c == "*":
            out.append(".*")
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _match_segments(pat: list[str], tgt: list[str]) -> bool:
    """按 `/` 分段匹配：`**` 跨段、`*` 段内、其余字面（含段内 `*`）。"""
    memo: dict[tuple[int, int], bool] = {}

    def rec(i: int, j: int) -> bool:
        key = (i, j)
        if key in memo:
            return memo[key]
        if i == len(pat):
            res = j == len(tgt)
        else:
            p = pat[i]
            if p == "**":
                res = rec(i + 1, j) or (j < len(tgt) and rec(i, j + 1))
            elif j < len(tgt) and re.fullmatch(_glob_regex(p), tgt[j]) is not None:
                res = rec(i + 1, j + 1)
            else:
                res = False
        memo[key] = res
        return res

    return rec(0, 0)


def match_pattern(pattern: str, target: str, *, path: bool | None = None) -> bool:
    """glob 匹配：`pattern == ""` 恒 True。

    path=True 按 `/` 分段（`**` 跨段、`*` 段内）；否则命令串整串匹配
    （`*` 匹配任意字符含空格，`**` 等价 `*`）。未指定 path 时按是否含 `/` 自判。
    """
    if pattern == "":
        return True
    if path is None:
        path = "/" in pattern or "/" in target
    if path:
        return _match_segments(pattern.split("/"), target.split("/"))
    return re.fullmatch(_glob_regex(pattern), target) is not None


__all__ = ["Rule", "RuleSet", "match_pattern", "match_rule", "parse_rule"]
