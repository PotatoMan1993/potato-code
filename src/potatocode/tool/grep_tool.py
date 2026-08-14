"""grep 工具：在文件内容中按正则检索，返回命中位置（文件/行/内容）（AC6）。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path

from . import GREP_MAX_RESULTS, Result, _parse_args

# 单行超过该长度视为"超长行"，不再完整搜索，避免读爆内存/假无命中（plan 决策）
_MAX_LINE_CHARS = 1 << 20
_CHUNK = 1 << 16


def _iter_lines(file, max_line_chars: int) -> Iterator[tuple[int, str, bool]]:
    """按块读文件并逐行产出 (行号, 行文本, 是否超长未完整)。

    超长行只保留前缀并标记 too_long，避免整行读入内存或卡死搜索。
    """
    lineno = 0
    buf = ""
    while True:
        chunk = file.read(_CHUNK)
        if not chunk:
            if buf:
                lineno += 1
                yield lineno, buf, len(buf) > max_line_chars
            return
        buf += chunk
        if "\n" not in buf and len(buf) > max_line_chars:
            lineno += 1
            yield lineno, buf[:max_line_chars] + "…", True
            buf = ""
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            lineno += 1
            too_long = len(line) > max_line_chars
            yield lineno, line[:max_line_chars] + ("…" if too_long else ""), too_long


class GrepTool:
    """内容搜索工具：给定正则与可选路径/文件名过滤，返回命中位置列表。"""

    def name(self) -> str:
        return "grep"

    def description(self) -> str:
        return (
            "在文件内容中按正则表达式检索，返回命中的 file:line:content 列表。"
            "可用 glob 参数限定只搜某些文件（如 *.py）。结果最多 100 条。"
        )

    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python 正则表达式（re 语法）"},
                "path": {"type": "string", "description": "起始目录，默认当前工作目录"},
                "glob": {
                    "type": "string",
                    "description": "可选，仅搜索匹配此 glob 的文件（如 *.py）",
                },
            },
            "required": ["pattern"],
        }

    async def execute(self, args: str) -> Result:
        data = _parse_args(args)
        pattern = data.get("pattern")
        if not pattern:
            return Result(content="缺少参数 pattern", is_error=True)
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return Result(content=f"正则非法: {e}", is_error=True)

        root = Path(data.get("path") or ".")
        glob_pat = data.get("glob") or "*"
        hits: list[str] = []
        try:
            for p in root.rglob(glob_pat):
                if not p.is_file():
                    continue
                try:
                    with p.open("r", encoding="utf-8", errors="replace") as f:
                        for lineno, line, too_long in _iter_lines(f, _MAX_LINE_CHARS):
                            if rx.search(line) is not None:
                                hit = f"{p}:{lineno}:{line.rstrip()}"
                                if too_long:
                                    hit += "（该行过长，未完整搜索）"
                                hits.append(hit)
                                if len(hits) >= GREP_MAX_RESULTS:
                                    break
                except (OSError, UnicodeDecodeError):
                    continue  # 单个文件读失败跳过，不影响整体搜索
                await asyncio.sleep(0)  # 每文件让出 event loop（N2）
                if len(hits) >= GREP_MAX_RESULTS:
                    break
        except OSError as e:
            return Result(content=f"grep 失败: {e}", is_error=True)

        if not hits:
            return Result(content="无命中")
        content = "\n".join(hits)
        if len(hits) >= GREP_MAX_RESULTS:
            content += "\n[truncated]"
        return Result(content=content)
