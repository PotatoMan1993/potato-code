"""持久化单测：精确规则生成、escape_glob、本地层写入与重载（AC10/F8）。"""

from __future__ import annotations

import json

from potatocode.llm import ToolCall
from potatocode.permission import Mode, new_engine
from potatocode.permission.persist import escape_glob, persist_local_allow, rule_for
from potatocode.permission.types import Decision


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(id="x", name=name, input=json.dumps(args))


def test_escape_glob():
    assert escape_glob("rm -rf *") == "rm -rf \\*"
    assert escape_glob("echo a*b") == "echo a\\*b"


def test_rule_for_bash():
    eng, _ = new_engine(".")
    rule, text, ok = rule_for(eng, _call("bash", {"command": "git status"}))
    assert ok
    assert rule.tool == "Bash"
    assert rule.allow
    assert text == "Bash(git status)"


def test_rule_for_file_relpath(tmp_path):
    eng, _ = new_engine(str(tmp_path))
    rule, text, ok = rule_for(eng, _call("write_file", {"path": "src/a.py", "content": "x"}))
    assert ok
    assert rule.tool == "Write"
    assert rule.raw == "src/a.py"
    assert rule.matcher is not None and rule.matcher.match("src/a.py")
    assert text == "Write(src/a.py)"


def test_persist_local_allow(tmp_path):
    """永久放行写入本地层文件，重载后同调用判 Allow（AC10/F8）。"""
    eng, _ = new_engine(str(tmp_path))
    call = _call("bash", {"command": "pytest"})
    # default + exec 未命中 → 先确认 Ask
    assert eng.check(Mode.DEFAULT, call, False)[0] is Decision.ASK

    persist_local_allow(eng, call)

    # 文件已写入
    from pathlib import Path

    p = Path(eng.local_path)
    assert p.is_file()
    assert "Bash(pytest)" in p.read_text(encoding="utf-8")

    # 以新引擎重载，同调用判 Allow
    eng2, _ = new_engine(str(tmp_path))
    assert eng2.check(Mode.DEFAULT, call, False)[0] is Decision.ALLOW


def test_persist_local_allow_idempotent(tmp_path):
    """重复 persist 不抛、不重复写文件。"""
    eng, _ = new_engine(str(tmp_path))
    call = _call("bash", {"command": "pytest"})
    persist_local_allow(eng, call)
    persist_local_allow(eng, call)  # 第二次不抛

    from pathlib import Path

    text = Path(eng.local_path).read_text(encoding="utf-8")
    assert text.count("Bash(pytest)") == 1
