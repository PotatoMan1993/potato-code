"""协议 usage 累计快照：保留原数值路径，归一完整输入，不累加流事件。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from . import Usage


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def numeric_counts(value: Any, prefix: str = "") -> dict[str, int | None]:
    """只保留数值；嵌套明细用原字段组成的点路径，不保存 SDK 对象。"""
    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_unset=True)
    if not isinstance(value, Mapping):
        return {}
    counts: dict[str, int | None] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, Mapping):
            counts.update(numeric_counts(item, path))
        elif type(item) is int or item is None:
            counts[path] = item
    return counts


def merge_counts(target: dict[str, int | None], value: Any) -> None:
    """delta 中缺失或 null 的字段不覆盖此前已收到的累计值。"""
    for key, item in numeric_counts(value).items():
        if item is not None or key not in target:
            target[key] = item


def usage_from_counts(counts: dict[str, int | None], protocol: str) -> Usage:
    if protocol == "anthropic":
        raw_input = counts.get("input_tokens")
        cache_write = counts.get("cache_creation_input_tokens") or 0
        cache_read = counts.get("cache_read_input_tokens") or 0
        total = None if raw_input is None else raw_input + cache_write + cache_read
        output = counts.get("output_tokens")
    else:
        total = counts.get("prompt_tokens")
        output = counts.get("completion_tokens")
        cache_write = 0
        cache_read = counts.get("prompt_tokens_details.cached_tokens") or 0
    return Usage(total, output, cache_write, cache_read, dict(counts))
