"""会话历史：隔离快照、可追溯来源与带版本检查的原子替换。"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, replace
from typing import Literal

from .llm import Message, ToolCall, ToolResult

Origin = Literal["user_input", "assistant", "tool", "control", "summary"]


@dataclass(frozen=True)
class UserQuote:
    source_id: str
    text: str


@dataclass(frozen=True)
class HistoryEntry:
    id: str
    message: Message
    origin: Origin
    content_hash: str
    verified_quotes: tuple[UserQuote, ...] = ()


@dataclass(frozen=True)
class HistorySnapshot:
    session_id: str
    revision: int
    content_epoch: int
    entries: tuple[HistoryEntry, ...]

    def messages(self) -> list[Message]:
        return [copy.deepcopy(e.message) for e in self.entries]


@dataclass(frozen=True)
class ToolResultPatch:
    entry_id: str
    result_index: int
    result: ToolResult


def message_hash(message: Message) -> str:
    text = json.dumps(asdict(message), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def legal_boundaries(entries: tuple[HistoryEntry, ...]) -> set[int]:
    """只返回已完整闭合工具调用的边界；非法历史直接拒绝。"""
    pending: set[str] = set()
    boundaries = {0}
    for i, entry in enumerate(entries):
        msg = entry.message
        if pending and msg.role != "tool":
            raise ValueError("工具调用尚未收到完整结果")
        if msg.role == "tool":
            ids = [r.tool_call_id for r in msg.tool_results]
            if not pending or len(ids) != len(set(ids)) or set(ids) != pending:
                raise ValueError("工具结果与调用不配对")
            pending.clear()
        elif msg.tool_calls:
            ids = [c.id for c in msg.tool_calls]
            if msg.role != "assistant" or len(ids) != len(set(ids)) or not all(ids):
                raise ValueError("工具调用标识无效")
            pending.update(ids)
        if not pending:
            boundaries.add(i + 1)
    if pending:
        raise ValueError("历史末尾存在未完成的工具调用")
    return boundaries


class Conversation:
    """仅在当前进程维护会话；追加不使已有输入锚点失效。"""

    def __init__(self) -> None:
        self.session_id = uuid.uuid4().hex
        self.revision = 0
        self.content_epoch = 0
        self._next_id = 0
        self._entries: list[HistoryEntry] = []

    def _entry(
        self, message: Message, origin: Origin, quotes: tuple[UserQuote, ...] = ()
    ) -> HistoryEntry:
        self._next_id += 1
        msg = copy.deepcopy(message)
        return HistoryEntry(str(self._next_id), msg, origin, message_hash(msg), quotes)

    def _append(self, message: Message, origin: Origin) -> None:
        self._entries.append(self._entry(message, origin))
        self.revision += 1

    def add_user(self, text: str, origin: Origin = "user_input") -> None:
        self._append(Message(role="user", content=text), origin)

    def add_assistant(self, text: str) -> None:
        self._append(Message(role="assistant", content=text), "assistant")

    def add_assistant_with_tool_calls(self, text: str, calls: list[ToolCall]) -> None:
        self._append(Message(role="assistant", content=text, tool_calls=calls), "assistant")

    def add_tool_results(self, results: list[ToolResult]) -> None:
        self._append(Message(role="tool", tool_results=results), "tool")

    def messages(self) -> list[Message]:
        return [copy.deepcopy(e.message) for e in self._entries]

    def last_role(self) -> str:
        return self._entries[-1].message.role if self._entries else ""

    def snapshot(self) -> HistorySnapshot:
        return HistorySnapshot(
            self.session_id, self.revision, self.content_epoch, tuple(copy.deepcopy(self._entries))
        )

    def _check_revision(self, expected: int) -> None:
        if self.revision != expected:
            raise ValueError("会话已变化，放弃本次上下文替换")

    def replace_tool_results(self, expected_revision: int, patches: list[ToolResultPatch]) -> None:
        self._check_revision(expected_revision)
        if not patches:
            return
        entries = copy.deepcopy(self._entries)
        positions = {e.id: i for i, e in enumerate(entries)}
        touched: set[tuple[str, int]] = set()
        for patch in patches:
            key = (patch.entry_id, patch.result_index)
            if key in touched or patch.entry_id not in positions:
                raise ValueError("工具结果替换目标无效")
            touched.add(key)
            i = positions[patch.entry_id]
            entry = entries[i]
            if entry.message.role != "tool" or not 0 <= patch.result_index < len(
                entry.message.tool_results
            ):
                raise ValueError("工具结果索引无效")
            old = entry.message.tool_results[patch.result_index]
            if (old.tool_call_id, old.is_error) != (
                patch.result.tool_call_id,
                patch.result.is_error,
            ):
                raise ValueError("压缩不能改变工具关联或错误状态")
            entry.message.tool_results[patch.result_index] = copy.deepcopy(patch.result)
            entries[i] = replace(entry, content_hash=message_hash(entry.message))
        legal_boundaries(tuple(entries))
        self._entries = entries
        self.revision += 1
        self.content_epoch += 1

    def replace_prefix(
        self, expected_revision: int, count: int, summary: str, quotes: tuple[UserQuote, ...] = ()
    ) -> None:
        self._check_revision(expected_revision)
        if not 0 < count < len(self._entries) or not summary.strip():
            raise ValueError("摘要前缀或内容无效")
        if count not in legal_boundaries(tuple(self._entries)):
            raise ValueError("摘要切点拆散了工具调用")
        if self._entries[count].message.role == "tool":
            raise ValueError("保留历史不能以工具结果开头")
        entry = self._entry(Message(role="user", content=summary), "summary", quotes)
        candidate = [entry, *self._entries[count:]]
        legal_boundaries(tuple(candidate))
        self._entries = candidate
        self.revision += 1
        self.content_epoch += 1
