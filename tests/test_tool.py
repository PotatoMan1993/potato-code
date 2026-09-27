"""tool 包单测：注册中心与 6 个核心工具（AC1–AC6）。"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from potatocode.tool import Registry, new_default_registry
from potatocode.tool.bash import BashTool
from potatocode.tool.edit_file import EditFileTool
from potatocode.tool.glob_tool import GlobTool
from potatocode.tool.grep_tool import _MAX_LINE_CHARS, GrepTool, _iter_lines
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


async def test_read_file_missing_is_error(tmp_path):
    """读不存在文件返回结构化错误而非崩溃（AC2/F9）。"""
    r = await ReadFileTool().execute(json.dumps({"path": str(tmp_path / "nope.txt")}))
    assert r.is_error
    assert "不存在" in r.content


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


async def test_edit_file_zero_match(tmp_path):
    """匹配 0 处返回可区分错误（AC4）。"""
    p = tmp_path / "f.txt"
    p.write_text("hello", encoding="utf-8")
    r = await EditFileTool().execute(
        json.dumps({"path": str(p), "old_string": "zzz", "new_string": "x"})
    )
    assert r.is_error
    assert "未找到" in r.content


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


async def test_glob_excludes_noise_dirs(tmp_path):
    """默认排除隐藏目录与固定噪声目录，配额留给项目自身（ch09 F1/AC1/AC2）。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "v.py").write_text("x", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "c.py").write_text("x", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "n.py").write_text("x", encoding="utf-8")

    r = await GlobTool().execute(json.dumps({"pattern": "**/*.py", "path": str(tmp_path)}))
    assert not r.is_error
    files = r.content.splitlines()
    assert "src\\a.py" in files or "src/a.py" in files
    assert len(files) == 1  # 噪声目录的文件一条都不占配额


async def test_glob_escape_hatch(tmp_path):
    """搜索根指向被排除目录时能搜进去，但根之下的被排除子目录仍排除（ch09 F2/AC4）。"""
    venv = tmp_path / ".venv"
    (venv / "lib").mkdir(parents=True)
    (venv / "lib" / "v.py").write_text("x", encoding="utf-8")
    (venv / "lib" / "__pycache__").mkdir()
    (venv / "lib" / "__pycache__" / "c.py").write_text("x", encoding="utf-8")

    r = await GlobTool().execute(json.dumps({"pattern": "**/*.py", "path": str(venv)}))
    assert not r.is_error
    files = r.content.splitlines()
    assert "lib\\v.py" in files or "lib/v.py" in files  # 根自身不再被判定
    assert not any("__pycache__" in f for f in files)  # 根之下照常生效


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


async def test_grep_excludes_noise_dirs(tmp_path):
    """默认排除噪声目录，不读其中的文件（ch09 F1/AC2）。"""
    (tmp_path / "a.py").write_text("KEY\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "b.py").write_text("KEY\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "c.py").write_text("KEY\n", encoding="utf-8")


async def test_grep_never_opens_excluded_files(tmp_path, monkeypatch):
    """排除判定先于文件读取：被排除目录中的文件根本不会被打开（ch09 F1/AC7）。

    直接数 open 调用，而非靠耗时推断——这是"省下读取开销"这一收益的确定性证据。
    """
    (tmp_path / "a.py").write_text("KEY\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "b.py").write_text("KEY\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "c.py").write_text("KEY\n", encoding="utf-8")

    opened: list[str] = []
    real_open = Path.open

    def spy(self, *args, **kwargs):
        opened.append(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy)
    r = await GrepTool().execute(json.dumps({"pattern": "KEY", "path": str(tmp_path)}))
    monkeypatch.undo()

    assert not r.is_error
    assert [Path(p).name for p in opened] == ["a.py"]  # 噪声文件一次都没被打开

    r = await GrepTool().execute(json.dumps({"pattern": "KEY", "path": str(tmp_path)}))
    assert not r.is_error
    assert r.content.splitlines() == [f"{tmp_path / 'a.py'}:1:KEY"]


async def test_grep_escape_hatch(tmp_path):
    """搜索根指向被排除目录时 grep 能搜进去（ch09 F2/AC4）。"""
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "b.py").write_text("KEY\n", encoding="utf-8")

    r = await GrepTool().execute(json.dumps({"pattern": "KEY", "path": str(venv)}))
    assert not r.is_error
    assert r.content.splitlines() == [f"{venv / 'b.py'}:1:KEY"]


async def test_grep_glob_param_combines_with_exclude(tmp_path):
    """文件名过滤与排除规则叠加生效，互不覆盖（ch09 F3/AC5）。"""
    (tmp_path / "a.txt").write_text("KEY\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("KEY\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "b.txt").write_text("KEY\n", encoding="utf-8")

    r = await GrepTool().execute(
        json.dumps({"pattern": "KEY", "glob": "*.txt", "path": str(tmp_path)})
    )
    assert not r.is_error
    # 排除先剔除 .venv/b.txt，文件名过滤再剔除 a.py，只剩 a.txt
    assert r.content.splitlines() == [f"{tmp_path / 'a.txt'}:1:KEY"]


