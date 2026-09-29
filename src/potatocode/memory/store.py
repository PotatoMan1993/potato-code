"""单级笔记仓库：笔记文件的增删改 + 索引文件（MEMORY.md）维护。

一条笔记 = 一个带 YAML frontmatter 的 Markdown 文件（F28），文件名由 LLM 生成的
`<type>_<slug>.md` 决定（F31）。索引每行一条摘要，行尾带上文件名——模型据此知道
要读哪个文件才能拿到笔记全文（F33）。
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path

import yaml

from .const import DESC_LIMIT, INDEX_FILENAME
from .types import Note, NoteType, UpdateAction

logger = logging.getLogger(__name__)

# 索引行的结尾标记：`（<文件名>）`，update/delete 靠它定位对应行
_FILENAME_RE = re.compile(r"（([^）]+\.md)）\s*$")
# 非法 slug 字符（LLM 生成的文件名一律再洗一遍，防目录穿越）
_SLUG_SAFE_RE = re.compile(r"[^a-z0-9_]+")


def _slugify(text: str) -> str:
    """把标题洗成全小写下划线分隔的 slug（F31）。"""
    slug = _SLUG_SAFE_RE.sub("_", text.strip().lower().replace("-", "_").replace(" ", "_"))
    return slug.strip("_") or "note"


def _safe_filename(name: str) -> str:
    """只保留文件名部分，挡掉 `../` 之类的目录穿越。"""
    return os.path.basename(name.strip())


def _first_line(text: str, limit: int = DESC_LIMIT) -> str:
    """取正文首行作为索引里的一句话描述。"""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


def render_index_line(type_: str, title: str, content: str, filename: str) -> str:
    """渲染一行索引：`- [<type>] <title> — <描述>（<filename>）`（F30）。"""
    desc = _first_line(content)
    head = f"- [{type_}] {title}".rstrip()
    if desc:
        head = f"{head} — {desc}"
    return f"{head}（{filename}）"


def render_note_file(
    type_: str,
    title: str,
    created: datetime,
    updated: datetime,
    content: str,
) -> str:
    """渲染笔记文件全文：YAML frontmatter + 正文（F28）。"""
    meta = yaml.safe_dump(
        {
            "type": type_,
            "title": title,
            "created": created.isoformat(),
            "updated": updated.isoformat(),
        },
        allow_unicode=True,
        sort_keys=False,
    ).rstrip()
    return f"---\n{meta}\n---\n{content.strip()}\n"


def parse_note_file(text: str) -> tuple[dict, str]:
    """拆出 frontmatter 与正文；没有 frontmatter 时元数据为空字典。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as e:  # frontmatter 坏了：当没有元数据，正文照读
        logger.warning("笔记 frontmatter 解析失败: %s", e)
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, "\n".join(lines[end + 1 :]).strip()


