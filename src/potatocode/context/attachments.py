"""最近文件与已加载 Skill：只补充可验证、当前仍允许读取的上下文。"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from ..conversation import HistorySnapshot
from ..llm import ToolCall
from ..permission import Decision, Mode
from ..permission.engine import Engine
from ..tool.file_observation import FileObservation
from .artifacts import ArtifactStore, cancellable_io
from .types import AttachmentBundle, FileAttachment, SkillAttachment

FILE_LIMIT = 5
FILE_CHAR_LIMIT = 10_000
SKILL_CHAR_LIMIT = 50_000


@dataclass(frozen=True)
class _FileVisit:
    path: str
    sequence: int


@dataclass(frozen=True)
class _SkillVisit:
    attachment: SkillAttachment
    sequence: int


class ContextAttachments:
    """跨用户轮保存成功操作次序；压缩失败不抹去已经发生的访问。"""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.root = Path(engine.root).resolve()
        self._sequence = 0
        self._files: dict[str, _FileVisit] = {}
        self._skills: dict[str, _SkillVisit] = {}
        self._pending: dict[str, tuple[FileObservation, int]] = {}

    def _path(self, path: str) -> tuple[str, str]:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = str(candidate.resolve())
        return os.path.normcase(resolved), resolved

    def _sensitive(self, path: str) -> bool:
        p = Path(path)
        name = p.name.casefold()
        if (
            name in {".env", ".potatocode.yaml", "api_keys.txt", "auth.json"}
            or name.startswith((".env.", "settings.local."))
            or p.suffix.casefold() in {".pem", ".key"}
        ):
            return True
        return p.suffix.casefold() in {".yaml", ".yml"} and ".potatocode" in {
            part.casefold() for part in p.parts
        }

    def observe_file(self, call_id: str, observation: FileObservation) -> None:
        """调用方只传成功结果；Skill 先待确认，避免重注入未交付的全文。"""
        key, path = self._path(observation.path)
        self._sequence += 1
        self._files[key] = _FileVisit(path, self._sequence)
        if (
            observation.operation == "read"
            and Path(path).name.casefold() == "skill.md"
            and observation.delivered_text is not None
        ):
            self._pending[call_id] = (replace(observation, path=path), self._sequence)

    def _store_skill(self, attachment: SkillAttachment, sequence: int) -> None:
        key, path = self._path(attachment.path)
        old = self._skills.get(key)
        if old is not None and old.sequence > sequence:
            return
        if (
            old is not None
            and old.attachment.complete
            and attachment.source_version is not None
            and old.attachment.source_version == attachment.source_version
            and not attachment.complete
        ):
            # 同版全文已被加载时，较短重读只更新使用次序，不遗忘已经交付的定义。
            self._skills[key] = _SkillVisit(old.attachment, sequence)
            return
        self._skills[key] = _SkillVisit(replace(attachment, path=path), sequence)

    def record_loaded_skill(
        self, path: str, text: str, source_version: str | None, complete: bool
    ) -> None:
        """上层明确加载入口；不由提及名称或扫描磁盘隐式触发。"""
        self._sequence += 1
        self._store_skill(
            SkillAttachment(
                path=path,
                text=text,
                source_version=source_version,
                complete=complete,
                truncated=not complete,
            ),
            self._sequence,
        )

    def confirm_delivered(self, snapshot: HistorySnapshot, artifacts: ArtifactStore) -> None:
        """轻量替换后确认实际可见的 Skill 片段，不把存盘路径说明当定义。"""
        confirmed: list[tuple[int, SkillAttachment]] = []
        # 某些兼容服务跨请求复用调用 ID，待确认观察应匹配最近的结果。
        for entry in reversed(snapshot.entries):
            for index, result in enumerate(entry.message.tool_results):
                pending = self._pending.pop(result.tool_call_id, None)
                if pending is None or result.is_error:
                    continue
                observation, sequence = pending
                observed = observation.delivered_text or ""
                visible = artifacts.visible_text(entry.id, index, result.content)
                delivered = result.content if visible is None else visible
                actual = os.path.commonprefix((observed, delivered))
                reduced = len(actual) < len(observed)
                line_count = len(actual.splitlines())
                end_line = (
                    observation.start_line + line_count - 1
                    if observation.start_line is not None and line_count
                    else None
                )
                confirmed.append(
                    (
                        sequence,
                        SkillAttachment(
                            path=observation.path,
                            text=actual,
                            source_version=observation.source_version,
                            start_line=observation.start_line if line_count else None,
                            end_line=end_line,
                            complete=observation.complete and not reduced,
                            truncated=observation.truncated or reduced,
                        ),
                    ),
                )
        # 并发读取的结果展示按调用序，加载次序仍按实际成功完成的序号。
        for sequence, attachment in sorted(confirmed, key=lambda item: item[0]):
            self._store_skill(attachment, sequence)

    def _read_decision(self, path: str, mode: Mode) -> tuple[bool, str]:
        try:
            resolved = Path(path).resolve()
        except (OSError, RuntimeError):
            return False, "文件路径无法解析，未重新读取"
        if not resolved.is_relative_to(self.root):
            return False, "路径在项目目录之外，未重新读取"
        call = ToolCall(
            id="context-attachment-read",
            name="read_file",
            input=json.dumps({"path": str(resolved)}, ensure_ascii=False),
        )
        decision, reason = self.engine.check(mode, call, True)
        return decision is Decision.ALLOW, reason or "当前权限不允许重新读取"

    def _read_file(self, path: str, mode: Mode) -> FileAttachment:
        permitted, reason = self._read_decision(path, mode)
        if not permitted:
            return FileAttachment(path=path, omitted_reason=reason)
        try:
            with Path(path).open(encoding="utf-8", errors="strict", newline="") as stream:
                text = stream.read(FILE_CHAR_LIMIT + 1)
            # 读取结束再次检查，避免期间路径变化导致将不再允许的正文加入附件。
            permitted, reason = self._read_decision(path, mode)
            if not permitted:
                return FileAttachment(path=path, omitted_reason=reason)
            if "\x00" in text:
                return FileAttachment(path=path, omitted_reason="文件不是可注入的文本内容")
            return FileAttachment(
                path=path, text=text[:FILE_CHAR_LIMIT], truncated=len(text) > FILE_CHAR_LIMIT
            )
        except (OSError, UnicodeError) as error:
            return FileAttachment(
                path=path, omitted_reason=f"文件无法重新读取：{type(error).__name__}"
            )

    async def collect(self, mode: Mode, cancel: asyncio.Event) -> AttachmentBundle:
        """只在已授权路径补读，无审批、无 recency 副作用，所有上限按字符计算。"""
        if cancel.is_set():
            raise asyncio.CancelledError
        skills: list[SkillAttachment] = []
        skill_paths: set[str] = set()
        remaining = SKILL_CHAR_LIMIT
        for key, skill_visit in sorted(self._skills.items(), key=lambda item: -item[1].sequence):
            if cancel.is_set():
                raise asyncio.CancelledError
            attachment = skill_visit.attachment
            if self._sensitive(attachment.path):
                continue
            permitted, reason = self._read_decision(attachment.path, mode)
            if not permitted:
                skills.append(replace(attachment, text="", omitted_reason=reason))
                skill_paths.add(key)
                continue
            if not remaining:
                skills.append(replace(attachment, text="", omitted_reason="Skill 正文额度已用完"))
            else:
                text = attachment.text[:remaining]
                reduced = len(text) < len(attachment.text)
                skills.append(
                    replace(
                        attachment,
                        text=text,
                        truncated=attachment.truncated or reduced,
                        complete=attachment.complete and not reduced,
                    )
                )
                remaining -= len(text)
            skill_paths.add(key)
        files: list[FileAttachment] = []
        for key, file_visit in sorted(self._files.items(), key=lambda item: -item[1].sequence):
            if key in skill_paths or self._sensitive(file_visit.path):
                continue
            if len(files) >= FILE_LIMIT:
                break
            file_attachment = await cancellable_io(
                partial(self._read_file, file_visit.path, mode), cancel
            )
            files.append(file_attachment)
        if cancel.is_set():
            raise asyncio.CancelledError
        return AttachmentBundle(files=files, skills=skills)


# 兼容管理器使用的简短名称，公开文档仍采用 ContextAttachments。
Attachments = ContextAttachments
