"""恢复段单测：文件快照裁剪 / 截断、工具列表逐项一致与文案稳定性。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from potatocode.compact.const import ESTIMATE_CHARS_PER_TOKEN, RECOVERY_TOKENS_PER_FILE
from potatocode.compact.recovery import (
    BOUNDARY_NOTICE,
    build_recovery_attachment,
    render_file_block,
    render_tools_block,
)
from potatocode.compact.state import FileReadRecord
from potatocode.llm import ToolDefinition

# 单文件快照的字符上限（token 上限 × 字符/token 比）
_CHAR_LIMIT = int(RECOVERY_TOKENS_PER_FILE * ESTIMATE_CHARS_PER_TOKEN)


def _record(path: str, content: str, minutes: int = 0) -> FileReadRecord:
    """构造一条带相对时间戳的文件记录。"""
    base = datetime(2026, 1, 1, 12, 0, 0)
    return FileReadRecord(path=path, content=content, timestamp=base + timedelta(minutes=minutes))


def _defs() -> list[ToolDefinition]:
    """构造两个工具定义（含参数 schema，供逐项比对）。"""
    return [
        ToolDefinition(
            name="read_file",
            description="读取文件",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        ),
        ToolDefinition(
            name="bash",
            description="执行命令",
            input_schema={"type": "object", "properties": {"cmd": {"type": "string"}}},
        ),
    ]


def test_render_file_block_keeps_head_and_marks_truncation():
    """超长内容保留头部、截掉尾部，并在末尾标注截断。"""
    long_text = "头" * (_CHAR_LIMIT + 500)
    block = render_file_block(_record("/x/big.py", long_text))

    assert block.startswith("### /x/big.py")
    assert "[read at]" in block
    assert "(content truncated)" in block
    assert block.count("头") == _CHAR_LIMIT  # 保留的是头部而不是尾部
    assert long_text[:100] in block


def test_render_file_block_no_truncation():
    """未超限的文件不出现截断标注，内容原样保留。"""
    block = render_file_block(_record("/x/small.py", "print(1)"))
    assert "print(1)" in block
    assert "(content truncated)" not in block


def test_build_recovery_attachment_has_three_sections():
    """恢复段包含文件 / 工具 / 边界提示三个分节标题。"""
    text = build_recovery_attachment([_record("/x/a.py", "a")], _defs())
    assert "## 最近读过的文件" in text
    assert "## 当前可用工具" in text
    assert "## 边界提示" in text
    assert BOUNDARY_NOTICE in text


def test_build_recovery_attachment_file_limit_and_order():
    """7 条记录只展示最近 5 条，按时间戳倒序，第 6、7 条路径不出现。"""
    records = [_record(f"/x/f{i}.py", f"c{i}", minutes=i) for i in range(7)]  # f6 最新
    records.sort(key=lambda r: r.timestamp, reverse=True)  # 与 RecoveryState.snapshot 口径一致

    text = build_recovery_attachment(records, _defs())

    assert "/x/f6.py" in text and "/x/f2.py" in text
    assert "/x/f1.py" not in text  # 反向断言：更旧的记录不出现
    assert "/x/f0.py" not in text
    # 展示顺序与时间戳倒序一致
    assert text.index("/x/f6.py") < text.index("/x/f5.py") < text.index("/x/f2.py")


def test_build_recovery_attachment_empty_files():
    """没有读过任何文件时文件块给一行占位。"""
    text = build_recovery_attachment([], _defs())
    assert "## 最近读过的文件\n(无)" in text


def test_build_recovery_attachment_tools_exact():
    """工具块逐行渲染，工具名集合与入参定义完全一致。"""
    defs = _defs()
    text = build_recovery_attachment([], defs)

    rendered_names = {line.split(":")[0][2:] for line in text.splitlines() if line.startswith("- ")}
    assert rendered_names == {d.name for d in defs}
    # schema 逐项可还原比对
    for d in defs:
        schema = json.dumps(d.input_schema, separators=(",", ":"), ensure_ascii=False)
        assert f"  schema: {schema}" in text


def test_render_tools_block_empty():
    """没有工具时工具块给一行占位。"""
    assert render_tools_block([]) == "(无)"


def test_build_recovery_attachment_stable():
    """相同入参连续两次渲染逐字节相等（prompt cache 稳定性）。"""
    records = [_record("/x/a.py", "a"), _record("/x/b.py", "b", minutes=1)]
    first = build_recovery_attachment(records, _defs())
    second = build_recovery_attachment(records, _defs())
    assert first == second
