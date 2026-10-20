"""匹配器单测：四种类型 × 命中/不命中 × 边界条件（ch14 T2/N7）。"""

from __future__ import annotations

import pytest

from potatocode.permission.matcher import (
    ExactMatcher,
    GlobMatcher,
    NotMatcher,
    RegexMatcher,
    compile_matcher,
)


@pytest.mark.parametrize(
    ("pattern", "target", "expected"),
    [
        pytest.param("=git status", "git status", True, id="exact-hit"),
        pytest.param("=git status", "git status -s", False, id="exact-miss-extra-args"),
        pytest.param("=git status", "git  status", False, id="exact-miss-double-space"),
        pytest.param("=", "", True, id="exact-empty-value-hits-empty-string"),
        pytest.param("=*", "*", True, id="exact-literal-star-no-globbing"),
    ],
)
def test_exact_matcher(pattern: str, target: str, expected: bool) -> None:
    """`=` 前缀做整串相等，无 glob 展开。"""
    assert compile_matcher(pattern, is_command=True).match(target) is expected


@pytest.mark.parametrize(
    ("pattern", "target", "expected"),
    [
        pytest.param("git *", "git status", True, id="command-glob-hit"),
        pytest.param("git *", "npm i", False, id="command-glob-miss"),
        pytest.param("git status", "git status", True, id="command-glob-literal-hit"),
        pytest.param(
            "git *", "git status -s", True, id="command-glob-star-crosses-spaces"
        ),
        pytest.param("src/*.py", "src/a.py", True, id="path-glob-star-in-segment"),
        pytest.param("src/*.py", "src/sub/a.py", False, id="path-glob-star-not-cross-slash"),
        pytest.param("src/**", "src/a/b.py", True, id="path-glob-doublestar-crosses"),
        pytest.param("**/*.py", "src/pkg/a.py", True, id="path-glob-lead-doublestar"),
        pytest.param("**/*.py", "a.py", True, id="path-glob-doublestar-zero-segments"),
    ],
)
def test_glob_matcher(pattern: str, target: str, expected: bool) -> None:
    """无前缀沿用 glob 语义：is_command 决定整串通配或路径分段。"""
    command_mode = "/" not in pattern  # 命令串通常不含路径分隔符
    assert compile_matcher(pattern, is_command=command_mode).match(target) is expected


@pytest.mark.parametrize(
    ("pattern", "target", "expected"),
    [
        pytest.param(r"~^npm (install|test)$", "npm install", True, id="regex-hit-install"),
        pytest.param(r"~^npm (install|test)$", "npm test", True, id="regex-hit-test"),
        pytest.param(r"~^npm (install|test)$", "npm run dev", False, id="regex-miss-run-dev"),
        pytest.param("~delete", "please DELETE it", False, id="regex-case-sensitive"),
        pytest.param("~delete", "please delete it", True, id="regex-search-anywhere"),
    ],
)
def test_regex_matcher(pattern: str, target: str, expected: bool) -> None:
    """`~` 前缀做 re.search 语义匹配。"""
    assert compile_matcher(pattern, is_command=False).match(target) is expected


def test_regex_matcher_invalid_pattern_raises() -> None:
    """非法正则在加载期编译失败抛 ValueError（ch14 F3/F4）。"""
    with pytest.raises(ValueError, match="invalid regex"):
        compile_matcher("~[invalid", is_command=False)


@pytest.mark.parametrize(
    ("pattern", "target", "expected"),
    [
        pytest.param("!=foo", "foo", False, id="not-exact-miss-target-equals"),
        pytest.param("!=foo", "bar", True, id="not-exact-hit-target-differs"),
        pytest.param("!~^rm", "rm -rf .", False, id="not-regex-miss-rm-prefixed"),
        pytest.param("!~^rm", "ls -lh", True, id="not-regex-hit-not-rm-prefixed"),
        pytest.param("!git *", "git status", False, id="not-glob-miss-git-command"),
        pytest.param("!git *", "npm install", True, id="not-glob-hit-non-git"),
    ],
)
def test_not_matcher(pattern: str, target: str, expected: bool) -> None:
    """`!` 前缀对内层描述串解析结果取反（支持 != / !~ / !glob）。"""
    assert compile_matcher(pattern, is_command=True).match(target) is expected


def test_not_matcher_nested_double_negation() -> None:
    """嵌套反向：`!!git status` 等价 `=git status`。"""
    matcher = compile_matcher("!!git status", is_command=True)
    assert isinstance(matcher, NotMatcher)
    assert isinstance(matcher.inner, NotMatcher)
    assert matcher.match("git status") is True
    assert matcher.match("git push") is False


def test_empty_pattern_raises() -> None:
    """空描述串抛 ValueError（ch14 N7 边界）。"""
    with pytest.raises(ValueError, match="empty matcher pattern"):
        compile_matcher("", is_command=False)


def test_str_roundtrip_prefixes() -> None:
    """__str__ 还原带前缀描述串（调试与 /hooks 输出用）。"""
    assert str(compile_matcher("=foo", is_command=False)) == "=foo"
    assert str(compile_matcher("git *", is_command=True)) == "git *"
    assert str(compile_matcher(r"~^a", is_command=False)) == r"~^a"
    assert str(compile_matcher("!=foo", is_command=False)) == "!=foo"


def test_matcher_instances_are_frozen() -> None:
    """四种实现均为不可变实例，可在加载期构造后安全复用（ch14 F15）。"""
    import re

    matchers = (
        ExactMatcher("x"),
        GlobMatcher("x*", is_command=True),
        RegexMatcher("x", re.compile("x")),
        NotMatcher(ExactMatcher("x")),
    )
    for matcher in matchers:
        with pytest.raises(Exception):  # noqa: B017 - dataclasses.FrozenInstanceError
            matcher.value = "y"  # type: ignore[misc]
