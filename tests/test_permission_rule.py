"""规则与匹配单测：解析、glob 匹配、同层 deny 优先（AC3/AC5/F3）。"""

from __future__ import annotations

from potatocode.permission.rule import Rule, RuleSet, match_pattern, parse_rule
from potatocode.permission.types import Decision


def test_parse_rule_with_pattern():
    """解析 `Bash(git *)`。"""
    rule, ok = parse_rule("Bash(git *)")
    assert ok
    assert rule.tool == "Bash"
    assert rule.pattern == "git *"
    assert rule.allow


def test_parse_rule_bare():
    """解析 `Read`（无模式段 = 全匹配）。"""
    rule, ok = parse_rule("Read")
    assert ok
    assert rule.tool == "Read"
    assert rule.pattern == ""


def test_parse_rule_invalid():
    """非法输入返回 ok=False。"""
    for bad in ("", "  ", "Bash(git", "Bash)", "()"):
        _, ok = parse_rule(bad)
        assert not ok, bad


def test_match_command_glob():
    """命令 glob：`*` 匹配任意字符（含空格）。"""
    assert match_pattern("git *", "git status")
    assert not match_pattern("git *", "npm i")
    assert match_pattern("git status", "git status")
    assert not match_pattern("git status", "git push")


def test_match_path_glob():
    """文件路径 glob：`**` 跨段、`*` 段内。"""
    assert match_pattern("src/**", "src/a/b.py")
    assert not match_pattern("src/**", "docs/x")
    assert match_pattern("src/*.py", "src/a.py")
    assert not match_pattern("src/*.py", "src/sub/a.py")


def test_match_empty_pattern_matches_all():
    """pattern == "" 恒匹配。"""
    assert match_pattern("", "anything at all")


def test_ruleset_deny_priority():
    """同层 deny 与 allow 同时命中 → deny 优先（AC5/F4）。"""
    rs = RuleSet(
        allow=[Rule("Bash", "git *", True)],
        deny=[Rule("Bash", "git push", False)],
    )
    decision, hit = rs.match("Bash", "git push")
    assert hit
    assert decision is Decision.DENY
    decision, hit = rs.match("Bash", "git status")
    assert hit
    assert decision is Decision.ALLOW


def test_ruleset_no_match():
    """未命中返回 (ALLOW, False)。"""
    rs = RuleSet(allow=[Rule("Bash", "git *", True)])
    decision, hit = rs.match("Bash", "npm install")
    assert not hit
    assert decision is Decision.ALLOW
