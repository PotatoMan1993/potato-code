"""conversation 模块单测：多轮历史的顺序、role 与副本语义。"""

from __future__ import annotations

from potatocode.conversation import Conversation
from potatocode.llm import Message, ToolCall, ToolResult


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


def test_tool_turn_sequence():
    """工具调用回合 + 工具结果回合按序落地，内容正确（F6）。"""
    conv = Conversation()
    conv.add_user("读一下配置文件")
    conv.add_assistant_with_tool_calls(
        "我来读取。",
        [ToolCall(id="call_1", name="read_file", input='{"path": "pyproject.toml"}')],
    )
    conv.add_tool_results([ToolResult(tool_call_id="call_1", content="已读取，内容如下")])
    conv.add_assistant("配置已就绪。")

    msgs = conv.messages()
    assert [m.role for m in msgs] == ["user", "assistant", "tool", "assistant"]
    # assistant 回合携带工具调用
    assert msgs[1].content == "我来读取。"
    assert [c.id for c in msgs[1].tool_calls] == ["call_1"]
    assert msgs[1].tool_calls[0].name == "read_file"
    assert msgs[1].tool_calls[0].input == '{"path": "pyproject.toml"}'
    # tool 回合携带执行结果
    assert msgs[2].tool_results[0].tool_call_id == "call_1"
    assert msgs[2].tool_results[0].content == "已读取，内容如下"
    assert msgs[2].tool_results[0].is_error is False


def test_messages_returns_copy():
    """messages() 返回副本：外部修改不影响内部历史。"""
    conv = Conversation()
    conv.add_user("第一条")

    snapshot = conv.messages()
    snapshot.clear()  # 清空副本

    assert len(conv.messages()) == 1  # 内部历史不受影响


def test_last_role():
    """last_role：空历史返回 ""，否则返回末尾消息的 role（F6 终止收尾）。"""
    conv = Conversation()
    assert conv.last_role() == ""

    conv.add_user("读一下配置文件")
    assert conv.last_role() == "user"

    conv.add_assistant_with_tool_calls(
        "我来读取。",
        [ToolCall(id="call_1", name="read_file", input='{"path": "pyproject.toml"}')],
    )
    assert conv.last_role() == "assistant"

    conv.add_tool_results([ToolResult(tool_call_id="call_1", content="已读取")])
    assert conv.last_role() == "tool"

    conv.add_assistant("配置已就绪。")
    assert conv.last_role() == "assistant"


def test_replace_history_deep_copy():
    """replace_history 深拷贝入参：之后修改原列表不影响会话内容。"""
    conv = Conversation()
    conv.add_user("旧消息")

    msgs = [Message(role="user", content="摘要"), Message(role="assistant", content="继续")]
    conv.replace_history(msgs)

    msgs.clear()
    msgs.append(Message(role="user", content="外部乱改"))

    after = conv.messages()
    assert [m.content for m in after] == ["摘要", "继续"]


def test_replace_history_copies_nested_lists():
    """深拷贝覆盖 tool_calls / tool_results 子列表，外部改动不回流。"""
    conv = Conversation()
    calls = [ToolCall(id="c1", name="read_file", input="{}")]
    results = [ToolResult(tool_call_id="c1", content="内容")]
    conv.replace_history(
        [
            Message(role="assistant", tool_calls=calls),
            Message(role="tool", tool_results=results),
        ]
    )

    calls.clear()
    results[0].content = "被改掉了"

    msgs = conv.messages()
    assert msgs[0].tool_calls[0].id == "c1"
    assert msgs[1].tool_results[0].content == "内容"


def test_replace_history_empty_and_none():
    """传 None / 空列表都视为清空历史，不抛异常。"""
    conv = Conversation()
    conv.add_user("旧消息")

    conv.replace_history(None)
    assert conv.messages() == []

    conv.add_user("再来一条")
    conv.replace_history([])
    assert conv.messages() == []
    assert conv.length() == 0


def test_length_counts_messages():
    """length 返回消息条数（用量锚点记录当时的条数）。"""
    conv = Conversation()
    assert conv.length() == 0
    conv.add_user("一")
    conv.add_assistant("二")
    assert conv.length() == 2


# ────────── 持久化回调（ch11 F44/AC28） ──────────


def test_on_append_fires_for_every_add():
    """四种追加各自触发一次回调，参数是刚落地的那条消息（AC28）。"""
    seen: list[Message] = []
    conv = Conversation(on_append=seen.append)

    conv.add_user("问")
    conv.add_assistant("答")
    conv.add_assistant_with_tool_calls("调用", [ToolCall(id="c1", name="read_file", input="{}")])
    conv.add_tool_results([ToolResult(tool_call_id="c1", content="结果")])

    assert [m.role for m in seen] == ["user", "assistant", "assistant", "tool"]
    assert seen[0].content == "问"
    assert seen[2].tool_calls[0].id == "c1"
    assert seen[3].tool_results[0].content == "结果"
    assert len(seen) == 4


def test_on_replace_fires_with_full_list():
    """整体替换后回调收到替换后的完整消息列表（AC28）。"""
    captured: list[list[Message]] = []
    conv = Conversation(on_replace=captured.append)
    conv.add_user("旧消息")

    conv.replace_history(
        [Message(role="user", content="摘要"), Message(role="assistant", content="继续")]
    )

    assert len(captured) == 1
    assert [m.content for m in captured[0]] == ["摘要", "继续"]


def test_on_replace_not_fired_when_content_unchanged():
    """内容没变的整体替换不通知持久化层（第 1 层压缩每轮都会走到这里）。"""
    captured: list[list[Message]] = []
    conv = Conversation(on_replace=captured.append)
    conv.add_user("你好")
    conv.add_assistant("你好！")

    conv.replace_history(conv.messages())  # 逐字段等价的一份新列表

    assert captured == []
    assert [m.content for m in conv.messages()] == ["你好", "你好！"]  # 历史仍然完好

    conv.replace_history([Message(role="user", content="真的变了")])
    assert len(captured) == 1


def test_from_messages_attaches_callbacks():
    """from_messages 构造的会话同样挂载回调（恢复场景）。"""
    appended: list[Message] = []
    conv = Conversation.from_messages(
        [Message(role="user", content="历史")], on_append=appended.append
    )

    assert [m.content for m in conv.messages()] == ["历史"]
    conv.add_user("新的一轮")
    assert [m.content for m in appended] == ["新的一轮"]


def test_no_callbacks_keeps_previous_behaviour():
    """不传回调时行为与之前一致：追加、替换、深拷贝语义都不变（AC28 向后兼容）。"""
    conv = Conversation()
    conv.add_user("一")
    conv.add_assistant("二")
    assert conv.length() == 2

    msgs = [Message(role="user", content="摘要")]
    conv.replace_history(msgs)
    msgs.append(Message(role="user", content="外部乱改"))

    assert [m.content for m in conv.messages()] == ["摘要"]
