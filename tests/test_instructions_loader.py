"""项目指令加载单测：三层优先级、@include 展开、深度/环路/逃逸/二进制降级（ch11 F1-F6）。"""

from __future__ import annotations

from pathlib import Path

from potatocode.instructions import Loader
from potatocode.instructions.loader import (
    BINARY_WARNING,
    CYCLE_WARNING,
    DEPTH_WARNING,
    ESCAPE_WARNING,
)


def _layout(tmp_path) -> tuple[Path, Path]:
    """建出「项目根 + 用户主目录」两个隔离目录，返回 (project_root, user_home)。"""
    project = tmp_path / "proj"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    return project, home


def _loader(project: Path, home: Path, **kwargs) -> Loader:
    return Loader(str(project), str(home), **kwargs)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ────────── 三层加载（F1/AC1/AC2） ──────────


def test_three_layers_merged_in_priority_order(tmp_path):
    """三层都有文件时按「项目根 → .potatocode → 用户级」顺序拼接（AC1）。"""
    project, home = _layout(tmp_path)
    _write(project / "MEWCODE.md", "项目级")
    _write(project / ".potatocode" / "MEWCODE.md", "项目配置级")
    _write(home / ".potatocode" / "MEWCODE.md", "用户级")

    text = _loader(project, home).load()

    assert text.index("项目级") < text.index("项目配置级") < text.index("用户级")


def test_missing_layers_are_silent(tmp_path):
    """只有项目根有文件时加载成功，不报错、不含其他层内容（AC2）。"""
    project, home = _layout(tmp_path)
    _write(project / "MEWCODE.md", "只有项目级")

    assert _loader(project, home).load() == "只有项目级"


def test_no_instruction_files_returns_empty(tmp_path):
    """三层都不存在 → 空字符串（空指令不干扰系统提示装配）。"""
    project, home = _layout(tmp_path)

    assert _loader(project, home).load() == ""


def test_empty_file_yields_empty(tmp_path):
    """空指令文件产出空内容，不影响拼接（F6）。"""
    project, home = _layout(tmp_path)
    _write(project / "MEWCODE.md", "   \n\n")

    assert _loader(project, home).load() == ""


# ────────── @include 展开（F2/F3/F4/F5） ──────────


def test_include_replaces_line(tmp_path):
    """独占一行的 @include 被引用文件内容替换（AC3）。"""
    project, home = _layout(tmp_path)
    _write(project / "MEWCODE.md", "头部\n@include rules/style.md\n尾部")
    _write(project / "rules" / "style.md", "规则内容")

    assert _loader(project, home).load() == "头部\n规则内容\n尾部"


def test_nested_include(tmp_path):
    """A include B、B include C → A 的输出包含 C 的内容。"""
    project, home = _layout(tmp_path)
    _write(project / "MEWCODE.md", "@include b.md")
    _write(project / "b.md", "B 的内容\n@include c.md")
    _write(project / "c.md", "C 的内容")

    text = _loader(project, home).load()

    assert "B 的内容" in text and "C 的内容" in text


def test_include_path_relative_to_current_file(tmp_path):
    """@include 路径相对当前文件所在目录解析（子目录里的相对引用）。"""
    project, home = _layout(tmp_path)
    _write(project / "MEWCODE.md", "@include sub/a.md")
    _write(project / "sub" / "a.md", "@include deep/b.md")
    _write(project / "sub" / "deep" / "b.md", "深层内容")

    assert "深层内容" in _loader(project, home).load()


def test_max_depth_truncates_sixth_layer(tmp_path):
    """6 层嵌套链的第 6 层不展开，输出出现深度警告（AC4/F3）。"""
    project, home = _layout(tmp_path)
    _write(project / "MEWCODE.md", "@include d2.md")
    for level in range(2, 6):  # d2 → d3 → d4 → d5
        _write(project / f"d{level}.md", f"第{level}层\n@include d{level + 1}.md")
    _write(project / "d6.md", "第6层不应出现")

    text = _loader(project, home).load()

    assert "第5层" in text  # 第 5 层仍然展开
    assert "第6层不应出现" not in text  # 第 6 层被挡下
    assert DEPTH_WARNING in text
    assert "@include d6.md" in text  # 超深的 @include 行保留原文


