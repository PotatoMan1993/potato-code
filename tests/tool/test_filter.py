"""工具过滤多层防线单测（ch15 T9）：五层顺序、后台白名单与子注册中心。"""

from __future__ import annotations

from typing import Any

import pytest

from potatocode.subagent import Source
from potatocode.tool import Registry, Result
from potatocode.tool.filter import (
    ALL_AGENT_DISALLOWED_TOOLS,
    ASYNC_AGENT_ALLOWED_TOOLS,
    CUSTOM_AGENT_DISALLOWED_TOOLS,
    FilterParams,
    apply_agent_tool_filter,
    build_sub_registry,
    is_mcp_or_skill,
)


class _FakeTool:
    """最小工具桩：仅携带名字与只读标记。"""

    def __init__(self, name: str, read_only: bool = False) -> None:
        self._name = name
        self._read_only = read_only

    def name(self) -> str:
        return self._name

    def description(self) -> str:
        return "fake tool"

    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def read_only(self) -> bool:
        return self._read_only

    async def execute(self, args: str) -> Result:
        return Result(content="ok")


def _registry(*names: str) -> Registry:
    registry = Registry()
    for name in names:
        registry.register(_FakeTool(name, read_only=name.startswith("read")))
    return registry


def _params(all_names: list[str], **kwargs: Any) -> FilterParams:
    defaults: dict[str, Any] = {"source": int(Source.BUILTIN), "background": False}
    defaults.update(kwargs)
    return FilterParams(all=all_names, **defaults)


def test_constants() -> None:
    """三个防线常量存在且内容符合本期约定（F26/F27/F28）。"""
    assert ALL_AGENT_DISALLOWED_TOOLS == ["Agent"]
    assert CUSTOM_AGENT_DISALLOWED_TOOLS == []
    assert ASYNC_AGENT_ALLOWED_TOOLS == [
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "bash",
        "LoadSkill",
        "InstallSkill",
    ]


def test_default_removes_agent_only() -> None:
    names = ["read_file", "Agent", "bash", "TaskList"]
    assert apply_agent_tool_filter(_params(names)) == ["read_file", "bash", "TaskList"]


def test_background_intersects_whitelist() -> None:
    """后台：与白名单取交集，Agent / TaskList / SendMessage 等元工具被剔除（AC 相关）。"""
    names = ["read_file", "bash", "Agent", "TaskList", "TaskStop", "SendMessage"]
    allowed = apply_agent_tool_filter(_params(names, background=True))
    assert allowed == ["read_file", "bash"]


def test_background_keeps_mcp_tools() -> None:
    """MCP 工具（mcp__ 前缀）在后台模式下被保留（F28）。"""
    names = ["read_file", "mcp__fs__read", "Agent", "TaskGet"]
    allowed = apply_agent_tool_filter(_params(names, background=True))
    assert allowed == ["read_file", "mcp__fs__read"]


def test_disallowed_blacklist() -> None:
    names = ["read_file", "bash", "write_file"]
    allowed = apply_agent_tool_filter(_params(names, disallowed=["bash"]))
    assert allowed == ["read_file", "write_file"]


def test_allowed_whitelist() -> None:
    names = ["read_file", "bash", "grep", "glob"]
    allowed = apply_agent_tool_filter(_params(names, allowed=["read_file", "grep"]))
    assert allowed == ["read_file", "grep"]


def test_whitelist_then_blacklist_order() -> None:
    """白名单先收窄、黑名单再剔除（F30 顺序）；黑名单可剔除白名单内的工具。"""
    names = ["read_file", "grep", "bash"]
    allowed = apply_agent_tool_filter(
        _params(names, allowed=["read_file", "grep"], disallowed=["grep"])
    )
    assert allowed == ["read_file"]


def test_custom_source_applies_extra_disallow() -> None:
    """非内置来源（user/project）会应用 CUSTOM_AGENT_DISALLOWED_TOOLS（本期为空，F27）。"""
    names = ["read_file", "Agent"]
    assert apply_agent_tool_filter(_params(names, source=int(Source.PROJECT))) == ["read_file"]


def test_order_preserved() -> None:
    names = ["bash", "read_file", "glob"]
    assert apply_agent_tool_filter(_params(names)) == ["bash", "read_file", "glob"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("mcp__fs__read", True),
        ("mcp__x", True),
        ("read_file", False),
        ("mcp", False),
        ("Agent", False),
    ],
)
def test_is_mcp_or_skill(name: str, expected: bool) -> None:
    assert is_mcp_or_skill(name) is expected


def test_build_sub_registry() -> None:
    """子注册中心只含 allowed 工具、顺序沿用父注册序，且引用同一批工具实例。"""
    parent = _registry("read_file", "bash", "glob", "Agent")
    sub = build_sub_registry(parent, ["bash", "read_file"])
    assert sub.names() == ["read_file", "bash"]
    assert sub.get("bash") is parent.get("bash")  # 同一实例：权限/执行行为一致
    assert sub.get("Agent") is None
    assert sub.is_read_only("read_file") is True
    assert sub.is_read_only("bash") is False
