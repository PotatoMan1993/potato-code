"""anthropic 系统块序列化单测：稳定块带缓存断点、环境块不带（守护回归，AC4/F3）。"""

from __future__ import annotations

from potatocode.llm.anthropic_provider import (
    _append_reminder_anthropic,
    _build_system_blocks,
)


def test_stable_block_has_cache_control():
    """稳定块序列化带 cache_control: ephemeral（断点）。"""
    blocks = _build_system_blocks("STABLE", "")
    assert blocks == [{"type": "text", "text": "STABLE", "cache_control": {"type": "ephemeral"}}]


def test_environment_block_has_no_cache_control():
    """环境块序列化不带 cache_control（不进缓存）。"""
    blocks = _build_system_blocks("STABLE", "ENV")
    assert blocks[1] == {"type": "text", "text": "ENV"}
    assert "cache_control" not in blocks[1]


def test_empty_blocks_omitted():
    """空 stable / 空 environment 不产生对应块。"""
    assert _build_system_blocks("", "") == []
    assert _build_system_blocks("", "ENV") == [{"type": "text", "text": "ENV"}]
    assert _build_system_blocks("STABLE", "") == [
        {"type": "text", "text": "STABLE", "cache_control": {"type": "ephemeral"}}
    ]


def test_append_reminder_to_trailing_user_string():
    """末条为 user 且 content 为字符串：转块列表后追加 reminder 文本块（N3）。"""
    msgs = [{"role": "user", "content": "hi"}]
    _append_reminder_anthropic(msgs, "<system-reminder>R</system-reminder>")
    assert msgs == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "text", "text": "<system-reminder>R</system-reminder>"},
            ],
        }
    ]


def test_append_reminder_to_tool_result_user():
    """末条为 user 且 content 为块列表（tool_result）：追加文本块保持合法（N3）。"""
    msgs = [
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "ok"}],
        }
    ]
    _append_reminder_anthropic(msgs, "REMINDER")
    assert msgs[0]["content"][-1] == {"type": "text", "text": "REMINDER"}


def test_append_reminder_new_user_when_trailing_assistant():
    """末条非 user：新起一条 user 消息承载 reminder。"""
    msgs = [{"role": "assistant", "content": "a"}]
    _append_reminder_anthropic(msgs, "REMINDER")
    assert msgs[-1] == {"role": "user", "content": "REMINDER"}


def test_append_reminder_empty_noop():
    """空 reminder 不改变消息序列。"""
    msgs = [{"role": "user", "content": "hi"}]
    _append_reminder_anthropic(msgs, "")
    assert msgs == [{"role": "user", "content": "hi"}]
