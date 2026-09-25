"""Token 估算单测：锚点 + 字符增量、用量合并与字符统计口径。"""

from __future__ import annotations

import math

from potatocode.compact import estimate_tokens, message_chars, usage_anchor
from potatocode.llm import Message, ToolCall, ToolResult, Usage


def test_estimate_tokens_empty():
    """无锚点、无消息时为 0。"""
    assert estimate_tokens(0, [], 0) == 0


def test_estimate_tokens_anchor_and_tail():
    """只对锚点之后新增的消息做字符增量估算（350 字符 → 100 token）。"""
    msgs = [
        Message(role="user", content="x" * 350),
        Message(role="user", content="y" * 350),
    ]
    assert estimate_tokens(5000, msgs, 0) == 5000 + math.ceil(700 / 3.5)
    # 锚点已覆盖第 1 条：只统计第 2 条
    assert estimate_tokens(5000, msgs, 1) == 5000 + math.ceil(350 / 3.5)


def test_estimate_tokens_anchor_msg_len_beyond_list():
    """锚点条数超过列表长度时退化为 0 增量，而不是报错。"""
    msgs = [Message(role="user", content="x" * 35)]
    assert estimate_tokens(1000, msgs, 99) == 1000


def test_estimate_tokens_large_values():
    """大锚点数值不溢出（Python int 任意精度），逻辑仍正确。"""
    msgs = [Message(role="user", content="z" * 350)]
    assert estimate_tokens(2_000_000_000, msgs, 0) == 2_000_000_000 + 100
    assert isinstance(estimate_tokens(2_000_000_000, msgs, 0), int)


def test_message_chars_counts_all_parts():
    """字符统计覆盖正文、工具调用参数与工具结果三部分。"""
    msgs = [
        Message(role="user", content="abcd"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="t1", name="read_file", input='{"path": "a.txt"}')],
        ),
        Message(
            role="tool",
            tool_results=[ToolResult(tool_call_id="t1", content="12345")],
        ),
    ]
    expected = len("abcd") + len('{"path": "a.txt"}') + len("12345")
    assert message_chars(msgs) == expected
    assert message_chars([]) == 0


def test_message_chars_handles_none_and_dict_input():
    """None 内容记 0；工具调用参数为 dict 时按紧凑 JSON 计数。"""
    msgs = [Message(role="assistant", tool_calls=[ToolCall(id="t", name="n", input={"a": 1})])]
    assert message_chars(msgs) == len('{"a":1}')


def test_usage_anchor_sum():
    """锚点等于输入 + 输出 + 缓存读 + 缓存写四项之和。"""
    u = Usage(input_tokens=1000, output_tokens=200, cache_write=50, cache_read=30)
    assert usage_anchor(u) == 1280
    assert isinstance(usage_anchor(Usage()), int)
    assert usage_anchor(Usage()) == 0
