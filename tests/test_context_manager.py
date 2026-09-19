"""真实切分、混合重试、候选预算与会话原子性的控制器测试。"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from context_helpers import (
    ContextProvider,
    substantial_conversation,
    summary_events,
    summary_prefix_length,
)
from potatocode.context.manager import ContextManager
from potatocode.context.types import (
    AttachmentBundle,
    CompactResult,
    ContextEvent,
    FileAttachment,
    PreparedRequest,
    RequestFrame,
    RunScope,
    SkillAttachment,
    TokenEstimate,
)
from potatocode.conversation import Conversation, legal_boundaries
from potatocode.llm import StreamEvent, System, ToolCall, ToolResult, Usage
from potatocode.llm.errors import ContextWindowExceeded
from potatocode.permission import Mode, new_engine


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch):
    monkeypatch.setattr("potatocode.context.manager.RETRY_DELAYS", (0.001, 0.001))


def manager_for(tmp_path, conv=None):
    engine, _ = new_engine(str(tmp_path))
    return ContextManager(conv if conv is not None else substantial_conversation(), engine)


def frame(provider):
    return RequestFrame(
        System("普通系统"), max_output_tokens=provider.context_limits.max_output_tokens
    )


async def compact(manager, provider, *, scope=None, reason="manual", cancel=None):
    events = [
        e
        async for e in manager.compact(
            provider,
            frame(provider),
            Mode.DEFAULT,
            cancel or asyncio.Event(),
            scope or RunScope(),
            reason,
        )
    ]
    assert isinstance(events[-1], CompactResult)
    assert not manager._lock.locked()
    return events[-1], events


async def preflight(manager, provider, scope=None):
    events = [
        e
        async for e in manager.preflight(
            provider, frame(provider), Mode.DEFAULT, asyncio.Event(), scope or RunScope()
        )
    ]
    assert isinstance(events[-1], PreparedRequest)
    assert not manager._lock.locked()
    return events[-1], events


def test_select_cut_is_maximal_and_does_not_pin_entire_current_user_task(tmp_path):
    conv = Conversation()
    conv.add_user("持续执行本项任务")
    for i in range(8):
        conv.add_assistant_with_tool_calls("读取下一项", [ToolCall(f"c{i}", "read_file", "{}")])
        conv.add_tool_results([ToolResult(f"c{i}", "文" * 3200)])
        conv.add_assistant("继续")
    manager, provider = manager_for(tmp_path, conv), ContextProvider()
    snapshot = conv.snapshot()
    cut = manager.select_cut(snapshot, provider)
    assert cut is not None and cut.m > 1
    assert cut.m + cut.n == len(snapshot.entries) and cut.n >= 5
    assert cut.recent_tokens > 10000
    assert cut.m in legal_boundaries(snapshot.entries)
    assert snapshot.entries[cut.m].message.role != "tool"
    # 预算足够，此例最大切点仅受近期原文与工具配对约束。
    costs = manager.estimator.history_prefix_costs(provider, snapshot)
    candidates = [
        m
        for m in legal_boundaries(snapshot.entries)
        if 0 < m <= len(snapshot.entries) - 5
        and snapshot.entries[m].message.role != "tool"
        and costs[-1] - costs[m] > 10000
    ]
    assert cut.m == max(candidates)


@pytest.mark.parametrize("count,size", [(4, 4000), (12, 1), (5, 4000)])
async def test_strict_recent_requirements_skip_without_calling_summary(tmp_path, count, size):
    conv = substantial_conversation(count, size)
    manager, provider = manager_for(tmp_path, conv), ContextProvider()
    before = conv.snapshot()
    result, _ = await compact(manager, provider)
    assert result.status == "skipped" and not provider.requests
    assert conv.snapshot() == before


async def test_success_replaces_only_prefix_with_five_part_user_reconstruction(tmp_path):
    manager, provider = manager_for(tmp_path), ContextProvider()
    before = manager.conv.snapshot()
    result, events = await compact(manager, provider)
    assert result.status == "success" and result.attempts == 1
    after = manager.conv.snapshot()
    assert after.entries[1:] == before.entries[result.m :]
    assert after.entries[0].origin == "summary" and after.entries[0].message.role == "user"
    text = after.entries[0].message.content
    for required in (
        "按用户要求整理上下文",
        "用户的主要意图",
        "最近使用的文件及内容",
        "使用过的 Skill",
        "如需精确的代码",
    ):
        assert required in text
    assert "不应出现在历史里的草稿" not in text
    assert result.after < result.before and result.after < 95000
    assert [
        e.record.status for e in events if isinstance(e, ContextEvent) and e.kind == "usage"
    ] == ["success"]
    assert manager.ledger.anchor is None


@pytest.mark.parametrize("observed,expected_calls", [(94999, 0), (95000, 1)])
async def test_auto_trigger_uses_usage_anchor_at_exact_threshold(
    tmp_path, observed, expected_calls
):
    manager, provider = manager_for(tmp_path), ContextProvider()
    snapshot = manager.conv.snapshot()
    request = frame(provider).request(snapshot)
    rid = manager.ledger.begin(
        provider, request, snapshot, 0, len(snapshot.entries), 1, TokenEstimate(observed)
    )
    manager.ledger.observe(rid, Usage(observed, 10))
    manager.ledger.finish(rid, "success", "stop")
    prepared, _ = await preflight(manager, provider)
    assert prepared.request is not None
    assert len(provider.requests) == expected_calls


async def test_output_reserve_can_trigger_below_automatic_threshold(tmp_path):
    manager, provider = manager_for(tmp_path), ContextProvider(output=90000)
    before = manager.estimator.estimate_fresh(
        provider, frame(provider).request(manager.conv.snapshot())
    )
    assert before < 95000 and before + 90000 > 128000
    prepared, _ = await preflight(manager, provider)
    assert len(provider.requests) == 1 and prepared.request is not None
    assert prepared.estimate.tokens + 90000 <= 128000


async def test_mixed_failures_share_three_attempts_and_only_overflow_moves_cut(tmp_path):
    scripts = [
        [StreamEvent(err=RuntimeError("网络失败"))],
        [StreamEvent(err=ContextWindowExceeded())],
        summary_events("无效JSON"),
    ]
    manager, provider = manager_for(tmp_path), ContextProvider(summary_scripts=scripts)
    scope = RunScope()
    snapshot = manager.conv.snapshot()
    result, events = await compact(manager, provider, scope=scope)
    assert result.status == "failed" and result.attempts == 3 and scope.circuit_broken
    assert len(provider.requests) == 3
    lengths = [summary_prefix_length(req) for req in provider.requests]
    assert lengths[0] == lengths[1] and lengths[2] < lengths[1]
    costs = manager.estimator.history_prefix_costs(provider, snapshot)
    assert costs[lengths[2]] <= costs[lengths[1]] * 9 // 10
    assert manager.conv.snapshot() == snapshot
    assert len([e for e in events if isinstance(e, ContextEvent) and e.kind == "retry"]) == 2
    again, _ = await compact(manager, provider, scope=scope)
    assert again.status == "failed" and len(provider.requests) == 3


async def test_each_input_overflow_retry_reduces_early_tokens_at_least_ten_percent(tmp_path):
    manager = manager_for(tmp_path)
    provider = ContextProvider(summary_scripts=[[StreamEvent(err=ContextWindowExceeded())]] * 3)
    snapshot = manager.conv.snapshot()
    result, _ = await compact(manager, provider)
    assert result.attempts == 3 and result.status == "failed"
    lengths = [summary_prefix_length(req) for req in provider.requests]
    costs = manager.estimator.history_prefix_costs(provider, snapshot)
    assert all(costs[b] <= costs[a] * 9 // 10 for a, b in zip(lengths, lengths[1:], strict=False))
    assert manager.conv.snapshot() == snapshot


async def test_overflow_with_no_smaller_legal_prefix_stops_early(tmp_path):
    manager = manager_for(tmp_path, substantial_conversation(6))
    provider = ContextProvider(summary_scripts=[[StreamEvent(err=ContextWindowExceeded())]])
    result, _ = await compact(manager, provider)
    assert result.status == "failed" and result.attempts == 1
    assert len(provider.requests) == 1


async def test_summary_success_but_unfittable_recent_history_never_commits(tmp_path):
    manager, provider = manager_for(tmp_path), ContextProvider(window=50000)
    before = manager.conv.snapshot()
    result, _ = await compact(manager, provider)
    assert result.status == "failed" and result.attempts == 1
    assert len(provider.requests) == 1
    assert manager.conv.snapshot() == before


async def test_oldest_file_is_omitted_before_skill_or_recent_file(tmp_path):
    manager, provider = manager_for(tmp_path), ContextProvider()
    bundle = AttachmentBundle(
        files=[FileAttachment("new.py", "N" * 1000), FileAttachment("old.py", "旧" * 20000)],
        skills=[SkillAttachment("SKILL.md", "规" * 5000)],
    )
    manager.attachments.collect = AsyncMock(return_value=bundle)
    result, _ = await compact(manager, provider)
    assert result.status == "success"
    assert bundle.files[1].text == "" and bundle.files[1].omitted_reason
    assert bundle.files[0].text and bundle.skills[0].text


async def test_latest_oversized_user_message_remains_verbatim_when_cannot_compact(tmp_path):
    conv = Conversation()
    text = "原" * 70000
    conv.add_user(text)
    manager, provider = manager_for(tmp_path, conv), ContextProvider()
    prepared, _ = await preflight(manager, provider)
    assert prepared.request is None and not provider.requests
    assert conv.messages()[0].content == text


async def test_low_anchor_does_not_falsely_reject_emergency_progress(tmp_path):
    manager, provider = manager_for(tmp_path), ContextProvider()
    snapshot = manager.conv.snapshot()
    request = frame(provider).request(snapshot)
    rid = manager.ledger.begin(
        provider, request, snapshot, 0, len(snapshot.entries), 1, TokenEstimate(1)
    )
    manager.ledger.observe(rid, Usage(1, 1))
    manager.ledger.finish(rid, "success", "stop")
    result, _ = await compact(manager, provider, reason="emergency")
    assert result.status == "success" and result.after > 1


async def test_revision_change_during_summary_cannot_overwrite_new_messages(tmp_path):
    manager = manager_for(tmp_path)

    def mutate(_):
        manager.conv.add_user("新加入的要求必须保留")
        return summary_events()

    provider = ContextProvider(summary_scripts=[mutate])
    before = manager.conv.snapshot()
    result, _ = await compact(manager, provider)
    assert result.status == "failed" and result.attempts == 1
    assert manager.conv.snapshot().entries[: len(before.entries)] == before.entries
    assert manager.conv.messages()[-1].content == "新加入的要求必须保留"


async def test_cancel_silent_summary_preserves_history_and_allows_next_operation(tmp_path):
    manager = manager_for(tmp_path)
    provider = ContextProvider(
        summary_scripts=[[StreamEvent(usage=Usage(100, 1)), asyncio.Event()]]
    )
    before, cancel = manager.conv.snapshot(), asyncio.Event()
    task = asyncio.create_task(compact(manager, provider, cancel=cancel))
    await asyncio.wait_for(provider.waiting.wait(), 1)
    cancel.set()
    result, _ = await asyncio.wait_for(task, 1)
    assert result.status == "cancelled" and manager.conv.snapshot() == before
    assert manager.ledger.records()[0].output_tokens == 1
    result, _ = await compact(manager, provider)
    assert result.status == "success"


async def test_normal_skip_over_threshold_can_proceed_if_actual_window_fits(tmp_path):
    conv = substantial_conversation(4, 14000)
    manager, provider = manager_for(tmp_path, conv), ContextProvider()
    prepared, events = await preflight(manager, provider)
    assert prepared.estimate.tokens >= 95000
    assert prepared.request is not None and not provider.requests
    assert any(isinstance(e, ContextEvent) and e.kind == "skipped" for e in events)


async def test_second_compaction_replaces_previous_wrapper_and_retains_recent_ids(tmp_path):
    manager, provider = manager_for(tmp_path), ContextProvider()
    first, _ = await compact(manager, provider)
    assert first.status == "success"
    for i in range(6):
        manager.conv.add_user(f"新增要求{i}" + "文" * 2200)
    before = manager.conv.snapshot()
    second, _ = await compact(manager, provider)
    assert second.status == "success"
    after = manager.conv.snapshot()
    assert after.entries[1:] == before.entries[second.m :]
    assert sum(e.origin == "summary" for e in after.entries) == 1
    assert after.entries[0].message.content.count("本会话延续之前的对话") == 1


async def test_light_failure_prevents_request_and_preserves_original_history(tmp_path):
    conv = Conversation()
    conv.add_user("读取")
    conv.add_assistant_with_tool_calls("", [ToolCall("c", "read_file", "{}")])
    conv.add_tool_results([ToolResult("c", "x" * 50001)])
    manager, provider = manager_for(tmp_path, conv), ContextProvider()
    manager.artifacts.prepare = AsyncMock(side_effect=OSError("存盘失败"))
    before = conv.snapshot()
    prepared, _ = await preflight(manager, provider)
    assert prepared.request is None and "存盘失败" in prepared.blocked_reason
    assert conv.snapshot() == before and not provider.requests


async def test_final_auto_event_is_delivered_after_lock_release(tmp_path):
    manager, provider = manager_for(tmp_path), ContextProvider(output=90000)
    async for event in manager.preflight(
        provider, frame(provider), Mode.DEFAULT, asyncio.Event(), RunScope()
    ):
        if isinstance(event, ContextEvent) and event.kind == "success":
            assert not manager._lock.locked()
