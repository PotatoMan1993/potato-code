"""Token 估算：锚定上一次 provider 返回的真实 usage，之后按字符增量近似。

不引入精确 tokenizer：锚点由真实用量给出，两次真实请求之间只做字符/token 粗估，
误差会被下一次锚点覆盖。
"""

from __future__ import annotations

import json
import math
from typing import Any

from ..llm import Message, Usage
from .const import ESTIMATE_CHARS_PER_TOKEN


def _text_bytes(value: Any) -> int:
    """把一段内容折算成 UTF-8 字节数（非字符串先序列化，None 记 0）。"""
    if value is None:
        return 0
    if isinstance(value, str):
        s = value
    else:
        s = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return len(s.encode("utf-8"))


def message_chars(msgs: list[Message]) -> int:
    """计算一段消息列表的字符总量（正文 + 工具调用参数 + 工具结果）。"""
    total = 0
    for m in msgs:
        total += _text_bytes(m.content)
        for c in m.tool_calls:
            total += _text_bytes(c.input)
        for r in m.tool_results:
            total += _text_bytes(r.content)
    return total


def estimate_tokens(anchor: int, all_msgs: list[Message], anchor_msg_len: int) -> int:
    """估算"现在发请求会消耗多少 token"。

    只对锚点之后新增的消息（`all_msgs[anchor_msg_len:]`）做字符增量估算，
    避免把已含在 anchor 里的历史重复计入；锚点为 0 时退化为纯字符估算。
    """
    start = max(0, anchor_msg_len)
    tail = all_msgs[start:] if start < len(all_msgs) else []
    return anchor + math.ceil(message_chars(tail) / ESTIMATE_CHARS_PER_TOKEN)


def usage_anchor(u: Usage) -> int:
    """把一次真实请求的用量合并成单一锚点值（输入 + 输出 + 缓存读 + 缓存写）。"""
    return u.input_tokens + u.output_tokens + u.cache_read + u.cache_write
