"""文件工具成功操作的结构化观察，不参与模型消息序列化。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal


def text_version(text: str) -> str:
    """标识实际读写文本的版本，不在日志中暴露正文。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FileObservation:
    """一次成功读写的来源；读取正文仅包含实际交付的片段。"""

    path: str
    operation: Literal["read", "write", "edit"]
    delivered_text: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    complete: bool = False
    truncated: bool = False
    source_version: str | None = None
