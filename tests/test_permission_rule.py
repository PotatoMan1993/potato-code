"""规则与匹配单测：解析、四种 matcher 语义、同层 deny 优先（AC3/AC5/F3/ch14 F1-F5）。"""

from __future__ import annotations

from dataclasses import replace

from potatocode.permission.matcher import ExactMatcher, GlobMatcher, RegexMatcher
from potatocode.permission.rule import RuleSet, match_pattern, parse_rule
from potatocode.permission.types import Decision


def test_parse_rule_with_pattern():
    """解析 `Bash(git *)`：缺省 glob 语义（ch14 F1 向后兼容）。"""
    rule, err = parse_rule("Bash(git *)")
    assert err is None
    assert rule is not None
    assert rule.tool == "Bash"
    assert isinstance(rule.matcher, GlobMatcher)
    assert rule.raw == "git *"
    assert rule.allow


def test_parse_rule_bare():
    """解析 `Read`（无模式段 = 全匹配，matcher=None）。"""
    rule, err = parse_rule("Read")
    assert err is None
    assert rule is not None
    assert rule.tool == "Read"
    assert rule.matcher is None
    assert rule.raw == ""


def test_parse_rule_invalid():
    """非法输入返回 (None, 错误描述)（ch14 F4 不再静默）。"""
    for bad in ("", "  ", "Bash(git", "Bash)", "()"):
        rule, err = parse_rule(bad)
        assert rule is None, bad
        assert err, bad


def test_parse_rule_exact():
    """`Bash(=git status)` 解析为精确 matcher（ch14 F2/T5）。"""
    rule, err = parse_rule("Bash(=git status)")
    assert err is None
    assert rule is not None
    assert isinstance(rule.matcher, ExactMatcher)
    assert rule.matcher.match("git status")
    assert not rule.matcher.match("git status -s")


def test_parse_rule_regex():
    """`Bash(~^npm.*)` 解析为正则 matcher（ch14 T5）。"""
    rule, err = parse_rule("Bash(~^npm.*)")
    assert err is None
    assert rule is not None
    assert isinstance(rule.matcher, RegexMatcher)
    assert rule.matcher.match("npm install")


def test_parse_rule_not_regex():
    """`Bash(!~^rm)` 解析为反向正则 matcher（ch14 T5）。"""
    rule, err = parse_rule("Bash(!~^rm)")
    assert err is None
    assert rule is not None
    assert rule.matcher is not None
    assert rule.matcher.match("ls -lh")
    assert not rule.matcher.match("rm -rf .")


def test_parse_rule_regex_compile_failure():
    """正则编译失败返回 (None, 错误描述)（ch14 F3/F4）。"""
    rule, err = parse_rule("Bash(~[invalid)")
    assert rule is None
    assert err is not None and "invalid regex" in err


def test_parse_rule_path_glob_unchanged():
    """`Write(**/*.py)` 沿用路径 glob 语义（ch14 F5 向后兼容）。"""
    rule, err = parse_rule("Write(**/*.py)")
    assert err is None
    assert rule is not None
    assert isinstance(rule.matcher, GlobMatcher)
    assert rule.matcher.match("src/pkg/a.py")
    assert not rule.matcher.match("notes/readme.md")


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


def _ruleset_with_exact_and_glob() -> RuleSet:
    glob_rule, _ = parse_rule("Bash(git *)")
    assert glob_rule is not None
    exact_rule, _ = parse_rule("Bash(=git push)")
    assert exact_rule is not None
    # deny 列表里的规则要像 to_rule_set 一样翻转 allow 标记
    return RuleSet(allow=[glob_rule], deny=[replace(exact_rule, allow=False)])


def test_ruleset_deny_priority():
    """同层 deny 与 allow 同时命中 → deny 优先（AC5/F4）。"""
    rs = _ruleset_with_exact_and_glob()
    decision, hit = rs.match("Bash", "git push")
    assert hit
    assert decision is Decision.DENY
    decision, hit = rs.match("Bash", "git status")
    assert hit
    assert decision is Decision.ALLOW


def test_ruleset_exact_rule_does_not_hit_extra_args():
    """精确规则不做泛化：`=git status` 不命中 `git status -s`（AC1 语义）。"""
    exact_rule, _ = parse_rule("Bash(=git status)")
    assert exact_rule is not None
    rs = RuleSet(allow=[exact_rule])
    _, hit = rs.match("Bash", "git status")
    assert hit
    _, hit = rs.match("Bash", "git status -s")
    assert not hit


def test_ruleset_no_match():
    """未命中返回 (ALLOW, False)。"""
    glob_rule, _ = parse_rule("Bash(git *)")
    assert glob_rule is not None
    rs = RuleSet(allow=[glob_rule])
    decision, hit = rs.match("Bash", "npm install")
    assert not hit
    assert decision is Decision.ALLOW