class Store:
    """管理单级（项目级或用户级）的笔记文件与索引。"""

    def __init__(self, dir_: str) -> None:
        self._dir = dir_
        self._lock = threading.Lock()

    @property
    def dir(self) -> str:
        """该级 memory 目录。"""
        return self._dir

    def ensure_dir(self) -> None:
        """确保目录存在（首次写笔记时才建，不打扰干净的项目）。"""
        os.makedirs(self._dir, exist_ok=True)

    def load_index(self) -> str:
        """读取索引文件内容；不存在返回空字符串。"""
        try:
            return (Path(self._dir) / INDEX_FILENAME).read_text(encoding="utf-8")
        except OSError:
            return ""

    def notes(self) -> list[Note]:
        """列出该级已有的笔记（frontmatter 坏掉的文件跳过）。"""
        out: list[Note] = []
        try:
            files = sorted(Path(self._dir).glob("*.md"))
        except OSError:
            return out
        for path in files:
            if path.name == INDEX_FILENAME:
                continue
            try:
                meta, content = parse_note_file(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            try:
                type_ = NoteType(str(meta.get("type") or ""))
            except ValueError:  # 无类型标记的文件不是笔记
                continue
            out.append(
                Note(
                    type=type_,
                    title=str(meta.get("title") or ""),
                    slug=path.stem,
                    content=content,
                    filename=path.name,
                    created=datetime.now().astimezone(),
                    updated=datetime.now().astimezone(),
                )
            )
        return out

    def apply(self, actions: list[UpdateAction]) -> None:
        """按顺序执行一组更新操作；单条失败只记日志，不拖垮其余操作（N5）。"""
        if not actions:
            return
        with self._lock:
            self.ensure_dir()
            for action in actions:
                try:
                    self._apply_one(action)
                except (OSError, ValueError) as e:
                    logger.warning("记忆操作失败: %s: %s", action.action, e)

    # ────────────────────────── 内部 ──────────────────────────

    def _apply_one(self, action: UpdateAction) -> None:
        """分派 create / update / delete。"""
        if action.action == "create":
            self._create(action)
        elif action.action == "update":
            self._update(action)
        else:
            self._delete(action)

    def _create(self, action: UpdateAction) -> None:
        """新建笔记文件并在索引里追加一行。"""
        try:
            type_ = NoteType(action.type)
        except ValueError:  # 类型缺失或非法：降级为项目知识
            type_ = NoteType.PROJECT_KNOWLEDGE
        slug = _slugify(action.slug or action.title)
        filename = _safe_filename(action.filename) or f"{type_.value}_{slug}.md"
        now = datetime.now().astimezone()
        content = action.content.strip()
        self._write_note(filename, type_.value, action.title, now, now, content)
        line = render_index_line(type_.value, action.title, content, filename)
        self._upsert_index(filename, line)

    def _update(self, action: UpdateAction) -> None:
        """重写笔记内容（保留 created，刷新 updated）并更新索引对应行。"""
        filename = _safe_filename(action.filename)
        if not filename:
            raise ValueError("update 操作缺少 filename")
        path = Path(self._dir) / filename
        meta: dict = {}
        try:
            meta, old_content = parse_note_file(path.read_text(encoding="utf-8"))
        except OSError:  # 文件不在：按新文件处理，不让整批操作失败
            old_content = ""
        created = _parse_dt(meta.get("created")) or datetime.now().astimezone()
        title = action.title or str(meta.get("title") or "")
        type_ = action.type or str(meta.get("type") or _type_from_filename(filename))
        content = action.content.strip() or old_content
        self._write_note(filename, type_, title, created, datetime.now().astimezone(), content)
        self._upsert_index(filename, render_index_line(type_, title, content, filename))

    def _delete(self, action: UpdateAction) -> None:
        """删除笔记文件并移除索引对应行。"""
        filename = _safe_filename(action.filename)
        if not filename:
            raise ValueError("delete 操作缺少 filename")
        try:
            os.remove(Path(self._dir) / filename)
        except FileNotFoundError:
            pass  # 已经不在了：删除是幂等的
        self._remove_index(filename)

    def _write_note(
        self,
        filename: str,
        type_: str,
        title: str,
        created: datetime,
        updated: datetime,
        content: str,
    ) -> None:
        """按统一模板写文件（覆盖写）。"""
        self.ensure_dir()
        text = render_note_file(type_, title, created, updated, content)
        (Path(self._dir) / filename).write_text(text, encoding="utf-8")

    def _index_lines(self) -> list[str]:
        """索引文件的非空行。"""
        text = self.load_index()
        return [ln for ln in text.splitlines() if ln.strip()]

    def _upsert_index(self, filename: str, line: str) -> None:
        """索引里已有该文件的条目就替换，否则追加。"""
        lines = self._index_lines()
        for i, existing in enumerate(lines):
            match = _FILENAME_RE.search(existing)
            if match and match.group(1) == filename:
                lines[i] = line
                break
        else:
            lines.append(line)
        self._write_index(lines)

    def _remove_index(self, filename: str) -> None:
        """移除索引里指向该文件的条目。"""
        lines = self._index_lines()
        kept = [
            ln for ln in lines if not ((m := _FILENAME_RE.search(ln)) and m.group(1) == filename)
        ]
        self._write_index(kept)

    def _write_index(self, lines: list[str]) -> None:
        """把索引写回文件（行尾统一一个换行）。"""
        self.ensure_dir()
        text = "\n".join(lines)
        if text:
            text += "\n"
        (Path(self._dir) / INDEX_FILENAME).write_text(text, encoding="utf-8")


def _type_from_filename(filename: str) -> str:
    """从 `<type>_<slug>.md` 猜类型；猜不出按项目知识。"""
    stem = filename[:-3] if filename.endswith(".md") else filename
    for note_type in NoteType:
        if stem.startswith(note_type.value):
            return note_type.value
    return NoteType.PROJECT_KNOWLEDGE.value


def _parse_dt(value: object) -> datetime | None:
    """解析 frontmatter 里的时间字符串（ISO 8601）。"""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def project_memory_dir(workspace: str) -> str:
    """项目级 memory 目录：`<workspace>/.potatocode/memory`。"""
    return str(Path(workspace) / ".potatocode" / "memory")


def user_memory_dir(user_home: str = "") -> str:
    """用户级 memory 目录：`~/.potatocode/memory`。"""
    home = user_home or os.path.expanduser("~")
    return str(Path(home) / ".potatocode" / "memory")


__all__ = [
    "Store",
    "parse_note_file",
    "project_memory_dir",
    "render_index_line",
    "render_note_file",
    "user_memory_dir",
]
