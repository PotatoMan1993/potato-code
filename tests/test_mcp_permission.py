"""MCP 工具复用既有权限链路的集成测试。"""

from __future__ import annotations

import json

from potatocode.llm import ToolCall
from potatocode.permission import Mode, new_engine
from potatocode.permission.types import Decision


def _call(name: str) -> ToolCall:
    return ToolCall(id="mcp-test", name=name, input=json.dumps({"command": "rm -rf /"}))


def test_mcp_mode_fallback_and_builtin_guards_are_scoped(tmp_path):
    engine, error = new_engine(str(tmp_path))
    assert error is None
    call = _call("mcp__demo__operate")

    assert engine.check(Mode.DEFAULT, call, read_only=True)[0] is Decision.ALLOW
    # 未声明只读的 MCP 工具归 Exec 并 Ask，但参数中的危险字符串不被当成 bash 命令。
    assert engine.check(Mode.DEFAULT, call, read_only=False)[0] is Decision.ASK
    assert engine.check(Mode.BYPASS, call, read_only=False)[0] is Decision.ALLOW


def test_mcp_exact_and_server_wildcard_rules(tmp_path):
    settings = tmp_path / ".potatocode/settings.yaml"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        """
permissions:
  allow: ["mcp__github__*"]
  deny: ["mcp__github__delete_repo"]
""",
        encoding="utf-8",
    )
    engine, error = new_engine(str(tmp_path))
    assert error is None

    create = engine.check(Mode.DEFAULT, _call("mcp__github__create_issue"), False)
    delete = engine.check(Mode.BYPASS, _call("mcp__github__delete_repo"), False)
    other = engine.check(Mode.DEFAULT, _call("mcp__slack__post_message"), False)

    assert create[0] is Decision.ALLOW
    assert delete[0] is Decision.DENY
    assert other[0] is Decision.ASK
