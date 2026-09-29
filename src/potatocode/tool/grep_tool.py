"""grep 工具：在文件内容中按正则检索，返回命中位置（文件/行/内容）（AC6）。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path

from . import GREP_MAX_RESULTS, Result, _parse_args
from .exclude import is_excluded

# 单行超过该长度视为"超长行"，不再完整搜索，避免读爆内存/假无命中（plan 决策）
_MAX_LINE_CHARS = 1 << 20
_CHUNK = 1 << 16


def _iter_lines(file, max_line_chars: int) -> Iterator[tuple[int, str, bool]]:
    """按块读文件并逐行产出 (行号, 行文本, 是否超长未完整)。

    超长行只保留前缀并标记 too_long，避免整行读入内存或卡死搜索。
    每块只做一次切分，未完成的行尾留到下一块拼接（ch09 F4：原先逐行切分要反复重扫剩余缓冲）。

    超长行的剩余部分**整段丢弃**（不产出、不涨行号），直到遇到换行才恢复，
    保证行号与物理行一一对应；否则一个超长行会被拆成多条记录、后续行号全部错位。
    """
    lineno = 0
    buf = ""
    dropping = False  # 正在丢弃某个超长行的剩余部分，期间不产出、不涨行号
    while True:
        chunk = file.read(_CHUNK)
        if not chunk:
            if buf and not dropping:
                lineno += 1
                yield lineno, buf, len(buf) > max_line_chars
            return
        buf += chunk

        if dropping:
            # 丢弃超长行的剩余部分，直到换行；换行之后的才是新一行
            nl = buf.find("\n")
            if nl < 0:
                buf = ""
                continue
            buf = buf[nl + 1 :]
            dropping = False

        if "\n" not in buf and len(buf) > max_line_chars:
            # 单行超长：只产出截断前缀，其余丢弃（dropping 保证同一物理行只产出一次）
            lineno += 1
            yield lineno, buf[:max_line_chars] + "…", True
            buf = ""
            dropping = True
            continue

        parts = buf.split("\n")  # 整块一次切完
        buf = parts.pop()  # 尾段是未完成的行，留给下一块
        for line in parts:
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

    def read_only(self) -> bool:
        return True

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
                if is_excluded(p, root):
                    continue  # 不读噪声目录，省下扫描开销（F1/F2）
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