def test_cycle_detected(tmp_path):
    """A include B、B include A → 第二次引用不展开，出现环路警告（AC5/F4）。"""
    project, home = _layout(tmp_path)
    _write(project / "MEWCODE.md", "A 的内容\n@include b.md")
    _write(project / "b.md", "B 的内容\n@include MEWCODE.md")

    text = _loader(project, home).load()

    assert text.count("A 的内容") == 1  # 只展开一次
    assert "B 的内容" in text
    assert CYCLE_WARNING in text  # 环路处跳过该行、就地给出警告


def test_path_escape_rejected(tmp_path):
    """项目级指令引用根边界外的文件 → 不加载，出现范围警告（AC6/F5）。"""
    project, home = _layout(tmp_path)
    _write(tmp_path / "outside.md", "越界内容")
    _write(project / "MEWCODE.md", "@include ../outside.md")

    text = _loader(project, home).load()

    assert "越界内容" not in text
    assert ESCAPE_WARNING in text


def test_user_layer_boundary_is_user_potatocode(tmp_path):
    """用户级文件的根边界是 ~/.potatocode/：引用同目录内文件可以，跳出去不行。"""
    project, home = _layout(tmp_path)
    user_md = home / ".potatocode" / "MEWCODE.md"
    _write(user_md, "@include shared.md\n@include ../secret.md")
    _write(home / ".potatocode" / "shared.md", "用户级可读")
    _write(home / "secret.md", "越界内容")

    text = _loader(project, home).load()

    assert "用户级可读" in text
    assert "越界内容" not in text
    assert ESCAPE_WARNING in text


def test_missing_include_is_silent(tmp_path):
    """被引用文件不存在 → 该行静默消失，不报错（F6）。"""
    project, home = _layout(tmp_path)
    _write(project / "MEWCODE.md", "头部\n@include not-there.md\n尾部")

    assert _loader(project, home).load() == "头部\n\n尾部"


def test_binary_include_skipped(tmp_path):
    """含 \\x00 的文件视为二进制跳过，出现警告注释（F6）。"""
    project, home = _layout(tmp_path)
    _write(project / "MEWCODE.md", "头部\n@include blob.bin")
    (project / "blob.bin").write_bytes(b"BM\x00\x01\x02SECRETBYTES")

    text = _loader(project, home).load()

    assert BINARY_WARNING in text
    assert "SECRETBYTES" not in text  # 二进制内容没被读进来


def test_inline_include_not_expanded(tmp_path):
    """出现在段落中间的 @include 不做替换，保持原文（F2）。"""
    project, home = _layout(tmp_path)
    _write(project / "MEWCODE.md", "请参考 @include rules/style.md 这份文件")
    _write(project / "rules" / "style.md", "规则内容")

    text = _loader(project, home).load()

    assert text == "请参考 @include rules/style.md 这份文件"
    assert "规则内容" not in text


def test_indented_include_not_expanded(tmp_path):
    """有缩进的 @include 不算独占一行，不展开（F2）。"""
    project, home = _layout(tmp_path)
    _write(project / "MEWCODE.md", "  @include rules/style.md")
    _write(project / "rules" / "style.md", "规则内容")

    assert "规则内容" not in _loader(project, home).load()


def test_unreadable_layer_degrades(tmp_path):
    """某一层读取失败（这里是目录占位）时静默跳过，其余层照常加载（N5）。"""
    project, home = _layout(tmp_path)
    (project / "MEWCODE.md").mkdir()  # 用目录冒充文件，read_bytes 必然失败
    _write(project / ".potatocode" / "MEWCODE.md", "项目配置级")

    assert _loader(project, home).load() == "项目配置级"


def test_default_user_home_is_expanded(tmp_path, monkeypatch):
    """未显式给出 user_home 时按 ~ 展开（F1 路径③）。"""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    loader = Loader(str(tmp_path / "proj"))
    assert loader.user_home  # 非空即可，具体取值随平台
