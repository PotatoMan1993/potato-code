"""会话层：进程内维护单会话多轮历史，可挂载持久化回调。

历史为 user/assistant/tool 交替：assistant 回合可携带工具调用，
tool 回合携带工具执行结果（F6 回灌的基础）。

历史可能被 Agent 主循环、TUI 命令路径与上下文压缩并发读写（replace_history
与 messages 同时发生会读到半截列表），因此内部用 RLock 保护每次读写。

持久化以两个可选回调实现（ch11 F44）：`on_append` 在每次追加消息后调用，
`on_replace` 在历史被整体改写后调用。回调在锁外调用——回调内部可能做磁盘 I/O，
持锁调用会把压缩路径一起堵住。未注入回调时行为与之前完全一致。
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable

from .llm import (
    ROLE_ASSISTANT,
    ROLE_TOOL,
    Message,
    ToolCall,
    ToolResult,
)

# 追加回调：收到刚落地的那条消息（副本语义由实现方自行决定，只读不改）
AppendHook = Callable[[Message], None]
# 整体替换回调：收到替换后的完整消息列表
ReplaceHook = Callable[[list[Message]], None]


class Conversation:
    """单会话多轮对话历史。"""

    def __init__(
        self,
        on_append: AppendHook | None = None,
        on_replace: ReplaceHook | None = None,
    ) -> None:
        # RLock 保护：压缩路径 replace_history 与主循环 messages 可能并发
        self._lock = threading.RLock()
        self._messages: list[Message] = []
        self._on_append = on_append
        self._on_replace = on_replace

    @classmethod
    def from_messages(
        cls,
        msgs: list[Message],
        on_append: AppendHook | None = None,
        on_replace: ReplaceHook | None = None,
    ) -> Conversation:
        """用已有消息列表创建会话（恢复场景），可同时挂载持久化回调。"""
        conv = cls(on_append=on_append, on_replace=on_replace)
        conv._messages = list(msgs)
        return conv

    def add_user(self, text: str) -> None:
        """追加一条用户消息。"""
        msg = Message(role="user", content=text)
        with self._lock:
            self._messages.append(msg)
        self._notify_append(msg)

    def add_assistant(self, text: str) -> None:
        """追加一条助手消息。"""
        msg = Message(role=ROLE_ASSISTANT, content=text)
        with self._lock:
            self._messages.append(msg)
        self._notify_append(msg)

    def add_assistant_with_tool_calls(self, text: str, calls: list[ToolCall]) -> None:
        """追加一条带工具调用的 assistant 回合（请求#1 的 preamble + 工具调用）。"""
        msg = Message(role=ROLE_ASSISTANT, content=text, tool_calls=list(calls))
        with self._lock:
            self._messages.append(msg)
        self._notify_append(msg)

    def add_tool_results(self, results: list[ToolResult]) -> None:
        """追加一条工具结果回合（F6：结果回灌进历史）。"""
        msg = Message(role=ROLE_TOOL, tool_results=list(results))
        with self._lock:
            self._messages.append(msg)
        self._notify_append(msg)

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

        内容与当前完全一致时不触发 `on_replace`：第 1 层压缩每轮都会走这条路，
        若每轮都通知持久化层，会话存档会被无意义的改写记录灌满（ch11 F47）。
        """
        new = copy.deepcopy(msgs or [])
        with self._lock:
            changed = new != self._messages
            if changed:
                self._messages = new
                snapshot = list(self._messages)
            else:
                snapshot = []
        if changed:
            self._notify_replace(snapshot)

    def last_role(self) -> str:
        """返回最后一条消息的 role；空历史返回 ""（终止收尾判断角色尾巴用，F6）。"""
        with self._lock:
            return self._messages[-1].role if self._messages else ""

    def _notify_append(self, msg: Message) -> None:
        """锁外通知追加回调（回调异常不回流，避免拖垮主循环）。"""
        if self._on_append is not None:
            self._on_append(msg)

    def _notify_replace(self, msgs: list[Message]) -> None:
        """锁外通知整体替换回调。"""
        if self._on_replace is not None:
            self._on_replace(msgs)
