"""subagent Catalog 单测（ch15 T7）：内置加载、三层覆盖与错误跳过。"""

from __future__ import annotations

from pathlib import Path

import pytest

from potatocode.permission import Mode
from potatocode.subagent import Source, builtin_definitions, load_catalog

EXPLORE_MD = """---
name: Explore
description: 只读探索
disallowedTools:
  - write_file
  - edit_file
model: haiku
maxTurns: 30
---

探索用
"""


def _write_agent(directory: Path, filename: str, content: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(content, encoding="utf-8")
    return path


def test_builtin_definitions() -> None:
    """内置 3 角色：general-purpose / Explore / Plan，随包读取成功（F32/AC16 基础）。"""
    definitions = builtin_definitions()
    assert [d.name for d in definitions] == ["Explore", "Plan", "general-purpose"]  # name 升序
    by_name = {d.name: d for d in definitions}
    assert all(d.source is Source.BUILTIN for d in definitions)
    # general-purpose：全工具、默认模式
    assert by_name["general-purpose"].disallowed_tools == []
    assert by_name["general-purpose"].permission_mode is Mode.DEFAULT
    assert by_name["general-purpose"].max_turns == 30
    # Explore：禁写、haiku
    assert by_name["Explore"].disallowed_tools == ["write_file", "edit_file"]
    assert by_name["Explore"].model == "haiku"
    # Plan：plan 模式、15 轮
    assert by_name["Plan"].permission_mode is Mode.PLAN
    assert by_name["Plan"].max_turns == 15


def test_builtin_definitions_all_have_system_prompt() -> None:
    for definition in builtin_definitions():
        assert definition.system_prompt.strip()


def test_load_catalog_builtin_only(tmp_path: Path) -> None:
    """无用户/项目定义时：resolve 命中内置版本。"""
    catalog = load_catalog(str(tmp_path), user_dir=tmp_path / "no-user", project_dir=tmp_path / "no-proj")
    definition = catalog.resolve("Explore")
    assert definition is not None
    assert definition.source is Source.BUILTIN
    assert catalog.resolve("non-existent") is None


def test_three_layer_override(tmp_path: Path) -> None:
    """同名定义按 项目 > 用户 > 内置 覆盖（F6/AC16）。"""
    user_dir = tmp_path / "user-agents"
    project_dir = tmp_path / "project-agents"

    # ① 三层都有 → 项目级胜出
    _write_agent(user_dir, "explore.md", EXPLORE_MD)
    _write_agent(project_dir, "explore.md", EXPLORE_MD)
    catalog = load_catalog(str(tmp_path), user_dir=user_dir, project_dir=project_dir)
    assert catalog.resolve("Explore").source is Source.PROJECT

    # ② 用户 + 内置 → 用户级胜出
    catalog = load_catalog(str(tmp_path), user_dir=user_dir, project_dir=project_dir / "absent")
    assert catalog.resolve("Explore").source is Source.USER

    # ③ 仅内置
    catalog = load_catalog(str(tmp_path), user_dir=user_dir / "absent", project_dir=project_dir / "absent")
    assert catalog.resolve("Explore").source is Source.BUILTIN


def test_project_override_changes_content(tmp_path: Path) -> None:
    """项目级覆盖不只换 source，内容也以高优先级版本为准。"""
    user_dir = tmp_path / "user-agents"
    project_dir = tmp_path / "project-agents"
    _write_agent(user_dir, "explore.md", EXPLORE_MD)
    _write_agent(
        project_dir,
        "explore.md",
        "---\nname: Explore\ndescription: 项目级覆盖\nmaxTurns: 10\n---\n\n项目级提示\n",
    )
    catalog = load_catalog(str(tmp_path), user_dir=user_dir, project_dir=project_dir)
    definition = catalog.resolve("Explore")
    assert definition is not None
    assert definition.max_turns == 10
    assert definition.model == "inherit"
    assert "项目级提示" in definition.system_prompt


def test_fork_definition(tmp_path: Path) -> None:
    """fork_definition：name=__fork__、is_fork=True、工具集不收窄（F22/F24）。"""
    catalog = load_catalog(str(tmp_path))
    fork = catalog.fork_definition()
    assert fork.is_fork() is True
    assert fork.name == "__fork__"
    assert fork.tools == []
    assert fork.disallowed_tools == []
    assert fork.system_prompt == ""


def test_invalid_file_skipped_with_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """单文件解析失败：stderr 警告 + 跳过，其余文件正常加载（F7）。"""
    project_dir = tmp_path / "agents"
    _write_agent(project_dir, "good.md", EXPLORE_MD)
    _write_agent(project_dir, "bad.md", "---\nname: 有空格 名\ndescription: d\n---\nbody")
    catalog = load_catalog(str(tmp_path), user_dir=tmp_path / "absent", project_dir=project_dir)
    assert catalog.resolve("Explore") is not None
    assert catalog.resolve("bad") is None
    assert "已跳过" in capsys.readouterr().err


def test_list_sorted_and_by_source(tmp_path: Path) -> None:
    project_dir = tmp_path / "agents"
    _write_agent(project_dir, "explore.md", EXPLORE_MD)
    catalog = load_catalog(str(tmp_path), user_dir=tmp_path / "absent", project_dir=project_dir)
    names = [d.name for d in catalog.list()]
    assert names == sorted(names)
    project_defs = catalog.list_by_source(Source.PROJECT)
    assert [d.name for d in project_defs] == ["Explore"]
    assert catalog.list_by_source(Source.PLUGIN) == []  # 插件级本期恒空（F8）
