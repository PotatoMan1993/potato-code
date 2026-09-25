"""第 1 层压缩单测：单条 / 聚合落盘、幂等、决策冻结与落盘失败降级。"""

from __future__ import annotations

import os
from pathlib import Path

from potatocode.compact import ContentReplacementState, SessionContext
from potatocode.compact.const import (
    MESSAGE_AGGREGATE_LIMIT,
    PREVIEW_HEAD_BYTES,
    PREVIEW_HEAD_LINES,
    SINGLE_RESULT_LIMIT,
)
from potatocode.compact.layer1 import build_preview, offload_and_snip, spill_single
from potatocode.conversation import Conversation
from potatocode.llm import ROLE_TOOL, ToolCall, ToolResult


def _conv_with_results(contents: list[str], *, prefix: str = "t") -> Conversation:
    """构造一条 RoleTool 消息，携带若干条工具结果。"""
    conv = Conversation()
    conv.add_user("读文件")
    conv.add_assistant_with_tool_calls(
        "",
        [
            ToolCall(id=f"{prefix}{i}", name="read_file", input='{"path": "a.txt"}')
            for i in range(len(contents))
        ],
    )
    conv.add_tool_results(
        [ToolResult(tool_call_id=f"{prefix}{i}", content=c) for i, c in enumerate(contents)]
    )
    return conv


def _tool_results(msgs) -> list[ToolResult]:
    """取出消息列表里第一条 RoleTool 消息的工具结果。"""
    return [m for m in msgs if m.role == ROLE_TOOL][0].tool_results


def _total_bytes(results) -> int:
    """统计一组工具结果的字节总量。"""
    return sum(len(r.content.encode("utf-8")) for r in results)


def test_spill_single_idempotent(session: SessionContext):
    """同一 tool_use_id 重复落盘只写一次（mtime 不变）。"""
    spill_single(session, "t0", "内容")
    path = Path(session.spill_dir) / "t0"
    first_mtime = os.stat(path).st_mtime_ns

    spill_single(session, "t0", "内容变了也不重写")
    assert os.stat(path).st_mtime_ns == first_mtime
    assert path.read_text(encoding="utf-8") == "内容"


def test_offload_single_result(session: SessionContext):
    """单条 60000 字节结果被替换，原文完整落盘，预览体四项信息齐备。"""
    content = "x" * 60000
    conv = _conv_with_results([content])
    state = ContentReplacementState()

    out = offload_and_snip(conv.messages(), state, session)
    preview = _tool_results(out)[0].content

    assert preview != content
    assert preview is not content
    assert "original size: 60000 bytes" in preview
    assert f"[saved to] {Path(session.spill_dir) / 't0'}" in preview
    assert "[head preview]" in preview
    assert "文件读取工具" in preview
    assert "不要凭头部预览猜测" in preview

    spill_path = Path(session.spill_dir) / "t0"
    assert spill_path.read_text(encoding="utf-8") == content


def test_preview_head_within_limits(session: SessionContext):
    """预览体头部同时不超过 20 行与 2048 字节（含多字节字符的边界）。"""
    lines = [f"{i} " + "中" * 200 for i in range(300)]  # 远超单条阈值且含大量多字节字符
    conv = _conv_with_results(["\n".join(lines)])
    state = ContentReplacementState()

    preview = _tool_results(offload_and_snip(conv.messages(), state, session))[0].content
    head = preview.split("[head preview]\n", 1)[1].split("\n完整内容已保存到")[0]

    assert head.count("\n") + 1 <= PREVIEW_HEAD_LINES
    assert len(head.encode("utf-8")) <= PREVIEW_HEAD_BYTES
    assert head  # 头部非空


def test_offload_aggregate(session: SessionContext):
    """3 条 80000 字节（合计 240000 超聚合阈值）→ 按倒序落盘，剩余聚合回落到阈值内。"""
    conv = _conv_with_results(["x" * 80000, "y" * 80000, "z" * 80000])
    state = ContentReplacementState()

    out = offload_and_snip(conv.messages(), state, session)
    results = _tool_results(out)
    replaced = [r for r in results if "content offloaded" in r.content]

    assert len(replaced) >= 2  # 每条都超过单条阈值，必然全部落盘
    assert _total_bytes(results) <= MESSAGE_AGGREGATE_LIMIT
    assert len(list(Path(session.spill_dir).iterdir())) == 3


