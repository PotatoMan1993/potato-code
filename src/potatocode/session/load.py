"""会话恢复：把 `conversation.jsonl` 还原成消息列表。

容错三件套（F21）：跳过解析失败的行、从最后一个压缩标记之后开始加载、
截断结尾悬空的工具调用（assistant 带 tool_calls 却没有对应的 tool 回合，
直接回灌会被 API 拒）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..llm import ROLE_ASSISTANT, Message
from .writer import COMPACT_MARKER, JSONL_FILENAME, dict_to_message

logger = logging.getLogger(__name__)


@dataclass
class LoadedSession:
    """一次恢复的产物：消息列表 + 最后一条有效消息的时间戳。"""

    messages: list[Message] = field(default_factory=list)
    last_ts: int = 0


def _truncate_orphaned_tool_calls(msgs: list[Message]) -> list[Message]:
    """截断结尾悬空的工具调用回合（F21-3）。"""
    out = list(msgs)
    while out and out[-1].role == ROLE_ASSISTANT and out[-1].tool_calls:
        out.pop()
    return out


def load_session_full(session_dir: str) -> LoadedSession:
    """读取存档并还原消息列表（含最后一条消息的时间戳）。"""
    path = Path(session_dir) / JSONL_FILENAME
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:  # 文件缺失/无权限：返回空会话，不抛
        logger.warning("读取会话存档失败: %s: %s", path, e)
        return LoadedSession()

    entries: list[tuple[Message, int]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue  # 坏行（含崩溃时的半行）静默跳过（F21-2）
        if not isinstance(data, dict):
            continue
        if data.get("type") == COMPACT_MARKER:
            entries = []  # 只保留最后一个压缩标记之后的内容（F21-1）
            continue
        msg = dict_to_message(data)
        if msg is None:
            continue
        entries.append((msg, int(data.get("ts") or 0)))

    messages = _truncate_orphaned_tool_calls([m for m, _ in entries])
    last_ts = entries[len(messages) - 1][1] if messages else 0
    return LoadedSession(messages=messages, last_ts=last_ts)


def load_session(session_dir: str) -> list[Message]:
    """读取存档并还原消息列表（不带时间戳的便捷入口）。"""
    return load_session_full(session_dir).messages


__all__ = ["LoadedSession", "load_session", "load_session_full"]
