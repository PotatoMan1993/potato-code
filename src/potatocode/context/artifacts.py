"""工具结果存盘：先完成可回读的文件，再返回待提交的消息补丁。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from ..conversation import HistorySnapshot, ToolResultPatch
from ..llm import ToolCall, ToolResult
from ..permission import Decision, Mode
from ..permission.engine import Engine

SINGLE_LIMIT = 50_000
PREVIEW_LIMIT = 1_500
AGGREGATE_LIMIT = 200_000


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def cancellable_io[T](function: Callable[[], T], cancel: asyncio.Event) -> T:
    """取消只放弃线程结果；线程不得修改真实会话或异步侧索引。"""
    if cancel.is_set():
        raise asyncio.CancelledError
    worker = asyncio.create_task(asyncio.to_thread(function))
    cancellation = asyncio.create_task(cancel.wait())
    try:
        await asyncio.wait((worker, cancellation), return_when=asyncio.FIRST_COMPLETED)
        if cancel.is_set():
            raise asyncio.CancelledError
        return await worker
    finally:
        cancellation.cancel()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, cancellation, return_exceptions=True)


@dataclass(frozen=True)
class ArtifactRecord:
    """同一结果的原文与预览双指纹，避免二次包装预览。"""

    original_hash: str
    preview_hash: str
    path: Path
    original_length: int
    preview: str
    visible_text: str


class ArtifactStore:
    """会话专属结果仓库；prepare 不修改 Conversation。"""

    def __init__(self, engine: Engine, session_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            raise ValueError("无效的会话存盘标识")
        self.engine = engine
        self.root = Path(engine.root).resolve()
        self.directory = self.root / ".potatocode" / "context" / session_id / "tool-results"
        self._records: dict[tuple[str, int], ArtifactRecord] = {}

    def visible_text(self, entry_id: str, result_index: int, current_content: str) -> str | None:
        """仅当内容确为本仓库生成的预览时，返回其中实际保留的原文。"""
        record = self._records.get((entry_id, result_index))
        if record is not None and _digest(current_content) == record.preview_hash:
            return record.visible_text
        return None

    def _checked_path(self, path: Path, mode: Mode) -> Path:
        try:
            resolved = path.resolve()
        except RuntimeError as error:
            raise ValueError("工具结果存盘路径无法解析") from error
        if not resolved.is_relative_to(self.root):
            raise ValueError("工具结果存盘路径超出项目目录")
        call = ToolCall(
            id="context-artifact-read",
            name="read_file",
            input=json.dumps({"path": str(resolved)}, ensure_ascii=False),
        )
        decision, reason = self.engine.check(mode, call, True)
        if decision is not Decision.ALLOW:
            raise ValueError(f"工具结果存盘位置不可读取：{reason}")
        return resolved

    def _verify(self, record: ArtifactRecord, mode: Mode) -> None:
        path = self._checked_path(record.path, mode)
        if hashlib.sha256(path.read_bytes()).hexdigest() != record.original_hash:
            raise ValueError("工具结果存盘文件内容已变化，无法确认完整原文")

    def _write(self, path: Path, content: str, mode: Mode) -> None:
        self._checked_path(path, mode)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._checked_path(path, mode)
        data = content.encode("utf-8")
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError("工具结果存盘位置已存在不同内容，拒绝覆盖")
            return
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".pending-", delete=False
            ) as f:
                temporary = Path(f.name)
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            self._checked_path(path, mode)
            if path.exists():
                if path.read_bytes() != data:
                    raise ValueError("工具结果存盘位置已存在不同内容，拒绝覆盖")
            else:
                os.replace(temporary, path)
                temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _new_record(self, entry_id: str, result_index: int, content: str) -> ArtifactRecord:
        digest = _digest(content)
        # ID 来自应用；散列命名还使测试或旧会话中的特殊字符不可能变成路径。
        entry_name = hashlib.sha256(entry_id.encode("utf-8")).hexdigest()[:16]
        path = self.directory / f"{entry_name}-{result_index}-{digest}.txt"
        visible = content[:PREVIEW_LIMIT]
        preview = (
            f"{visible}\n\n"
            f"[工具结果已存盘；原文 {len(content)} 字符，以上为前 {len(visible)} 字符预览。]\n"
            f"完整内容：{path}"
        )
        return ArtifactRecord(digest, _digest(preview), path, len(content), preview, visible)

    async def _store_result(
        self, entry_id: str, index: int, original: ToolResult, mode: Mode, cancel: asyncio.Event
    ) -> ToolResult | None:
        key = (entry_id, index)
        record = self._records.get(key)
        if record is None or record.original_hash != _digest(original.content):
            record = self._new_record(entry_id, index, original.content)
        if len(record.preview) >= len(original.content):
            return None
        await cancellable_io(partial(self._write, record.path, original.content, mode), cancel)
        self._checked_path(record.path, mode)
        self._records[key] = record
        return replace(original, content=record.preview)

    async def prepare(
        self, snapshot: HistorySnapshot, mode: Mode, cancel: asyncio.Event
    ) -> list[ToolResultPatch]:
        """处理单条及聚合阈值，失败或取消时调用方不会收到可提交的半成品。"""
        patches: list[ToolResultPatch] = []
        for entry in snapshot.entries:
            if cancel.is_set():
                raise asyncio.CancelledError
            results = list(entry.message.tool_results)
            if not results:
                continue
            stored: set[int] = set()
            for index, result in enumerate(results):
                record = self._records.get((entry.id, index))
                if record is not None and _digest(result.content) == record.preview_hash:
                    await cancellable_io(partial(self._verify, record, mode), cancel)
                    stored.add(index)

            for index, result in enumerate(results):
                if index not in stored and len(result.content) > SINGLE_LIMIT:
                    replacement = await self._store_result(entry.id, index, result, mode, cancel)
                    if replacement is None:
                        raise ValueError("工具结果预览无法满足单条限制")
                    results[index] = replacement
                    stored.add(index)

            total = sum(len(result.content) for result in results)
            if total > AGGREGATE_LIMIT:
                candidates = sorted(
                    (i for i in range(len(results)) if i not in stored),
                    key=lambda i: (-len(results[i].content), i),
                )
                for index in candidates:
                    previous_length = len(results[index].content)
                    replacement = await self._store_result(
                        entry.id, index, results[index], mode, cancel
                    )
                    if replacement is not None:
                        results[index] = replacement
                        stored.add(index)
                        total -= previous_length - len(results[index].content)
                    if total <= AGGREGATE_LIMIT:
                        break
                if total > AGGREGATE_LIMIT:
                    raise ValueError("工具结果预览合计仍超过 200,000 字符，无法进一步缩减")
            patches.extend(
                ToolResultPatch(entry.id, index, result)
                for index, result in enumerate(results)
                if result.content != entry.message.tool_results[index].content
            )
        if cancel.is_set():
            raise asyncio.CancelledError
        return patches
