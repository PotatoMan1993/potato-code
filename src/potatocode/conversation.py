"""会话层：进程内维护单会话多轮历史，不持久化。

历史为 user/assistant/tool 交替：assistant 回合可携带工具调用，
tool 回合携带工具执行结果（F6 回灌的基础）。
"""

from __future__ import annotations

from .llm import (
    ROLE_ASSISTANT,
    ROLE_TOOL,
    Message,
    ToolCall,
    ToolResult,
)


class Conversation:
    """单会话多轮对话历史。"""

    def __init__(self) -> None:
        self._messages: list[Message] = []

    def add_user(self, text: str) -> None:
        """追加一条用户消息。"""
        self._messages.append(Message(role="user", content=text))

    def add_assistant(self, text: str) -> None:
        """追加一条助手消息。"""
        self._messages.append(Message(role=ROLE_ASSISTANT, content=text))

    def add_assistant_with_tool_calls(self, text: str, calls: list[ToolCall]) -> None:
        """追加一条带工具调用的 assistant 回合（请求#1 的 preamble + 工具调用）。"""
        self._messages.append(Message(role=ROLE_ASSISTANT, content=text, tool_calls=list(calls)))

    def add_tool_results(self, results: list[ToolResult]) -> None:
        """追加一条工具结果回合（F6：结果回灌进历史）。"""
        self._messages.append(Message(role=ROLE_TOOL, tool_results=list(results)))

    def messages(self) -> list[Message]:
        """返回完整历史的副本（防止外部修改内部状态）。"""
        return list(self._messages)
