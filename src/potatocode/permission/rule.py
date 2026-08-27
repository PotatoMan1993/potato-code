"""规则与匹配（F3）：以「工具名(模式)」声明 allow/deny，支持精确与 glob 匹配。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .types import Decision


@dataclass
class Rule:
    """单条规则：友好工具名 + 模式段；pattern 为空表示匹配该工具全部调用。"""

    tool: str  # 友好名：Bash/Read/Write/Edit/Glob/Grep
    pattern: str  # 模式段；"" 表示全匹配
    allow: bool  # True=allow, False=deny


@dataclass
class RuleSet:
    """一份规则集：allow 与 deny 两列（F4 同层 deny 优先 allow）。"""

    allow: list[Rule] = field(default_factory=list)
    deny: list[Rule] = field(default_factory=list)

    def find(self, friendly: str, target: str) -> Rule | None:
        """返回命中规则：先 deny 再 allow；未命中返回 None。"""
        for r in self.deny:
            if r.tool == friendly and match_pattern(r.pattern, target):
                return r
        for r in self.allow:
            if r.tool == friendly and match_pattern(r.pattern, target):
                return r
        return None

    def match(self, friendly: str, target: str) -> tuple[Decision, bool]:
        """先 deny 再 allow；返回 (Allow|Deny, 命中?)。"""
        r = self.find(friendly, target)
        if r is None:
            return Decision.ALLOW, False
        return (Decision.DENY if not r.allow else Decision.ALLOW), True


def parse_rule(s: str) -> tuple[Rule, bool]:
    """解析 `Tool(pattern)` 或 `Tool` 为 Rule。

    非法（空、括号不配对、工具名为空）返回 (Rule("","",False), False)。
    """
    s = (s or "").strip()
    if not s:
        return Rule("", "", False), False
    if s.endswith(")"):
        idx = s.find("(")
        if idx <= 0:
            return Rule("", "", False), False
        tool = s[:idx].strip()
        pattern = s[idx + 1 : -1].strip()
        if not tool:
            return Rule("", "", False), False
        return Rule(tool, pattern, True), True
    if "(" in s or ")" in s:
        # 括号不配对
        return Rule("", "", False), False
    tool = s.strip()
    if not tool:
        return Rule("", "", False), False
    return Rule(tool, "", True), True


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


__all__ = ["Rule", "RuleSet", "match_pattern", "parse_rule"]
