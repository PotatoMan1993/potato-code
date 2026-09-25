"""manage_context 编排单测：自动阈值、熔断跳过、手动绕过与紧急先跑第 1 层。"""

from __future__ import annotations

from pathlib import Path

import pytest

from potatocode.compact import CompactCircuitBreaker, TriggerKind, manage_context
from potatocode.compact.const import AUTO_SAFETY_MARGIN, SUMMARY_RESERVE
from potatocode.conversation import Conversation
from potatocode.llm import ROLE_TOOL, PromptTooLongError, ToolCall, ToolResult

# 默认窗口下的自动触发阈值
_THRESHOLD = 200000 - SUMMARY_RESERVE - AUTO_SAFETY_MARGIN


def _conv_with_big_result(conv: Conversation, size: int = 60000) -> Conversation:
    """给对话追加一条带超大工具结果的往返（触发第 1 层落盘）。"""
    conv.add_assistant_with_tool_calls(
        "", [ToolCall(id="big", name="read_file", input='{"path": "big.txt"}')]
    )
    conv.add_tool_results([ToolResult(tool_call_id="big", content="x" * size)])
    return conv


def _last_tool_content(conv: Conversation) -> str:
    """取会话里最后一条 RoleTool 消息的首个工具结果内容。"""
    tool_msgs = [m for m in conv.messages() if m.role == ROLE_TOOL]
    return tool_msgs[-1].tool_results[0].content


async def test_auto_triggers_on_threshold(fake_provider, manage_input, make_conv, summary_body):
    """估算 token 越过阈值 → 触发一次摘要并整体替换会话。"""
    provider = fake_provider()
    conv = make_conv()
    original_len = conv.length()
    inp = manage_input(
        conv,
        provider,
        usage_anchor=180000,
        anchor_msg_len=conv.length(),
        estimated_token=180000,
    )

    out = await manage_context(inp)

    assert provider.summarize_calls == 1
    assert conv.length() != original_len
    assert summary_body in conv.messages()[0].content
    assert out.before_tokens == 180000
    assert out.after_tokens >= 0


async def test_auto_skipped_below_threshold(fake_provider, manage_input, make_conv):
    """估算 token 未达阈值 → 不触发摘要，仅第 1 层生效。"""
    provider = fake_provider()
    conv = make_conv()
    inp = manage_input(conv, provider, usage_anchor=_THRESHOLD - 1, anchor_msg_len=conv.length())

    out = await manage_context(inp)

    assert provider.summarize_calls == 0
    assert out.after_tokens < _THRESHOLD


async def test_auto_threshold_boundary(fake_provider, manage_input, make_conv):
    """达到阈值即触发，低 1 个 token 则不触发。"""
    provider = fake_provider()
    conv = make_conv()
    inp = manage_input(conv, provider, usage_anchor=_THRESHOLD - 1, anchor_msg_len=conv.length())
    await manage_context(inp)
    assert provider.summarize_calls == 0

    provider2 = fake_provider()
    conv2 = make_conv()
    inp2 = manage_input(conv2, provider2, usage_anchor=_THRESHOLD, anchor_msg_len=conv2.length())
    await manage_context(inp2)
    assert provider2.summarize_calls == 1


async def test_auto_uses_layer1_output_for_estimate(
    fake_provider, manage_input, make_conv, session
):
    """第 1 层落盘后重估：跌回阈值以下就不再触发第 2 层。"""
    provider = fake_provider()
    conv = make_conv()
    conv = _conv_with_big_result(conv)
    # 锚点单凭自身即越过阈值，但 60000 字节工具结果落盘后估算会跌回去
    inp = manage_input(
        conv,
        provider,
        usage_anchor=_THRESHOLD - 10000,
        anchor_msg_len=1,  # 锚点只覆盖首条 user：尾部的超大工具结果计入增量
        estimated_token=_THRESHOLD + 1000,
    )

    out = await manage_context(inp)

    assert provider.summarize_calls == 0
    assert "content offloaded" in _last_tool_content(conv)
    assert len(list(Path(session.spill_dir).iterdir())) == 1
    assert out.after_tokens < _THRESHOLD


async def test_auto_skipped_when_tripped(fake_provider, manage_input, make_conv):
    """熔断跳闸后自动路径不再触发第 2 层。"""
    provider = fake_provider()
    tracking = CompactCircuitBreaker()
    for _ in range(3):
        tracking.record_failure()
    conv = make_conv()
    inp = manage_input(
        conv,
        provider,
        usage_anchor=180000,
        anchor_msg_len=conv.length(),
        auto_tracking=tracking,
    )

    await manage_context(inp)

    assert provider.summarize_calls == 0


