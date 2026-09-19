"""tool 包单测：注册中心与 6 个核心工具（AC1–AC6）。"""

from __future__ import annotations

import json
import sys

import pytest

from potatocode.tool import Registry, new_default_registry
from potatocode.tool.bash import BashTool
from potatocode.tool.edit_file import EditFileTool
from potatocode.tool.glob_tool import GlobTool
from potatocode.tool.grep_tool import GrepTool
from potatocode.tool.read_file import ReadFileTool
from potatocode.tool.write_file import WriteFileTool

# ────────── 注册中心 ──────────


def test_registry_definitions_ordered():
    """导出恰好 6 条定义、按注册顺序、可按键名查找（AC1/F1）。"""
    reg = new_default_registry()
    defs = reg.definitions()
    assert len(defs) == 6
    names = [d.name for d in defs]
    assert names == ["read_file", "write_file", "edit_file", "bash", "glob", "grep"]
    for d in defs:
        assert d.input_schema["type"] == "object"
    assert reg.get("read_file") is not None
    assert reg.get("read_file").name() == "read_file"  # type: ignore[union-attr]
    assert reg.get("no_such_tool") is None


def test_registry_duplicate_register_raises():
    """重名注册抛 ValueError，不静默覆盖。"""
    reg = Registry()
    reg.register(ReadFileTool())
    with pytest.raises(ValueError):
        reg.register(ReadFileTool())


async def test_registry_execute_unknown_tool():
    """未知工具名返回结构化错误而非崩溃（F9/N4）。"""
    reg = new_default_registry()
    r = await reg.execute("no_such_tool", "{}")
    assert r.is_error
    assert "未知工具" in r.content


def test_read_only_classification():
    """只读分类：read_file/glob/grep 只读；write_file/edit_file/bash 有副作用（F5）。"""
    assert ReadFileTool().read_only() is True
    assert GlobTool().read_only() is True
    assert GrepTool().read_only() is True
    assert WriteFileTool().read_only() is False
    assert EditFileTool().read_only() is False
    assert BashTool().read_only() is False


def test_read_only_definitions():
    """Plan Mode 只导出只读工具定义，保留注册顺序（F10）。"""
    reg = new_default_registry()
    defs = reg.read_only_definitions()
    assert [d.name for d in defs] == ["read_file", "glob", "grep"]


def test_is_read_only():
    """分批判定：已注册只读工具为 True；有副作用与未知工具为 False（F5）。"""
    reg = new_default_registry()
    assert reg.is_read_only("read_file") is True
    assert reg.is_read_only("grep") is True
    assert reg.is_read_only("bash") is False
    assert reg.is_read_only("no_such_tool") is False


# ────────── read_file ──────────


async def test_read_file_with_line_numbers(tmp_path):
    """读文件带行号输出内容（AC2）。"""
    p = tmp_path / "hello.txt"
    p.write_text("alpha\nbeta\n", encoding="utf-8")
    r = await ReadFileTool().execute(json.dumps({"path": str(p)}))
    assert not r.is_error
    assert "     1\talpha" in r.content
    assert "     2\tbeta" in r.content
    observation = r.file_observation
    assert observation is not None
    assert observation.operation == "read"
    assert observation.delivered_text == r.content
    assert observation.complete
    assert (observation.start_line, observation.end_line) == (1, 2)


async def test_read_file_missing_is_error(tmp_path):
    """读不存在文件返回结构化错误而非崩溃（AC2/F9）。"""
    r = await ReadFileTool().execute(json.dumps({"path": str(tmp_path / "nope.txt")}))
    assert r.is_error
    assert "不存在" in r.content
    assert r.file_observation is None


async def test_read_file_directory_is_error(tmp_path):
    """读目录返回结构化错误。"""
    r = await ReadFileTool().execute(json.dumps({"path": str(tmp_path)}))
    assert r.is_error
    assert "目录" in r.content


# ────────── write_file ──────────


async def test_write_file_creates_nested_dirs(tmp_path):
    """写文件覆盖落地、父目录自动创建（AC3）。"""
    target = tmp_path / "a" / "b" / "c.txt"
    r = await WriteFileTool().execute(json.dumps({"path": str(target), "content": "hello\n"}))
    assert not r.is_error
    assert target.read_text(encoding="utf-8") == "hello\n"
    assert r.file_observation is not None
    assert r.file_observation.operation == "write"
    assert r.file_observation.delivered_text is None


# ────────── edit_file ──────────


