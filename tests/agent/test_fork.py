"""Fork 路径辅助函数单测（ch15 T13）：消息克隆、悬空修补与上下文识别。"""

from __future__ import annotations

from potatocode.agent.fork import (
    FORK_BOILERPLATE,
    FORK_BOILERPLATE_TAG,
    build_forked_messages,
    is_fork_context,
)
from potatocode.llm import Message, ToolCall, ToolResult


def _assistant_with_calls(*call_ids: str, text: str = "") -> Message:
    return Message(
        role="assistant",
        content=text,
        tool_calls=[ToolCall(id=cid, name="bash", input="{}") for cid in call_ids],
    )


def _tool_results(*call_ids: str) -> Message:
    return Message(
        role="tool",
        tool_results=[ToolResult(tool_call_id=cid, content="ok") for cid in call_ids],
    )


def test_empty_parent_single_user_message() -> None:
    """空父对话：克隆结果只有一条 user 消息 = Boilerplate + 任务（F22）。"""
    msgs = build_forked_messages([], "统计行数")
    assert len(msgs) == 1
    assert msgs[0].role == "user"
    assert msgs[0].content.startswith(FORK_BOILERPLATE_TAG)
    assert msgs[0].content.endswith("统计行数")
    assert msgs[0].content.index(FORK_BOILERPLATE_TAG) < msgs[0].content.index("统计行数")


def test_paired_history_appends_only_user_message() -> None:
    """完整配对的历史：克隆后 == 原历史 + 一条 user 消息（前缀与父一致，N2）。"""
    parent = [
        Message(role="user", content="q"),
        Message(role="assistant", content="a"),
    ]
    msgs = build_forked_messages(parent, "task")
    assert len(msgs) == 3
    assert msgs[:2] == parent  # 深拷贝但内容一致
    assert msgs[-1].role == "user"
    assert FORK_BOILERPLATE_TAG in msgs[-1].content


def test_dangling_tool_use_patched() -> None:
    """末尾 assistant 有 2 个未配对 tool_use：补 1 条 tool 回合（含 2 个占位结果）再补 user（F22-②）。"""
    parent = [
        Message(role="user", content="q"),
        _assistant_with_calls("c1", "c2"),
    ]
    msgs = build_forked_messages(parent, "task")
    # 父 2 条 + 占位 tool 回合 1 条 + user 1 条
    assert len(msgs) == 4
    tool_msg = msgs[2]
    assert tool_msg.role == "tool"
    assert [r.tool_call_id for r in tool_msg.tool_results] == ["c1", "c2"]
    assert all(r.is_error for r in tool_msg.tool_results)
    assert msgs[-1].role == "user"


def test_dangling_subset_only_unconsumed_patched() -> None:
    """部分消费：只补未被 tool 回合消费的调用（F22-② 配对检查）。"""
    parent = [
        Message(role="user", content="q"),
        _assistant_with_calls("c1", "c2"),
        _tool_results("c1"),
    ]
    msgs = build_forked_messages(parent, "task")
    tool_msg = msgs[3]
    assert tool_msg.role == "tool"
    assert [r.tool_call_id for r in tool_msg.tool_results] == ["c2"]
    assert msgs[-1].role == "user"


def test_earlier_paired_calls_untouched() -> None:
    """历史中段的配对调用不受影响，只在末尾追加 user 消息。"""
    parent = [
        Message(role="user", content="q"),
        _assistant_with_calls("c1"),
        _tool_results("c1"),
        Message(role="assistant", content="done"),
    ]
    msgs = build_forked_messages(parent, "task")
    assert len(msgs) == 5
    assert msgs[2].tool_results[0].content == "ok"  # 原结果保留
    assert not any(m.role == "tool" and m is not msgs[2] for m in msgs)


def test_clone_is_deep_copy() -> None:
    """修改克隆列表不影响父消息对象（互不影响父对话状态，F22-①）。"""
    parent = [Message(role="user", content="q")]
    msgs = build_forked_messages(parent, "task")
    msgs[0].content = "mutated"
    assert parent[0].content == "q"


def test_is_fork_context_positive_and_negative() -> None:
    assert is_fork_context(build_forked_messages([], "t")) is True
    assert is_fork_context([Message(role="user", content=FORK_BOILERPLATE)]) is True
    assert is_fork_context([Message(role="user", content="普通消息")]) is False
    assert is_fork_context([]) is False
