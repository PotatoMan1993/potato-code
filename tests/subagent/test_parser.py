"""subagent 解析器单测（ch15 T3）：frontmatter 分离、字段校验与降级警告。"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from potatocode.permission import Mode
from potatocode.subagent.definition import Definition, Source
from potatocode.subagent.parser import AgentParseError, parse_definition, parse_file


def _md(body: str) -> bytes:
    """构造定义文件字节（统一 utf-8 编码）。"""
    return body.encode("utf-8")


FULL_FRONTMATTER = """---
name: my-agent
description: 测试用角色
tools:
  - read_file
  - grep
disallowedTools:
  - bash
model: haiku
maxTurns: 7
permissionMode: acceptEdits
background: true
---

你是测试角色。
"""


def test_definition_fields() -> None:
    """Definition 数据类包含 spec F4 的全部字段。"""
    field_names = {f.name for f in fields(Definition)}
    assert field_names == {
        "name",
        "description",
        "tools",
        "disallowed_tools",
        "model",
        "max_turns",
        "permission_mode",
        "dont_ask",
        "background",
        "system_prompt",
        "file_path",
        "source",
    }


def test_parse_full_frontmatter() -> None:
    definition = parse_definition(_md(FULL_FRONTMATTER), "test:my-agent", Source.PROJECT)
    assert definition.name == "my-agent"
    assert definition.description == "测试用角色"
    assert definition.tools == ["read_file", "grep"]
    assert definition.disallowed_tools == ["bash"]
    assert definition.model == "haiku"
    assert definition.max_turns == 7
    assert definition.permission_mode is Mode.ACCEPT_EDITS
    assert definition.dont_ask is False
    assert definition.background is True
    assert definition.system_prompt == "你是测试角色。"
    assert definition.file_path == "test:my-agent"
    assert definition.source is Source.PROJECT


def test_parse_minimal_defaults() -> None:
    """仅必填字段：其余全部走默认值。"""
    definition = parse_definition(
        _md("---\nname: mini\ndescription: 最小定义\n---\n\nBODY\n"),
        "test:mini",
        Source.BUILTIN,
    )
    assert definition.tools == []
    assert definition.disallowed_tools == []
    assert definition.model == "inherit"
    assert definition.max_turns == 0
    assert definition.permission_mode is Mode.DEFAULT
    assert definition.dont_ask is False
    assert definition.background is False
    assert definition.system_prompt == "BODY"
    assert definition.is_fork() is False


def test_body_keeps_leading_content_after_delimiter() -> None:
    """正文紧贴收尾定界符时仍完整提取（去前导空行）。"""
    definition = parse_definition(
        _md("---\nname: x\ndescription: d\n---\n第一行\n\n第二行\n"),
        "t",
        Source.USER,
    )
    assert definition.system_prompt == "第一行\n\n第二行"


def test_utf8_bom_tolerated() -> None:
    """带 UTF-8 BOM 的文件正常解析（utf-8-sig 解码）。"""
    raw = b"\xef\xbb\xbf---\nname: bom-agent\ndescription: d\n---\n\nBODY\n"
    definition = parse_definition(raw, "t", Source.USER)
    assert definition.name == "bom-agent"
    assert definition.system_prompt == "BODY"


def test_missing_name_raises() -> None:
    with pytest.raises(AgentParseError, match="name"):
        parse_definition(_md("---\ndescription: d\n---\nbody"), "t", Source.USER)


def test_invalid_name_raises() -> None:
    for bad_name in ("1abc", "has space", "x" * 33, ""):
        frontmatter = f"---\nname: {bad_name}\ndescription: d\n---\nbody"
        with pytest.raises(AgentParseError, match="name"):
            parse_definition(_md(frontmatter), "t", Source.USER)


def test_missing_description_raises() -> None:
    with pytest.raises(AgentParseError, match="description"):
        parse_definition(_md("---\nname: alpha\n---\nbody"), "t", Source.USER)


def test_unclosed_frontmatter_raises() -> None:
    with pytest.raises(AgentParseError, match="未闭合"):
        parse_definition(_md("---\nname: alpha\ndescription: d\n"), "t", Source.USER)


def test_non_dict_frontmatter_raises() -> None:
    with pytest.raises(AgentParseError, match="键值映射"):
        parse_definition(_md("---\n- list\n---\nbody"), "t", Source.USER)


def test_unknown_model_warns_and_falls_back(capsys: pytest.CaptureFixture[str]) -> None:
    """未知 model：stderr 警告 + 降级 inherit，文件仍可用（AC22）。"""
    definition = parse_definition(
        _md("---\nname: bad\ndescription: d\nmodel: gpt-4\n---\nbody"),
        "t:bad.md",
        Source.USER,
    )
    assert definition.model == "inherit"
    err = capsys.readouterr().err
    assert "unknown model" in err
    assert "gpt-4" in err


def test_unknown_permission_mode_warns_and_falls_back(
    capsys: pytest.CaptureFixture[str],
) -> None:
    definition = parse_definition(
        _md("---\nname: bad\ndescription: d\npermissionMode: weirdMode\n---\nbody"),
        "t:bad.md",
        Source.USER,
    )
    assert definition.permission_mode is Mode.DEFAULT
    assert definition.dont_ask is False
    assert "unknown permissionMode" in capsys.readouterr().err


def test_dont_ask_recognized() -> None:
    """permissionMode=dontAsk：dont_ask=True 且模式落 DEFAULT（F4）。"""
    definition = parse_definition(
        _md("---\nname: auto\ndescription: d\npermissionMode: dontAsk\n---\nbody"),
        "t",
        Source.PROJECT,
    )
    assert definition.dont_ask is True
    assert definition.permission_mode is Mode.DEFAULT


def test_parse_file_reads_disk(tmp_path: Path) -> None:
    path = tmp_path / "disk-agent.md"
    path.write_text(FULL_FRONTMATTER, encoding="utf-8")
    definition = parse_file(path, Source.PROJECT)
    assert definition.name == "my-agent"
    assert definition.source is Source.PROJECT
    assert Path(definition.file_path) == path


def test_parse_file_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(AgentParseError, match="读取定义文件失败"):
        parse_file(tmp_path / "nope.md", Source.USER)
