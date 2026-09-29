"""会话存档写入器：把每条消息以一行 JSON 追加进 `conversation.jsonl`。

只做追加、不重写已有内容（F14）：进程崩溃最多丢最后一行不完整的写入。
每次 append 后 `flush` + `fsync`，保证外部进程（另开的 `tail -f`）能立刻读到，
崩溃前已写入的行也一定落在磁盘上。写盘失败只记日志不中断对话（N5）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..llm import Message, ToolCall, ToolResult

logger = logging.getLogger(__name__)

# 会话存档文件名（会话目录布局的一部分，list/load 共用）
JSONL_FILENAME = "conversation.jsonl"

# 压缩标记行的 type 值：恢复时从最后一个标记之后开始加载
COMPACT_MARKER = "compact"


@dataclass
class Entry:
    """JSONL 中一行的内存表示（消息行与压缩标记行共用）。"""

    role: str = ""  # "user"/"assistant"/"tool"；压缩标记行为空
    content: str = ""
    tool_calls: list[dict] | None = None  # 仅 assistant
    tool_results: list[dict] | None = None  # 仅 tool
    ts: int = 0  # 写入时刻的 Unix 秒
    model: str | None = None  # 仅首条消息：当前 provider 的模型名
    type: str | None = None  # "compact" 或 None

    def to_dict(self) -> dict:
        """转成落盘用的字典：空字段一律省掉，保持每行紧凑。"""
        data: dict = {"ts": self.ts}
        if self.role:
            data["role"] = self.role
        if self.content:
            data["content"] = self.content
        if self.tool_calls:
            data["tool_calls"] = self.tool_calls
        if self.tool_results:
            data["tool_results"] = self.tool_results
        if self.model:
            data["model"] = self.model
        if self.type:
            data["type"] = self.type
        return data


def tool_calls_to_dicts(calls: list[ToolCall]) -> list[dict] | None:
    """工具调用列表 → 可序列化字典列表（空列表返回 None）。"""
    if not calls:
        return None
    return [{"id": c.id, "name": c.name, "input": c.input} for c in calls]


def tool_results_to_dicts(results: list[ToolResult]) -> list[dict] | None:
    """工具结果列表 → 可序列化字典列表（空列表返回 None）。"""
    if not results:
        return None
    return [
        {"tool_call_id": r.tool_call_id, "content": r.content, "is_error": r.is_error}
        for r in results
    ]


def dict_to_message(data: dict) -> Message | None:
    """一行 JSON → 消息；role 非法（含压缩标记行）返回 None 由调用方跳过。"""
    role = data.get("role")
    if role not in ("user", "assistant", "tool"):
        return None
    calls = [
        ToolCall(
            id=str(c.get("id", "")),
            name=str(c.get("name", "")),
            input=str(c.get("input", "")),
        )
        for c in data.get("tool_calls") or []
        if isinstance(c, dict)
    ]
    results = [
        ToolResult(
            tool_call_id=str(r.get("tool_call_id", "")),
            content=str(r.get("content", "")),
            is_error=bool(r.get("is_error", False)),
        )
        for r in data.get("tool_results") or []
        if isinstance(r, dict)
    ]
    return Message(
        role=role,
        content=str(data.get("content") or ""),
        tool_calls=calls,
        tool_results=results,
    )


class Writer:
    """向 `conversation.jsonl` 追加写入的会话写入器。

    持有文件句柄与一把线程锁：主循环、TUI 命令路径与后台记忆更新可能从不同协程/
    线程触发写入，锁保证「一行 JSON + 一个换行」的原子性（N2）。
    """

    def __init__(self, session_dir: str, model: str = "") -> None:
        self._dir = session_dir
        self._path = os.path.join(session_dir, JSONL_FILENAME)
        self._lock = threading.Lock()
        self._model = model
        self._file = None
        os.makedirs(session_dir, exist_ok=True)
        self._open()

    @classmethod
    def open_existing(cls, session_dir: str, model: str = "") -> Writer:
        """追加模式打开已有会话目录（不创建目录，用于 /resume 恢复）。"""
        writer = cls.__new__(cls)
        writer._dir = session_dir
        writer._path = os.path.join(session_dir, JSONL_FILENAME)
        writer._lock = threading.Lock()
        writer._model = model
        writer._file = None
        writer._open()
        return writer

    @property
    def path(self) -> str:
        """存档文件的完整路径。"""
        return self._path

    def set_model(self, model: str) -> None:
        """记录当前模型名（首行落盘时带上，供会话列表展示）。"""
        self._model = model

    def append(self, msg: Message) -> None:
        """追加一条消息：首行额外带上 model 字段（F11）。"""
        with self._lock:
            self._write_message(msg, first=not self._first_done)
            self._first_done = True

    def append_all(self, msgs: list[Message]) -> None:
        """逐条追加一批消息（压缩后整体落盘用，不带 model 字段）。"""
        with self._lock:
            for msg in msgs:
                self._write_message(msg, first=False)
            self._first_done = True

    def write_compact_marker(self) -> None:
        """写入一行压缩标记，恢复时从最后一个标记之后开始加载（F12）。"""
        with self._lock:
            self._write_line(Entry(ts=int(time.time()), type=COMPACT_MARKER).to_dict())

    def on_append(self, msg: Message) -> None:
        """Conversation 的追加回调适配（ch11 F44）。"""
        self.append(msg)

    def on_replace(self, msgs: list[Message]) -> None:
        """Conversation 的整体替换回调适配：标记行 + 压缩后消息一次写盘。

        标记与消息在同一把锁内写完，中途崩溃不会留下「有标记没内容」的存档。
        """
        with self._lock:
            self._write_line(Entry(ts=int(time.time()), type=COMPACT_MARKER).to_dict())
            for msg in msgs:
                self._write_message(msg, first=False)
            self._first_done = True

    def close(self) -> None:
        """关闭文件句柄（可重复调用）。"""
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                except OSError as e:  # 关闭失败不影响进程退出
                    logger.warning("会话存档关闭失败: %s", e)
                self._file = None

    def __enter__(self) -> Writer:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ────────────────────────── 内部 ──────────────────────────

    def _open(self) -> None:
        """打开存档文件；已存在非空文件说明首行的 model 已写过。"""
        try:
            has_content = os.path.getsize(self._path) > 0
        except OSError:
            has_content = False
        self._first_done = has_content
        try:
            self._file = open(self._path, "ab")
        except OSError as e:  # 打开失败：本轮不落盘，但对话照常（N5）
            logger.warning("会话存档打开失败，本次运行不落盘: %s", e)
            self._file = None

    def _write_message(self, msg: Message, *, first: bool) -> None:
        """构造消息行并写盘（调用方须持锁）。"""
        entry = Entry(
            role=msg.role,
            content=msg.content,
            tool_calls=tool_calls_to_dicts(msg.tool_calls),
            tool_results=tool_results_to_dicts(msg.tool_results),
            ts=int(time.time()),
            model=self._model if first else None,
        )
        self._write_line(entry.to_dict())

    def _write_line(self, payload: dict) -> None:
        """序列化一行并 flush + fsync（调用方须持锁）。"""
        if self._file is None:
            return
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            self._file.write(line.encode("utf-8"))
            self._file.flush()
            os.fsync(self._file.fileno())
        except OSError as e:  # 磁盘满 / 句柄失效：只记日志，不打断对话
            logger.warning("会话存档写入失败: %s", e)


def jsonl_path(session_dir: str) -> str:
    """会话目录下的存档文件路径。"""
    return str(Path(session_dir) / JSONL_FILENAME)


__all__ = [
    "COMPACT_MARKER",
    "JSONL_FILENAME",
    "Entry",
    "Writer",
    "dict_to_message",
    "jsonl_path",
    "tool_calls_to_dicts",
    "tool_results_to_dicts",
]
