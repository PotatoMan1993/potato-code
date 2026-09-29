"""memory：自动笔记（长期记忆）的存储、索引与异步 LLM 更新。"""

from .const import LEVEL_PROJECT, LEVEL_USER, MAX_INDEX_BYTES
from .manager import Manager, parse_actions
from .store import Store, project_memory_dir, user_memory_dir
from .types import Note, NoteType, UpdateAction

__all__ = [
    "LEVEL_PROJECT",
    "LEVEL_USER",
    "MAX_INDEX_BYTES",
    "Manager",
    "Note",
    "NoteType",
    "Store",
    "UpdateAction",
    "parse_actions",
    "project_memory_dir",
    "user_memory_dir",
]
