"""上下文管理用到的会话级状态对象：会话上下文、替换账本、熔断器、文件追踪。

四类状态都可能被 TUI 命令路径、Agent 主循环、紧急压缩路径以及工具线程并发访问，
因此内部一律用 `threading.RLock` 保护临界区（读→决策→写在同一临界区内完成）。
"""

from __future__ import annotations

import logging
import random
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .const import MAX_CONSECUTIVE_AUTO_COMPACT_FAILURES

logger = logging.getLogger(__name__)

# 决策回调返回值语义：kept=保留原文、replaced=替换、skip=本轮放弃（下次重试）
DECISION_KEPT = "kept"
DECISION_REPLACED = "replaced"
DECISION_SKIP = "skip"


@dataclass
class SessionContext:
    """会话生命周期信息：会话 id 与工具结果落盘目录。"""

    session_id: str  # 形如 <unix_ts>-<short_random>
    spill_dir: str  # .potatocode/sessions/<session_id>/tool-results


def _new_session_id() -> str:
    """生成 `<unix_ts>-<short_random>` 形式的会话 id（进程内一次性）。"""
    try:
        rand = secrets.token_hex(4)
    except Exception as e:  # 熵源异常极少见：降级为随机数生成器，不阻断启动
        logger.warning("secrets 取随机串失败，降级为 random: %s", e)
        rand = random.Random(time.time()).randbytes(4).hex()
    return f"{int(time.time())}-{rand}"


def new_session_context(workspace: str) -> SessionContext:
    """新建会话上下文并确保落盘目录存在（已存在不算错误）。"""
    session_id = _new_session_id()
    spill_dir = str(Path(workspace) / ".potatocode" / "sessions" / session_id / "tool-results")
    Path(spill_dir).mkdir(parents=True, exist_ok=True)
    return SessionContext(session_id=session_id, spill_dir=spill_dir)


class ContentReplacementState:
    """工具结果替换决策账本：记录「已决策」与「已替换」两本簿子。

    同一个 tool_use_id 一旦进入 `_seen_ids` 就再也不重新评估，保证 prompt cache
    前缀逐字节稳定；对外只暴露 `decide_once` 这一个临界区入口。
    """

    def __init__(self) -> None:
        # RLock 保护：工具线程与主循环可能并发访问同一份账本
        self._lock = threading.RLock()
        self._seen_ids: set[str] = set()  # 已决策过的 id（无论保留还是替换）
        self._replacements: dict[str, str] = {}  # 仅「决定替换」那一支的预览字符串

    def seen(self, tool_use_id: str) -> bool:
        """该 id 是否已经决策过（供调用方分流候选列表）。"""
        with self._lock:
            return tool_use_id in self._seen_ids

    def decide_once(
        self,
        tool_use_id: str,
        original: str,
        decide: Callable[[], tuple[str, str]],
    ) -> str:
        """在临界区内完成「查账本 → 决策 → 写账本」，返回本轮的 content。

        已 Seen 的 id 直接复用存量结果（替换则返回 `_replacements[id]`，不重新构造）；
        未 Seen 的 id 调 decide 回调拿 `(decision, preview)`：
        kept 只写 seen、replaced 两者都写、skip 两者都不写（下一轮重试）。
        """
        with self._lock:
            if tool_use_id in self._seen_ids:
                return self._replacements.get(tool_use_id, original)
            decision, preview = decide()
            if decision == DECISION_REPLACED:
                self._seen_ids.add(tool_use_id)
                self._replacements[tool_use_id] = preview
                return preview
            if decision == DECISION_KEPT:
                self._seen_ids.add(tool_use_id)
            return original


class CompactCircuitBreaker:
    """自动摘要的连续失败计数：跳闸后只影响自动触发路径。"""

    def __init__(self) -> None:
        # RLock 保护：自动路径与手动路径可能并发读写计数
        self._lock = threading.RLock()
        self._consecutive_failures = 0

    def record_success(self) -> None:
        """一次自动摘要成功：连续失败计数清零。"""
        with self._lock:
            self._consecutive_failures = 0

    def record_failure(self) -> None:
        """一次自动摘要失败（含 PTL 重试用光）：连续失败计数 +1。"""
        with self._lock:
            self._consecutive_failures += 1

    def tripped(self) -> bool:
        """连续失败是否已达熔断阈值。"""
        with self._lock:
            return self._consecutive_failures >= MAX_CONSECUTIVE_AUTO_COMPACT_FAILURES


@dataclass
class FileReadRecord:
    """一次成功读取的文件快照：路径、纯净内容与读取时间。"""

    path: str  # 文件绝对路径
    content: str  # 不带行号前缀的原始内容
    timestamp: datetime  # 最后一次成功读取的时间


class RecoveryState:
    """最近读过文件的追踪状态：Agent 主循环写、摘要恢复段读。"""

    def __init__(self) -> None:
        # RLock 保护：工具线程写、主循环读快照
        self._lock = threading.RLock()
        self._files: dict[str, FileReadRecord] = {}  # 键为文件绝对路径

    def record_file(self, path: str, content: str) -> None:
        """记录（或覆盖）一个文件的纯净内容，时间戳取本次写入时刻。"""
        abs_path = str(Path(path).resolve())
        with self._lock:
            self._files[abs_path] = FileReadRecord(
                path=abs_path, content=content, timestamp=datetime.now()
            )

    def snapshot(self) -> list[FileReadRecord]:
        """返回按时间戳倒序排列的拷贝列表（同刻并列时后写入的在前）。"""
        with self._lock:
            records = list(self._files.values())[::-1]
        records.sort(key=lambda r: r.timestamp, reverse=True)  # 稳定排序保持并列时的新→旧
        return records
