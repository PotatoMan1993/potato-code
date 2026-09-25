"""会话层：进程内维护单会话多轮历史，不持久化。

历史为 user/assistant/tool 交替：assistant 回合可携带工具调用，
tool 回合携带工具执行结果（F6 回灌的基础）。

历史可能被 Agent 主循环、TUI 命令路径与上下文压缩并发读写（replace_history
与 messages 同时发生会读到半截列表），因此内部用 RLock 保护每次读写。
"""

from __future__ import annotations

import copy
import threading

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
        # RLock 保护：压缩路径 replace_history 与主循环 messages 可能并发
        self._lock = threading.RLock()
        self._messages: list[Message] = []

    def add_user(self, text: str) -> None:
        """追加一条用户消息。"""
        with self._lock:
            self._messages.append(Message(role="user", content=text))

    def add_assistant(self, text: str) -> None:
        """追加一条助手消息。"""
        with self._lock:
            self._messages.append(Message(role=ROLE_ASSISTANT, content=text))

    def add_assistant_with_tool_calls(self, text: str, calls: list[ToolCall]) -> None:
        """追加一条带工具调用的 assistant 回合（请求#1 的 preamble + 工具调用）。"""
        with self._lock:
            self._messages.append(
                Message(role=ROLE_ASSISTANT, content=text, tool_calls=list(calls))
            )

    def add_tool_results(self, results: list[ToolResult]) -> None:
        """追加一条工具结果回合（F6：结果回灌进历史）。"""
        with self._lock:
            self._messages.append(Message(role=ROLE_TOOL, tool_results=list(results)))

    def messages(self) -> list[Message]:
        """返回完整历史的副本（防止外部修改内部状态）。"""
        with self._lock:
            return list(self._messages)

    def length(self) -> int:
        """返回历史条数（用量锚点记录当时的条数，供增量估算划界）。"""
        with self._lock:
            return len(self._messages)

    def replace_history(self, msgs: list[Message] | None) -> None:
        """整体替换内存历史（压缩后用「摘要 + 恢复 + 近期原文」覆盖旧对话）。

        深拷贝入参，既不暴露内部列表引用，也不让调用方后续改动影响会话。
        """
        with self._lock:
            self._messages = copy.deepcopy(msgs or [])

    def last_role(self) -> str:
        """返回最后一条消息的 role；空历史返回 ""（终止收尾判断角色尾巴用，F6）。"""
        with self._lock:
            return self._messages[-1].role if self._messages else ""