def test_offload_aggregate_picks_minimal_count(session: SessionContext):
    """5 条 45000 字节（均在单条阈值内）：只需落盘最大的那条即可让聚合达标。"""
    conv = _conv_with_results(["x" * 45000 for _ in range(5)])
    state = ContentReplacementState()

    out = offload_and_snip(conv.messages(), state, session)
    results = _tool_results(out)
    replaced = [r for r in results if "content offloaded" in r.content]

    assert len(replaced) == 1
    assert _total_bytes(results) <= MESSAGE_AGGREGATE_LIMIT


def test_offload_below_threshold_keeps_original(session: SessionContext):
    """未超任何阈值时不落盘，且决策冻结为「保留原文」。"""
    conv = _conv_with_results(["x" * 1000])
    state = ContentReplacementState()

    out = offload_and_snip(conv.messages(), state, session)

    assert _tool_results(out)[0].content == "x" * 1000
    assert list(Path(session.spill_dir).iterdir()) == []
    assert state.seen("t0") is True


def test_offload_decision_freeze(session: SessionContext):
    """同一 id 连跑两轮：第二轮逐字节复用第一轮的预览体，落盘不重复。"""
    conv = _conv_with_results(["x" * 60000])
    state = ContentReplacementState()

    first = _tool_results(offload_and_snip(conv.messages(), state, session))[0].content
    path = Path(session.spill_dir) / "t0"
    mtime = os.stat(path).st_mtime_ns

    second = _tool_results(offload_and_snip(conv.messages(), state, session))[0].content

    assert second == first
    assert os.stat(path).st_mtime_ns == mtime  # 第二轮跳过 I/O


def test_offload_does_not_mutate_input(session: SessionContext):
    """第 1 层是纯函数风格：入参消息列表保持原样。"""
    conv = _conv_with_results(["x" * 60000])
    before = conv.messages()
    state = ContentReplacementState()

    offload_and_snip(before, state, session)

    assert before[2].tool_results[0].content == "x" * 60000


def test_offload_spill_failure_retryable(tmp_path):
    """落盘失败降级为不替换且不写账本，下一轮重新尝试落盘。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("我是文件不是目录", encoding="utf-8")  # 以其为父目录必然写失败
    session = SessionContext(session_id="s", spill_dir=str(blocked / "tool-results"))
    conv = _conv_with_results(["x" * 60000])
    state = ContentReplacementState()

    out = offload_and_snip(conv.messages(), state, session)

    assert _tool_results(out)[0].content == "x" * 60000  # 保持原文
    assert state.seen("t0") is False  # 未写账本，下次仍可评估

    # 修好目录后下一轮能成功落盘
    session.spill_dir = str(tmp_path / "ok" / "tool-results")
    Path(session.spill_dir).mkdir(parents=True)
    out2 = offload_and_snip(conv.messages(), state, session)
    assert "content offloaded" in _tool_results(out2)[0].content
    assert state.seen("t0") is True


def test_preview_stable_across_rounds(session: SessionContext):
    """相同入参两次构造预览体逐字节相等；第二轮也不会重新构造。"""
    first = build_preview(60000, "头部", "/tmp/x/t0")
    second = build_preview(60000, "头部", "/tmp/x/t0")
    assert first == second

    conv = _conv_with_results(["y" * 60000])
    state = ContentReplacementState()
    a = _tool_results(offload_and_snip(conv.messages(), state, session))[0].content
    b = _tool_results(offload_and_snip(conv.messages(), state, session))[0].content
    assert a == b


def test_single_limit_boundary(session: SessionContext):
    """恰好等于单条阈值的项不落盘，超过 1 字节即落盘。"""
    state = ContentReplacementState()
    conv = _conv_with_results(["a" * SINGLE_RESULT_LIMIT, "b" * (SINGLE_RESULT_LIMIT + 1)])
    out = offload_and_snip(conv.messages(), state, session)
    results = _tool_results(out)

    assert results[0].content == "a" * SINGLE_RESULT_LIMIT
    assert "content offloaded" in results[1].content
    assert (Path(session.spill_dir) / "t1").read_text(encoding="utf-8") == "b" * (
        SINGLE_RESULT_LIMIT + 1
    )
