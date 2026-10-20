"""配置加载与映射单测：friendly_name/categorize/extract_target/load_settings（F4/N5/N7）。"""

from __future__ import annotations

import json

import pytest

from potatocode.llm import ToolCall
from potatocode.permission.settings import (
    SettingsError,
    categorize,
    extract_target,
    friendly_name,
    load_settings,
    to_rule_set,
)
from potatocode.permission.types import Category


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(id="x", name=name, input=json.dumps(args))


def test_friendly_name():
    assert friendly_name("bash") == "Bash"
    assert friendly_name("read_file") == "Read"
    assert friendly_name("write_file") == "Write"
    assert friendly_name("edit_file") == "Edit"
    assert friendly_name("glob") == "Glob"
    assert friendly_name("grep") == "Grep"
    assert friendly_name("unknown") == "unknown"


def test_categorize():
    assert categorize("read_file", False) is Category.READ
    assert categorize("glob", False) is Category.READ
    assert categorize("grep", False) is Category.READ
    assert categorize("write_file", False) is Category.WRITE
    assert categorize("edit_file", False) is Category.WRITE
    assert categorize("bash", False) is Category.EXEC
    assert categorize("unknown_tool", False) is Category.EXEC  # N7 最严
    assert categorize("unknown_tool", True) is Category.READ  # read_only 优先


def test_extract_target_file():
    target, is_file, ok = extract_target(_call("read_file", {"path": "a.py"}))
    assert (target, is_file, ok) == ("a.py", True, True)


def test_extract_target_file_missing_path():
    target, is_file, ok = extract_target(_call("write_file", {"content": "x"}))
    assert (target, is_file, ok) == ("", True, False)


def test_extract_target_glob_empty_path():
    target, is_file, ok = extract_target(_call("glob", {"pattern": "*.py"}))
    assert (target, is_file, ok) == (".", True, True)


def test_extract_target_bash():
    target, is_file, ok = extract_target(_call("bash", {"command": "git status"}))
    assert (target, is_file, ok) == ("git status", False, True)


def test_extract_target_bad_json():
    call = ToolCall(id="x", name="read_file", input="not json")
    target, is_file, ok = extract_target(call)
    assert ok is False


def test_extract_target_unknown_tool():
    target, is_file, ok = extract_target(_call("mystery", {}))
    assert (target, is_file, ok) == ("", False, False)


def test_load_settings_missing_file(tmp_path):
    """文件缺失返回空 Settings，不抛。"""
    s = load_settings(str(tmp_path / "nope.yaml"))
    assert s.default_mode == ""
    assert s.permissions.allow == []
    assert s.permissions.deny == []


def test_load_settings_invalid_yaml(tmp_path):
    """非法 YAML 抛 SettingsError（N5）。"""
    p = tmp_path / "settings.yaml"
    p.write_text("allow: [unclosed\n", encoding="utf-8")
    with pytest.raises(SettingsError):
        load_settings(str(p))


def test_load_settings_valid(tmp_path):
    p = tmp_path / "settings.yaml"
    yaml_text = (
        "default_mode: acceptEdits\n"
        "permissions:\n"
        '  allow: ["Bash(git *)"]\n'
        '  deny: ["Read(.env)"]\n'
    )
    p.write_text(yaml_text, encoding="utf-8")
    s = load_settings(str(p))
    assert s.default_mode == "acceptEdits"
    assert s.permissions.allow == ["Bash(git *)"]
    assert s.permissions.deny == ["Read(.env)"]


def test_to_rule_set_skips_invalid(tmp_path, capsys):
    """非法规则跳过、其余正常加载（既有行为保持）。"""
    p = tmp_path / "settings.yaml"
    p.write_text(
        'permissions:\n  allow: ["Bash(git *)", "(bad"]\n  deny: ["Read(.env)"]\n', encoding="utf-8"
    )
    s = load_settings(str(p))
    rs = to_rule_set(s)
    assert [r.raw for r in rs.allow] == ["git *"]
    assert [r.tool for r in rs.deny] == ["Read"]
    assert rs.deny[0].allow is False


def test_to_rule_set_reports_parse_failure_to_stderr(tmp_path, capsys):
    """解析失败的规则 stderr 打印原文与原因，其余规则不受影响（ch14 F4/T5）。"""
    p = tmp_path / "settings.yaml"
    p.write_text(
        'permissions:\n  allow: ["Bash(~[broken", "Bash(git *)"]\n', encoding="utf-8"
    )
    s = load_settings(str(p))
    rs = to_rule_set(s)
    captured = capsys.readouterr()
    assert "parse failed" in captured.err
    assert "Bash(~[broken" in captured.err
    assert [r.raw for r in rs.allow] == ["git *"]  # 好规则不受坏规则影响
