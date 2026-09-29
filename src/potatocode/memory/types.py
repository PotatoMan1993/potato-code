"""记忆子包的数据类型：笔记分类、笔记与 LLM 返回的更新操作。

四类笔记（F27）——用户偏好与纠正反馈是跨项目通用的（用户级），
项目知识与参考资料与当前仓库强相关（项目级）。具体落在哪一级由 LLM 判断（F29）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .const import LEVEL_PROJECT, LEVEL_USER


class NoteType(StrEnum):
    """笔记分类。"""

    USER_PREFERENCE = "user_preference"  # 用户偏好
    CORRECTION_FEEDBACK = "correction_feedback"  # 纠正反馈
    PROJECT_KNOWLEDGE = "project_knowledge"  # 项目知识
    REFERENCE_MATERIAL = "reference_material"  # 参考资料


@dataclass
class Note:
    """一条笔记的内存表示。"""

    type: NoteType
    title: str
    slug: str
    content: str
    filename: str
    created: datetime
    updated: datetime


@dataclass
class UpdateAction:
    """LLM 返回的单条更新操作（F39）。"""

    action: str  # "create"/"update"/"delete"
    level: str  # "project"/"user"
    type: str = ""  # NoteType 取值（create 时必需）
    title: str = ""
    slug: str = ""
    content: str = ""
    filename: str = ""  # update/delete 时必需

    @classmethod
    def from_dict(cls, data: dict) -> UpdateAction | None:
        """从 LLM 返回的字典构造；action/level 非法返回 None。"""
        action = str(data.get("action") or "").strip().lower()
        level = str(data.get("level") or "").strip().lower()
        if action not in ("create", "update", "delete"):
            return None
        if level not in (LEVEL_PROJECT, LEVEL_USER):
            return None
        return cls(
            action=action,
            level=level,
            type=str(data.get("type") or "").strip(),
            title=str(data.get("title") or "").strip(),
            slug=str(data.get("slug") or "").strip(),
            content=str(data.get("content") or ""),
            filename=str(data.get("filename") or "").strip(),
        )


__all__ = ["Note", "NoteType", "UpdateAction"]
