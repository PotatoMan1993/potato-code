"""摘要 Prompt 单测：模板结构、序列化确定性与 <summary> 解析。"""

from __future__ import annotations

from potatocode.compact.summary_prompt import (
    build_summary_prompt,
    extract_summary,
    serialize_conversation,
)
from potatocode.llm import Message, ToolCall, ToolResult

# 9 个固定小节标题（与 prompt 模板逐字一致）
SECTION_TITLES = [
    "## 1 主要请求和意图",
    "## 2 关键技术概念",
    "## 3 文件和代码段",
    "## 4 错误和修复",
    "## 5 问题解决过程",
    "## 6 所有用户消息原文",
    "## 7 待办任务",
    "## 8 当前工作",
    "## 9 可能的下一步",
]


def _sample_msgs() -> list[Message]:
    """构造一段含工具调用与工具结果的对话。"""
    return [
        Message(role="user", content="读一下 a.txt"),
        Message(
            role="assistant",
            content="好的",
            tool_calls=[ToolCall(id="c1", name="read_file", input='{"path": "a.txt"}')],
        ),
        Message(role="tool", tool_results=[ToolResult(tool_call_id="c1", content="文件内容")]),
    ]


def test_build_summary_prompt_shape():
    """摘要请求只有一条 user 消息，包含两阶段标签、9 个小节与禁止调用工具的要求。"""
    prompt = build_summary_prompt(_sample_msgs())

    assert len(prompt) == 1
    assert prompt[0].role == "user"
    body = prompt[0].content
    assert "<analysis>" in body and "</analysis>" in body
    assert "<summary>" in body and "</summary>" in body
    assert "不要调用任何工具" in body
    for title in SECTION_TITLES:
        assert title in body
    assert "[conversation]" in body


def test_build_summary_prompt_embeds_user_messages():
    """用户消息原文被原样嵌入提示词，供模型在第 6 节逐条保留。"""
    prompt = build_summary_prompt(_sample_msgs())
    assert "读一下 a.txt" in prompt[0].content


def test_serialize_conversation_covers_all_parts():
    """序列化覆盖正文、工具调用与工具结果三类内容。"""
    text = serialize_conversation(_sample_msgs())
    assert "user: 读一下 a.txt" in text
    assert "assistant: 好的" in text
    assert "[call read_file id=c1" in text
    assert "[result id=c1 is_error=False] 文件内容" in text


def test_serialize_conversation_deterministic():
    """同一段对话两次序列化逐字节相等。"""
    msgs = _sample_msgs()
    assert serialize_conversation(msgs) == serialize_conversation(msgs)
    assert serialize_conversation([]) == ""


def test_extract_summary_standard():
    """标准输出：只保留 <summary> 正文，草稿被丢弃。"""
    raw = "<analysis>草稿</analysis>\n<summary>正式摘要</summary>"
    assert extract_summary(raw) == "正式摘要"


def test_extract_summary_missing_tags():
    """缺少标签时降级返回原文，不抛异常。"""
    raw = "没有标签的摘要正文"
    assert extract_summary(raw) == raw


def test_extract_summary_nested_and_multiple():
    """多对标签时取最后一对，嵌套内容原样保留。"""
    raw = "<summary>旧摘要</summary>\n<summary>新摘要\n带 <b>标签</b></summary>"
    assert extract_summary(raw) == "新摘要\n带 <b>标签</b>"