# ────────── 逐行扫描（ch09 F4） ──────────


def test_iter_lines_ultra_long_line():
    """超长行只保留前缀并标注 too_long，行号仍连续（ch09 F4/AC9）。"""
    content = "short1\n" + "X" * (_MAX_LINE_CHARS + 100) + "\nshort2\n"
    out = list(_iter_lines(io.StringIO(content), _MAX_LINE_CHARS))

    assert [ln for ln, _, _ in out] == [1, 2, 3]  # 行号连续
    assert out[0][1:] == ("short1", False)
    assert out[2][1:] == ("short2", False)

    lineno, text, too_long = out[1]
    assert too_long is True
    assert text.endswith("…")
    assert len(text) == _MAX_LINE_CHARS + 1  # 恰好截断到阈值 + 省略号


def test_iter_lines_at_threshold_not_truncated():
    """恰好等于阈值的行不算超长，不截断也不标注（边界）。"""
    line = "Y" * _MAX_LINE_CHARS
    out = list(_iter_lines(io.StringIO(line + "\n"), _MAX_LINE_CHARS))
    assert len(out) == 1
    _, text, too_long = out[0]
    assert too_long is False
    assert text == line


def test_iter_lines_long_line_without_newline_at_eof():
    """文件以无换行的超长行结尾：仍被标记截断，不抛异常（ch09 AC9）。"""
    out = list(_iter_lines(io.StringIO("Z" * (_MAX_LINE_CHARS + 50)), _MAX_LINE_CHARS))
    assert len(out) == 1
    _, text, too_long = out[0]
    assert too_long is True
    assert text.endswith("…")


def test_iter_lines_memory_bounded():
    """任意长度超长行都不把整行读进内存：产出文本长度不超过阈值 + 1（ch09 AC9）。"""
    out = list(_iter_lines(io.StringIO("W" * (_MAX_LINE_CHARS * 3)), _MAX_LINE_CHARS))
    assert out, "应至少产出一条"
    assert all(len(text) <= _MAX_LINE_CHARS + 1 for _, text, _ in out)


def test_iter_lines_multi_chunk_long_line_is_one_record():
    """跨多块的超长行只产出一条记录、只占一个行号，后续行号不错位（AC9）。

    行长达 3 倍阈值时，守卫分支会在同一物理行上被触发多次；
    若每次触发都自增行号，该行会被拆成多条记录，后续所有行号被顶高。
    """
    out = list(_iter_lines(io.StringIO("A" * (_MAX_LINE_CHARS * 3) + "\nTAIL\n"), _MAX_LINE_CHARS))

    assert [ln for ln, _, _ in out] == [1, 2]  # 超长行占 1 行，TAIL 是第 2 行
    assert out[1][1:] == ("TAIL", False)

    _, text, too_long = out[0]
    assert too_long is True
    assert text.endswith("…")
    assert len(text) == _MAX_LINE_CHARS + 1


def test_iter_lines_long_lines_mixed_keep_line_numbers():
    """两个超长行夹在短行之间：行号与物理行严格一一对应（AC9）。"""
    content = "a\n" + "B" * (_MAX_LINE_CHARS * 2) + "\nb\n" + "C" * (_MAX_LINE_CHARS * 4) + "\nc\n"
    out = list(_iter_lines(io.StringIO(content), _MAX_LINE_CHARS))

    assert [ln for ln, _, _ in out] == [1, 2, 3, 4, 5]  # 行号与物理行一一对应
    assert [(ln, text, tl) for ln, text, tl in out if ln in (1, 3, 5)] == [
        (1, "a", False),
        (3, "b", False),
        (5, "c", False),
    ]
    assert all(too_long is True for ln, _, too_long in out if ln in (2, 4))


def test_iter_lines_never_marks_tail_fragment_as_short():
    """超长行被截断后，剩余片段不得以 too_long=False 混入（AC9）。"""
    out = list(_iter_lines(io.StringIO("A" * (_MAX_LINE_CHARS * 3) + "\n"), _MAX_LINE_CHARS))

    assert len(out) == 1
    _, text, too_long = out[0]
    assert too_long is True
    assert text.endswith("…")


def test_iter_lines_matches_plain_iteration():
    """普通多行文件：产出的行号与文本与朴素按行读取逐条一致（ch09 AC8 等价性）。"""
    lines = ["alpha", "beta", "", "gamma delta", "epsilon"]
    out = list(_iter_lines(io.StringIO("\n".join(lines) + "\n"), _MAX_LINE_CHARS))
    assert [(ln, text) for ln, text, _ in out] == list(enumerate(lines, start=1))
    assert all(too_long is False for _, _, too_long in out)
