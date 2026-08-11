"""会话层：进程内维护单会话多轮历史（user/assistant 交替），不持久化。"""

from __future__ import annotations

from .llm import Message


class Conversation:
    """单会话多轮对话历史。"""

    def __init__(self) -> None:
        self._messages: list[Message] = []

    def add_user(self, text: str) -> None:
        """追加一条用户消息。"""
        self._messages.append(Message(role="user", content=text))

    def add_assistant(self, text: str) -> None:
        """追加一条助手消息。"""
        self._messages.append(Message(role="assistant", content=text))

    def messages(self) -> list[Message]:
        """返回完整历史的副本（防止外部修改内部状态）。"""
        return list(self._messages)
