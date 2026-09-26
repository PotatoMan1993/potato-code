"""tool.exclude 排除判定单测（ch09 F1/F2）。"""

from __future__ import annotations

from pathlib import Path

from potatocode.tool.exclude import EXCLUDED_DIR_NAMES, is_excluded

# 判定用的假项目根（纯字符串运算，不触碰磁盘）
ROOT = Path("/proj")


def test_excludes_hidden_dirs():
    """隐藏目录下的文件被排除（F1）。"""
    assert is_excluded(ROOT / ".venv" / "Lib" / "x.py", ROOT)
    assert is_excluded(ROOT / ".git" / "config", ROOT)
    assert is_excluded(ROOT / ".mypy_cache" / "3.12" / "a.json", ROOT)
    assert is_excluded(ROOT / ".potatocode" / "sessions" / "s.json", ROOT)


def test_excludes_noise_dir_names():
    """__pycache__ / node_modules 被排除（F1）。"""
    assert is_excluded(ROOT / "__pycache__" / "x.pyc", ROOT)
    assert is_excluded(ROOT / "node_modules" / "lodash" / "index.js", ROOT)


def test_excludes_nested_at_any_depth():
    """被排除目录位于任意深度都生效（F1）。"""
    assert is_excluded(ROOT / "src" / "a" / "node_modules" / "b" / "c.js", ROOT)
    assert is_excluded(ROOT / "src" / ".cache" / "d" / "e.py", ROOT)


def test_keeps_normal_dirs():
    """非隐藏、非噪声目录不排除（F1）。"""
    assert not is_excluded(ROOT / "src" / "a.py", ROOT)
    assert not is_excluded(ROOT / "docs" / "x.md", ROOT)
    assert not is_excluded(ROOT / "b.py", ROOT)


def test_root_itself_may_be_excluded_dir():
    """搜索根本身是被排除目录时：只解除根自身，根之下的子目录照常排除（F2 逃生口边界）。"""
    root = Path("/proj/.venv")
    assert not is_excluded(root / "Lib" / "x.py", root)  # 根自身不再参与判定
    assert is_excluded(root / "Lib" / "__pycache__" / "y.pyc", root)  # 根之下仍生效


def test_dotfile_is_not_excluded():
    """文件名以 . 开头不算隐藏目录，不排除（F2 只看目录分量）。"""
    assert not is_excluded(ROOT / ".foo.py", ROOT)
    assert not is_excluded(ROOT / "src" / ".env", ROOT)


def test_path_outside_root_not_excluded():
    """不在搜索根之下的路径返回 False（防御分支，失败放开）。"""
    assert not is_excluded(Path("/elsewhere/a.py"), ROOT)


def test_excluded_names_are_internal_and_fixed():
    """排除名单内置固定：只含两个非隐藏噪声目录，隐藏目录靠句点规则覆盖（F1）。"""
    assert EXCLUDED_DIR_NAMES == frozenset({"__pycache__", "node_modules"})
    assert ".venv" not in EXCLUDED_DIR_NAMES
