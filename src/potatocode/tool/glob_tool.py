"""glob 工具：按 glob 模式列出匹配的文件路径（AC6）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from . import GLOB_MAX_RESULTS, Result, _parse_args


class GlobTool:
    """按模式找文件工具：给定 glob 模式（如 `**/*.py`）返回匹配文件列表。"""

    def name(self) -> str:
        return "glob"

    def description(self) -> str:
        return (
            "按 glob 模式查找文件路径（如 `**/*.py`），返回匹配的文件列表。"
            "结果最多 100 条并排序，便于定位项目结构。"
        )

    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "glob 匹配模式，如 **/*.py"},
                "path": {"type": "string", "description": "起始目录，默认当前工作目录"},
            },
            "required": ["pattern"],
        }

    async def execute(self, args: str) -> Result:
        data = _parse_args(args)
        pattern = data.get("pattern")
        if not pattern:
            return Result(content="缺少参数 pattern", is_error=True)
        if not isinstance(pattern, str):
            return Result(content="参数 pattern 应为字符串", is_error=True)

        root = Path(data.get("path") or ".")
        matches: list[str] = []
        try:
            for i, p in enumerate(root.glob(pattern)):
                if not p.is_file():
                    continue
                try:
                    rel = str(p.relative_to(root))
                except ValueError:
                    rel = str(p)
                matches.append(rel)
                if (i + 1) % 100 == 0:
                    await asyncio.sleep(0)  # 让出 event loop（N2）
                if len(matches) >= GLOB_MAX_RESULTS:
                    break
        except OSError as e:
            return Result(content=f"glob 失败: {e}", is_error=True)

        matches.sort()
        if not matches:
            return Result(content="无匹配")
        content = "\n".join(matches)
        if len(matches) >= GLOB_MAX_RESULTS:
            content += "\n[truncated]"
        return Result(content=content)