async def test_edit_file_unique_replace(tmp_path):
    """唯一匹配时替换成功（AC4）。"""
    p = tmp_path / "f.txt"
    p.write_text("a ab b", encoding="utf-8")
    r = await EditFileTool().execute(
        json.dumps({"path": str(p), "old_string": "ab", "new_string": "XY"})
    )
    assert not r.is_error
    assert p.read_text(encoding="utf-8") == "a XY b"
    assert r.file_observation is not None
    assert r.file_observation.operation == "edit"


async def test_edit_file_zero_match(tmp_path):
    """匹配 0 处返回可区分错误（AC4）。"""
    p = tmp_path / "f.txt"
    p.write_text("hello", encoding="utf-8")
    r = await EditFileTool().execute(
        json.dumps({"path": str(p), "old_string": "zzz", "new_string": "x"})
    )
    assert r.is_error
    assert "未找到" in r.content
    assert r.file_observation is None


async def test_read_observation_records_partial_range_and_tool_truncation(tmp_path):
    """局部读取和工具字符截断不会被误称为完整文件。"""
    path = tmp_path / "SKILL.md"
    path.write_text("first\nsecond\nthird", encoding="utf-8")
    result = await ReadFileTool().execute(json.dumps({"path": str(path), "offset": 1, "limit": 1}))
    observation = result.file_observation
    assert observation is not None
    assert not observation.complete and not observation.truncated
    assert (observation.start_line, observation.end_line) == (2, 2)
    path.write_text("x" * 300_000, encoding="utf-8")
    result = await ReadFileTool().execute(json.dumps({"path": str(path)}))
    observation = result.file_observation
    assert observation is not None
    assert observation.truncated and not observation.complete
    assert observation.delivered_text is not None
    assert len(observation.delivered_text) == 256 * 1024
    assert result.content == observation.delivered_text + "\n[truncated]"


async def test_edit_file_multi_match(tmp_path):
    """匹配多于 1 处返回含匹配数的可区分错误（AC4）。"""
    p = tmp_path / "f.txt"
    p.write_text("abab", encoding="utf-8")
    r = await EditFileTool().execute(
        json.dumps({"path": str(p), "old_string": "ab", "new_string": "x"})
    )
    assert r.is_error
    assert "2 处" in r.content
    assert "不唯一" in r.content


# ────────── bash ──────────


async def test_bash_echo():
    """bash 返回 stdout 与退出码（AC5）。"""
    r = await BashTool().execute(json.dumps({"command": "echo hi"}))
    assert not r.is_error
    assert "hi" in r.content
    assert "exit_code: 0" in r.content


async def test_bash_timeout():
    """超时命令被终止并返回结构化超时结果（AC5/N1）。"""
    reg = new_default_registry()
    cmd = f'"{sys.executable}" -c "import time; time.sleep(5)"'
    r = await reg.execute("bash", json.dumps({"command": cmd}), timeout=0.5)
    assert r.is_error
    assert "超时" in r.content


async def test_bash_nonzero_exit_not_error():
    """非零退出按结果回灌（不设 is_error），由模型自行判断（F9）。"""
    r = await BashTool().execute(json.dumps({"command": "exit 3"}))
    assert not r.is_error
    assert "exit_code: 3" in r.content


# ────────── glob ──────────


async def test_glob_py_files(tmp_path):
    """glob `**/*.py` 列出匹配文件（AC6）。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "b.py").write_text("y", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("z", encoding="utf-8")
    r = await GlobTool().execute(json.dumps({"pattern": "**/*.py", "path": str(tmp_path)}))
    assert not r.is_error
    files = r.content.splitlines()
    assert "b.py" in files
    assert "src\\a.py" in files or "src/a.py" in files


async def test_glob_no_match(tmp_path):
    """无匹配返回空说明（非 is_error）。"""
    r = await GlobTool().execute(json.dumps({"pattern": "**/*.xyz", "path": str(tmp_path)}))
    assert not r.is_error
    assert r.content == "无匹配"


# ────────── grep ──────────


async def test_grep_keyword(tmp_path):
    """grep 返回 file:line:content 命中（AC6）。"""
    (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("# nothing here\n", encoding="utf-8")
    r = await GrepTool().execute(json.dumps({"pattern": "def foo", "path": str(tmp_path)}))
    assert not r.is_error
    assert "a.py:1:def foo():" in r.content


async def test_grep_invalid_regex():
    """非法正则返回结构化错误（F9/N4）。"""
    r = await GrepTool().execute(json.dumps({"pattern": "[", "path": "."}))
    assert r.is_error
    assert "正则非法" in r.content


async def test_grep_no_match(tmp_path):
    """无命中返回空说明（非 is_error）。"""
    r = await GrepTool().execute(json.dumps({"pattern": "zzz_no_such", "path": str(tmp_path)}))
    assert not r.is_error
    assert r.content == "无命中"
