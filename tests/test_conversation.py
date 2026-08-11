"""conversation 模块单测：多轮历史的顺序、role 与副本语义。"""

from __future__ import annotations

from potatocode.conversation import Conversation


def test_empty_at_start():
    """新会话历史为空。"""
    assert Conversation().messages() == []


def test_multi_turn_order_and_roles():
    """连续多轮追加后，顺序与 role 正确。"""
    conv = Conversation()
    conv.add_user("你好")
    conv.add_assistant("你好！有什么可以帮你？")
    conv.add_user("我叫小明")
    conv.add_assistant("好的，小明。")

    msgs = conv.messages()
    assert [m.role for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert [m.content for m in msgs] == [
        "你好",
        "你好！有什么可以帮你？",
        "我叫小明",
        "好的，小明。",
    ]


def test_messages_returns_copy():
    """messages() 返回副本：外部修改不影响内部历史。"""
    conv = Conversation()
    conv.add_user("第一条")

    snapshot = conv.messages()
    snapshot.clear()  # 清空副本

    assert len(conv.messages()) == 1  # 内部历史不受影响
