"""沙箱单测：项目内放行、项目外拒绝、符号链接防逃逸、新建文件祖先回退（AC2/F2/N2）。"""

from __future__ import annotations

import os

import pytest

from potatocode.permission import new_engine
from potatocode.permission.sandbox import relpath_for, resolve_root, sandbox_ok


def _engine(root):
    return new_engine(root)[0]


def test_in_root_allowed(tmp_path):
    """项目内文件放行。"""
    eng = _engine(str(tmp_path))
    inside = tmp_path / "a.txt"
    inside.write_text("x", encoding="utf-8")
    assert sandbox_ok(eng, str(inside))


def test_outside_root_denied(tmp_path):
    """项目外绝对路径拒绝（如 /etc/passwd、临时目录外）。"""
    eng = _engine(str(tmp_path))
    outside = tmp_path.parent / "outside.txt"
    assert not sandbox_ok(eng, str(outside))
    # 相对逃逸 ../outside
    assert not sandbox_ok(eng, os.path.join("..", "outside.txt"))


def test_relative_within_root(tmp_path):
    """相对路径相对项目根解析。"""
    eng = _engine(str(tmp_path))
    assert sandbox_ok(eng, "sub/dir/file.txt")


def test_empty_path_is_root(tmp_path):
    """空 path 视为根本身。"""
    eng = _engine(str(tmp_path))
    assert sandbox_ok(eng, "")


def _symlink_or_skip(target, link):
    """尝试建符号链接；环境不支持（如 Windows 无权限）则跳过测试。"""
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")


def test_symlink_escape_denied(tmp_path):
    """项目内软链接指向项目外 → 先解析再比对，拒绝（N2）。"""
    eng = _engine(str(tmp_path))
    outside_dir = tmp_path.parent / "real_outside"
    outside_dir.mkdir(exist_ok=True)
    (outside_dir / "secret.txt").write_text("s", encoding="utf-8")

    link = tmp_path / "link"
    _symlink_or_skip(outside_dir, link)
    assert not sandbox_ok(eng, str(link / "secret.txt"))


def test_symlink_within_root_allowed(tmp_path):
    """项目内软链接指向项目内 → 放行。"""
    eng = _engine(str(tmp_path))
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    _symlink_or_skip(real, link)
    assert sandbox_ok(eng, str(link / "x.txt"))


def test_new_file_ancestor_fallback(tmp_path):
    """新建文件（含未创建的多级中间目录）回退到最近已存在祖先，放行（AC2/N2）。"""
    eng = _engine(str(tmp_path))
    target = tmp_path / "a" / "b" / "c" / "new.txt"  # a/b/c 均不存在
    assert sandbox_ok(eng, str(target))


def test_relpath_for(tmp_path):
    """relpath_for 规整为项目相对 slash 路径。"""
    eng = _engine(str(tmp_path))
    assert relpath_for(eng.root, str(tmp_path / "src" / "a.py")) == "src/a.py"
    assert relpath_for(eng.root, "src/a.py") == "src/a.py"


def test_resolve_root_resolves(tmp_path):
    """resolve_root 解析为绝对路径。"""
    resolved = resolve_root(str(tmp_path))
    assert os.path.isabs(resolved)
