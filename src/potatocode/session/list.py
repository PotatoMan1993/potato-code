"""会话列表扫描：把 `.potatocode/sessions/` 下的目录整理成可展示的条目。

只认新格式会话 id（`YYYYMMDD-HHMMSS-xxxx`）——旧格式目录解析不出时间戳，
既不展示也不清理（N3），避免误删 ch08 遗留数据。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..compact.state import parse_session_time
from .writer import JSONL_FILENAME

logger = logging.getLogger(__name__)

# 标题截断上限（含省略号在内共 50 个字符）
TITLE_LIMIT = 50

_MINUTE = 60
_HOUR = 3600
_DAY = 86400
_MONTH = 30 * _DAY


@dataclass
class SessionInfo:
    """会话列表中的一项。"""

    id: str  # session ID（目录名）
    title: str  # 第一条 user 消息内容（截断）
    modified_at: datetime  # 最后修改时间
    model: str  # 模型标签
    size: int  # JSONL 文件大小（字节）
    dir: str  # 会话目录绝对路径

    def display_text(self) -> str:
        """列表项展示文案：标题 · 相对时间 · 模型 · 文件大小。"""
        bits = [self.title or "(无标题)", relative_time(self.modified_at)]
        if self.model:
            bits.append(self.model)
        bits.append(_human_size(self.size))
        return " · ".join(bits)


def _truncate(text: str, limit: int = TITLE_LIMIT) -> str:
    """截断到 limit 个字符，被截断时末位换成省略号。"""
    text = " ".join(text.split())  # 压平换行，避免一条消息撑爆列表项
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _human_size(size: int) -> str:
    """字节数转紧凑展示：1.3KB / 2.1MB。"""
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f}MB"
    if size >= 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size}B"


def relative_time(moment: datetime, now: datetime | None = None) -> str:
    """相对时间文案：just now / 3 minutes ago / 1 day ago。"""
    now = now or datetime.now()
    seconds = (now - moment).total_seconds()
    if seconds < 0:  # 时钟回拨：当作刚刚
        return "just now"
    if seconds < _MINUTE:
        return "just now"
    if seconds < _HOUR:
        return _plural(int(seconds // _MINUTE), "minute")
    if seconds < _DAY:
        return _plural(int(seconds // _HOUR), "hour")
    if seconds < _MONTH:
        return _plural(int(seconds // _DAY), "day")
    return _plural(int(seconds // _MONTH), "month")


def _plural(count: int, unit: str) -> str:
    """`3 hours ago` / `1 hour ago`。"""
    return f"{count} {unit}{'s' if count != 1 else ''} ago"


def _scan_journal(path: Path) -> tuple[str, str]:
    """扫描存档文件，取第一条 user 消息作标题、任一行的 model 作模型标签。

    只读到两个信息都拿到为止（N1：50 个会话的扫描不超过 500ms）。
    """
    title = ""
    model = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue  # 坏行不影响列表展示（N5）
                if not isinstance(data, dict):
                    continue
                if not model and data.get("model"):
                    model = str(data["model"])
                if not title and data.get("role") == "user" and data.get("content"):
                    title = _truncate(str(data["content"]))
                if title and model:
                    break
    except OSError as e:  # 文件恰好被删/无权限：按空信息展示
        logger.warning("读取会话存档失败: %s: %s", path, e)
    return title, model


def list_sessions(sessions_dir: str) -> list[SessionInfo]:
    """扫描会话目录，返回按最后修改时间倒序排列的有效会话。"""
    root = Path(sessions_dir)
    try:
        children = list(root.iterdir())
    except OSError:  # 目录不存在：没有历史会话，不是错误
        return []

    items: list[SessionInfo] = []
    for child in children:
        if not child.is_dir():
            continue
        if parse_session_time(child.name) is None:
            continue  # 旧格式 / 非法 id：不展示（N3）
        journal = child / JSONL_FILENAME
        try:
            stat = journal.stat()
        except OSError:
            continue  # 没有存档文件：不是可恢复会话
        if stat.st_size == 0:
            continue  # 空存档（刚建还没说过话的会话）：没什么可恢复的
        title, model = _scan_journal(journal)
        items.append(
            SessionInfo(
                id=child.name,
                title=title,
                modified_at=datetime.fromtimestamp(stat.st_mtime),
                model=model,
                size=stat.st_size,
                dir=str(child.resolve()),
            )
        )
    items.sort(key=lambda i: i.modified_at, reverse=True)
    return items


__all__ = ["TITLE_LIMIT", "SessionInfo", "list_sessions", "relative_time"]
