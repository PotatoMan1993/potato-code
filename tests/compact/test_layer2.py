"""第 2 层压缩单测：近期原文边界、配对修正、摘要请求、PTL 自重试与熔断记账。"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from potatocode.compact import CompactCircuitBreaker, RecoveryState, TriggerKind
from potatocode.compact.const import PTL_RETRY_LIMIT, RECENT_KEEP_MESSAGES, RECENT_KEEP_TOKENS
from potatocode.compact.layer2 import (
    _join_after_summary,
    auto_compact,
    force_compact,
    group_by_user_turn,
    pick_recent_tail,
    ptl_retry,
    run_summary,
    summarize_once,
)
from potatocode.compact.token import message_chars
from potatocode.conversation import Conversation
from potatocode.llm import (
    ROLE_ASSISTANT,
    ROLE_TOOL,
    ROLE_USER,
    Message,
    PromptTooLongError,
    Request,
    ToolCall,
    ToolResult,
)


def _ptl() -> PromptTooLongError:
    """构造一个 PTL 哨兵异常（模拟摘要请求自身撞墙）。"""
    return PromptTooLongError("prompt is too long")


def _result(call_id: str, content: str) -> ToolResult:
    """构造一条工具结果。"""
    return ToolResult(tool_call_id=call_id, content=content)


def _tool_call(call_id: str) -> ToolCall:
    """构造一次 read_file 工具调用。"""
    return ToolCall(id=call_id, name="read_file", input='{"path": "a.txt"}')


def _multi_turn_conv(turns: int, filler: str = "") -> Conversation:
    """构造 G 轮「user → assistant」对话（每轮一组，供丢组测试计数）。"""
    conv = Conversation()
    for i in range(turns):
        conv.add_user(f"第{i}轮请求{filler}")
        conv.add_assistant(f"第{i}轮答复")
    return conv


def _groups_in_request(req: Request) -> int:
    """统计一次摘要请求里还剩下几组对话（按序列化文本中的 user 行数）。"""
    body = req.messages[-1].content.split("[conversation]\n", 1)[1]
    return sum(1 for line in body.splitlines() if line.startswith("user: "))


def _groups_in_requests(provider) -> list[int]:
    """把 fake provider 收到的每次请求折算成剩余组数序列。"""
    return [_groups_in_request(r) for r in provider.requests]


# ────────── summarize_once ──────────


async def test_summarize_once_sends_no_tools(
    fake_provider, manage_input, summary_text, summary_body
):
    """摘要请求不携带任何工具定义，且只保留 <summary> 正文。"""
    provider = fake_provider(responses=[summary_text])
    conv = Conversation()
    conv.add_user("帮我改代码")

    text = await summarize_once(manage_input(conv, provider), conv.messages())

    assert not provider.requests[0].tools  # None 或空列表都算「没传工具」
    assert provider.summarize_calls == 1
    assert text == summary_body
    assert "<analysis>" not in text


async def test_summarize_once_ptl_passthrough(fake_provider, manage_input):
    """摘要请求撞 PTL 时原样抛出哨兵异常，供上层用 isinstance 识别。"""
    provider = fake_provider(responses=[_ptl()])
    conv = Conversation()
    conv.add_user("请求")

    with pytest.raises(PromptTooLongError):
        await summarize_once(manage_input(conv, provider), conv.messages())


# ────────── 近期原文 ──────────


def test_pick_recent_tail_both_lower_bounds():
    """两个下界都满足才停手：单条已超 token 下界时仍要凑满 5 条。"""
    msgs = [Message(role=ROLE_USER, content="x" * 40000) for _ in range(6)]

    tail = pick_recent_tail(msgs)

    assert len(tail) == RECENT_KEEP_MESSAGES  # 条数下界强制保留 5 条
    assert message_chars(tail) / 3.5 >= RECENT_KEEP_TOKENS
    assert len(tail) < len(msgs)  # 仍丢弃了更旧的 1 条


def test_pick_recent_tail_keeps_everything_when_short():
    """对话比两个下界都短时全量保留（择宽），不凭空截断。"""
    msgs = [Message(role=ROLE_USER, content="短") for _ in range(3)]
    assert len(pick_recent_tail(msgs)) == 3
    assert pick_recent_tail([]) == []


def test_pick_recent_tail_pair_fix():
    """截断点落在落单的 tool_result 上时，向前推到发起调用的 assistant。"""
    big = "z" * 40000
    msgs = [
        Message(role=ROLE_USER, content="第一问"),
        Message(role=ROLE_ASSISTANT, tool_calls=[_tool_call("c1")]),
        Message(role=ROLE_TOOL, tool_results=[_result("c1", big)]),
        Message(role=ROLE_ASSISTANT, tool_calls=[_tool_call("c2")]),
        Message(role=ROLE_TOOL, tool_results=[_result("c2", big)]),
        Message(role=ROLE_ASSISTANT, tool_calls=[_tool_call("c3")]),
        Message(role=ROLE_TOOL, tool_results=[_result("c3", big)]),
    ]

    tail = pick_recent_tail(msgs)

    assert tail[0].role == ROLE_ASSISTANT  # 不可为落单的 tool
    assert tail[0].tool_calls[0].id == "c1"  # 起点前推到配对的 assistant
    assert tail[1].role == ROLE_TOOL
    assert tail[1].tool_results[0].tool_call_id == "c1"
    assert len(tail) >= RECENT_KEEP_MESSAGES
    assert message_chars(tail) / 3.5 >= RECENT_KEEP_TOKENS
    assert len(tail) <= len(msgs)


def test_join_after_summary_avoids_consecutive_user():
    """近期原文首条是 user 时插入 assistant 衔接占位，避免 user/user 连续。"""
    summary = Message(role=ROLE_USER, content="摘要")
    recent = [Message(role=ROLE_USER, content="最近的用户消息")]

    joined = _join_after_summary(summary, recent)

    assert [m.role for m in joined] == [ROLE_USER, ROLE_ASSISTANT, ROLE_USER]


def test_join_after_summary_plain_and_empty():
    """无近期原文时只返回摘要；首条是 assistant 时直接拼接。"""
    summary = Message(role=ROLE_USER, content="摘要")
    assert _join_after_summary(summary, []) == [summary]
    joined = _join_after_summary(summary, [Message(role=ROLE_ASSISTANT, content="答")])
    assert [m.role for m in joined] == [ROLE_USER, ROLE_ASSISTANT]


def test_join_after_summary_drops_orphan_tool():
    """防御性修正：首条仍是落单 tool_result 时直接丢弃。"""
    summary = Message(role=ROLE_USER, content="摘要")
    recent = [
        Message(role=ROLE_TOOL, tool_results=[_result("c9", "孤儿结果")]),
        Message(role=ROLE_ASSISTANT, content="答"),
    ]

    joined = _join_after_summary(summary, recent)

    assert [m.role for m in joined] == [ROLE_USER, ROLE_ASSISTANT]


def test_group_by_user_turn():
    """按 user 切分：标准用例切出两组，每组一轮往返。"""
    conv = _multi_turn_conv(2)
    groups = group_by_user_turn(conv.messages())
    assert len(groups) == 2
    assert all(len(g) == 2 for g in groups)


def test_group_by_user_turn_keeps_leading_non_user():
    """对话不以 user 开头时，头部消息单独成组而不被丢弃。"""
    msgs = [Message(role=ROLE_ASSISTANT, content="开场"), Message(role=ROLE_USER, content="问")]
    groups = group_by_user_turn(msgs)
    assert len(groups) == 2
    assert groups[0][0].role == ROLE_ASSISTANT


# ────────── PTL 自重试 ──────────


async def test_ptl_retry_drops_exactly_one_group_per_step(
    fake_provider, manage_input, summary_body
):
    """从 5 组起：每次丢最旧 1 组，第 3 次重试（2 组）成功。"""
    provider = fake_provider(responses=[_ptl(), _ptl(), summary_body])
    conv = _multi_turn_conv(5)

    text = await ptl_retry(manage_input(conv, provider), conv.messages(), _ptl())

    assert text == summary_body
    assert _groups_in_requests(provider) == [4, 3, 2]


async def test_ptl_retry_sequence_via_run_summary(fake_provider, manage_input, summary_text):
    """整条摘要路径的组数序列为 [G, G-1, G-2, G-3]，第 4 次成功。"""
    assert PTL_RETRY_LIMIT == 3
    provider = fake_provider(responses=[_ptl(), _ptl(), _ptl(), summary_text])
    conv = _multi_turn_conv(6)

    await run_summary(manage_input(conv, provider))

    assert _groups_in_requests(provider) == [6, 5, 4, 3]


async def test_ptl_retry_fall_to_percentage(fake_provider, manage_input):
    """超过 3 次后按 ceil(剩余 × 0.2) 丢组，且每次至少丢 1 组。"""
    provider = fake_provider(repeat=_ptl())
    conv = _multi_turn_conv(10)

    with pytest.raises(PromptTooLongError):
        await run_summary(manage_input(conv, provider))

    sequence = _groups_in_requests(provider)
    assert sequence == [10, 9, 8, 7, 5, 4, 3, 2, 1]
    # 第 5 次请求起为按比例丢弃，逐项复核丢弃量
    for prev, nxt in zip(sequence[3:-1], sequence[4:], strict=True):
        assert prev - nxt == max(1, math.ceil(prev * 0.2))


async def test_ptl_retry_stops_before_empty_messages(fake_provider, manage_input):
    """组丢光后抛异常，绝不发送 messages 为空的摘要请求。"""
    provider = fake_provider(repeat=_ptl())
    conv = _multi_turn_conv(5)

    with pytest.raises(PromptTooLongError):
        await run_summary(manage_input(conv, provider))

    counts = _groups_in_requests(provider)
    assert min(counts) == 1  # 最后一次请求仍有 1 组
    assert all(req.messages for req in provider.requests)


async def test_ptl_retry_non_ptl_error_propagates(fake_provider, manage_input):
    """中间出现非 PTL 错误立即上抛，不再继续丢组。"""
    provider = fake_provider(repeat=RuntimeError("服务 500"))
    conv = _multi_turn_conv(5)

    with pytest.raises(RuntimeError, match="服务 500"):
        await run_summary(manage_input(conv, provider))

    assert provider.calls == 1  # 没有做任何重试


# ────────── run_summary / auto_compact / force_compact ──────────


async def test_run_summary_basic(fake_provider, manage_input, summary_body):
    """摘要 + 三段恢复合并为一条 user 消息，其后接近期原文。"""
    provider = fake_provider(responses=None)  # 默认返回 9 部分摘要
    conv = _multi_turn_conv(2)
    inp = manage_input(conv, provider)

    new_msgs = await run_summary(inp)

    assert new_msgs[0].role == ROLE_USER
    assert summary_body in new_msgs[0].content
    assert "## 历史会话摘要" in new_msgs[0].content
    assert "## 最近读过的文件" in new_msgs[0].content
    assert "## 当前可用工具" in new_msgs[0].content
    assert "## 边界提示" in new_msgs[0].content
    # 近期原文首条是 user → 摘要与原文之间插入 assistant 衔接占位
    assert new_msgs[1].role == ROLE_ASSISTANT
    assert new_msgs[2:] == conv.messages()


async def test_run_summary_includes_user_messages_verbatim(fake_provider, manage_input):
    """每条用户消息原文都能在摘要请求体里逐条找到。"""
    provider = fake_provider()
    conv = _multi_turn_conv(3)

    await run_summary(manage_input(conv, provider))

    request_body = provider.requests[0].messages[-1].content
    users = [m for m in conv.messages() if m.role == ROLE_USER]
    assert users
    for m in users:
        assert m.content in request_body


async def test_run_summary_uses_recovery_snapshot(fake_provider, manage_input):
    """恢复段使用入口处的文件追踪快照。"""
    provider = fake_provider()
    recovery = RecoveryState()
    recovery.record_file("/x/note.py", "print('hi')")
    recorded_path = str(Path("/x/note.py").resolve())  # 追踪状态统一存绝对路径
    conv = _multi_turn_conv(2)

    new_msgs = await run_summary(manage_input(conv, provider, recovery=recovery))

    assert recorded_path in new_msgs[0].content
    assert "print('hi')" in new_msgs[0].content


async def test_auto_compact_records_failure_then_success(fake_provider, manage_input):
    """自动路径连续失败 3 次跳闸，成功一次立即清零。"""
    tracking = CompactCircuitBreaker()
    conv = _multi_turn_conv(2)
    bad = fake_provider(repeat=RuntimeError("500"))
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await auto_compact(manage_input(conv, bad, auto_tracking=tracking))
    assert tracking.tripped() is True

    good = fake_provider()
    new_msgs, before, after = await auto_compact(manage_input(conv, good, auto_tracking=tracking))

    assert tracking.tripped() is False
    assert before == 0
    assert after >= 0
    assert new_msgs


async def test_auto_compact_ptl_exhaustion_counts_as_failure(fake_provider, manage_input):
    """PTL 重试用光同样计一次失败。"""
    tracking = CompactCircuitBreaker()
    conv = _multi_turn_conv(4)
    provider = fake_provider(repeat=_ptl())

    with pytest.raises(PromptTooLongError):
        await auto_compact(manage_input(conv, provider, auto_tracking=tracking))

    assert tracking._consecutive_failures == 1


async def test_force_compact_ignores_tracking(fake_provider, manage_input):
    """手动 / 紧急路径成功与失败都不改熔断计数。"""
    tracking = CompactCircuitBreaker()
    conv = _multi_turn_conv(4)

    _, before, after = await force_compact(
        manage_input(conv, fake_provider(), auto_tracking=tracking)
    )
    assert before == 0 and after >= 0

    bad = fake_provider(repeat=_ptl())
    with pytest.raises(PromptTooLongError):
        await force_compact(manage_input(conv, bad, auto_tracking=tracking))
    assert tracking._consecutive_failures == 0


def test_manage_input_defaults_to_auto(manage_input, fake_provider):
    """入参工厂默认走自动路径。"""
    assert manage_input(Conversation(), fake_provider()).trigger is TriggerKind.AUTO


async def test_force_compact_prechecks_oversized_summary_prompt(
    fake_provider, manage_input, summary_body
):
    """F23：摘要请求自身超限时跳过首发，直接丢最旧一组后重试。"""
    provider = fake_provider(responses=[summary_body])
    conv = _multi_turn_conv(5, filler="x" * 30000)  # 单轮即远超预检阈值
    inp = manage_input(
        conv,
        provider,
        context_window=40000,  # 预检阈值 = 40000 - 20000 - 3000 = 17000 token
        estimated_token=500,
        trigger=TriggerKind.MANUAL,
    )

    _, before, after = await force_compact(inp)

    assert _groups_in_requests(provider) == [4]  # 首发被预检拦下，直接丢 1 组
    assert before == 500
    assert after >= 0


async def test_auto_path_does_not_precheck(fake_provider, manage_input, summary_body):
    """自动路径不做预检：首发照常带上全部消息组（撞墙才由 PTL 重试兜底）。"""
    provider = fake_provider(responses=[summary_body])
    conv = _multi_turn_conv(5, filler="x" * 30000)
    inp = manage_input(
        conv,
        provider,
        context_window=40000,
        usage_anchor=20000,
        anchor_msg_len=conv.length(),
        estimated_token=20000,
    )

    await auto_compact(inp)

    assert _groups_in_requests(provider) == [5]