async def test_auto_failure_records_failure(fake_provider, manage_input, make_conv):
    """自动摘要失败上抛并累加熔断计数，连续 3 次后跳闸。"""
    tracking = CompactCircuitBreaker()
    provider = fake_provider(repeat=RuntimeError("500"))
    conv = make_conv()

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await manage_context(
                manage_input(
                    conv,
                    provider,
                    usage_anchor=180000,
                    anchor_msg_len=conv.length(),
                    auto_tracking=tracking,
                )
            )

    assert tracking.tripped() is True


async def test_auto_ptl_exhaustion_counts_as_failure(fake_provider, manage_input, make_conv):
    """摘要请求 PTL 重试用光同样计入熔断。"""
    tracking = CompactCircuitBreaker()
    provider = fake_provider(repeat=PromptTooLongError("prompt is too long"))
    conv = make_conv()

    with pytest.raises(PromptTooLongError):
        await manage_context(
            manage_input(
                conv,
                provider,
                usage_anchor=180000,
                anchor_msg_len=conv.length(),
                auto_tracking=tracking,
            )
        )

    assert tracking._consecutive_failures == 1


async def test_auto_skipped_when_window_too_small(fake_provider, manage_input, make_conv):
    """窗口小于预留 + 安全余量时跳过自动第 2 层，避免每轮都触发。"""
    provider = fake_provider()
    conv = make_conv()
    inp = manage_input(
        conv,
        provider,
        context_window=SUMMARY_RESERVE + AUTO_SAFETY_MARGIN,
        usage_anchor=30000,
        anchor_msg_len=conv.length(),
        estimated_token=30000,
    )

    out = await manage_context(inp)

    assert provider.summarize_calls == 0
    assert out.before_tokens == 30000


async def test_manual_bypasses_threshold_and_layer1(
    fake_provider, manage_input, make_conv, session, summary_body
):
    """手动压缩：估算远低于阈值、且不跑第 1 层，仍无条件摘要。"""
    provider = fake_provider()
    conv = make_conv()
    conv = _conv_with_big_result(conv)
    inp = manage_input(
        conv,
        provider,
        estimated_token=500,
        usage_anchor=0,
        trigger=TriggerKind.MANUAL,
    )

    out = await manage_context(inp)

    assert provider.summarize_calls == 1
    assert out.before_tokens == 500  # 手动路径 before 取入口估算值
    assert list(Path(session.spill_dir).iterdir()) == []  # 第 1 层未执行
    assert summary_body in conv.messages()[0].content


async def test_manual_bypasses_circuit_breaker(fake_provider, manage_input, make_conv):
    """熔断跳闸状态下手动压缩照常执行。"""
    provider = fake_provider()
    tracking = CompactCircuitBreaker()
    for _ in range(3):
        tracking.record_failure()
    conv = make_conv()
    inp = manage_input(
        conv,
        provider,
        estimated_token=500,
        auto_tracking=tracking,
        trigger=TriggerKind.MANUAL,
    )

    await manage_context(inp)

    assert provider.summarize_calls == 1
    assert tracking._consecutive_failures == 3  # 手动路径不改熔断计数


async def test_emergency_runs_layer1_then_force(fake_provider, manage_input, make_conv, session):
    """紧急压缩：先强制第 1 层落盘，再无条件摘要。"""
    provider = fake_provider()
    tracking = CompactCircuitBreaker()
    for _ in range(3):
        tracking.record_failure()  # 熔断跳闸也不影响紧急路径
    conv = make_conv()
    conv = _conv_with_big_result(conv)
    inp = manage_input(conv, provider, auto_tracking=tracking, trigger=TriggerKind.EMERGENCY)

    await manage_context(inp)

    assert len(list(Path(session.spill_dir).iterdir())) == 1  # 大工具结果已落盘
    assert provider.summarize_calls == 1
    assert tracking._consecutive_failures == 3
    assert "## 历史会话摘要" in conv.messages()[0].content


async def test_emergency_failure_does_not_touch_tracking(fake_provider, manage_input, make_conv):
    """紧急路径摘要失败同样不写熔断计数，异常向上抛给主循环。"""
    tracking = CompactCircuitBreaker()
    provider = fake_provider(repeat=RuntimeError("500"))
    conv = make_conv()
    inp = manage_input(conv, provider, auto_tracking=tracking, trigger=TriggerKind.EMERGENCY)

    with pytest.raises(RuntimeError):
        await manage_context(inp)

    assert tracking._consecutive_failures == 0


async def test_manual_before_after_tokens(fake_provider, manage_input, make_conv):
    """手动路径返回的 before 是入口估算，after 是替换后重算的估算。"""
    provider = fake_provider()
    conv = make_conv()
    inp = manage_input(conv, provider, estimated_token=50000, trigger=TriggerKind.MANUAL)

    out = await manage_context(inp)

    assert out.before_tokens == 50000
    assert 0 < out.after_tokens < 50000
