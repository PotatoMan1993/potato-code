"""read_file 工具：读取文件文本（带行号，便于模型引用）。"""

from __future__ import annotations

from pathlib import Path

from . import READ_MAX_CHARS, READ_MAX_LINES, Result, _parse_args, _truncate


class ReadFileTool:
    """读文件工具：给定路径返回带行号的文本内容（N5：上限内截断）。"""

    def name(self) -> str:
        return "read_file"

    def description(self) -> str:
        return (
            "读取指定文件并返回其内容（带行号）。文件不存在或不可读时返回错误。"
            "对于大文件，建议先用grep_tool定位相关行，再用read_file读取指定范围。"
            "二进制文件不可读取，请改bash工具执行合适的命令。"
            "请优先使用本工具读取文件内容，不要用bash cat命令。"
        )

    def read_only(self) -> bool:
        return True

    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要读取的文件路径"},
                "offset": {
                    "type": "integer",
                    "default": 0,
                    "description": "开始读取的行偏移量（最小为0）",
                },
                "limit": {"type": "integer", "default": 2000, "description": "读取的最大行数"},
            },
            "required": ["path"],
        }

    async def execute(self, args: str) -> Result:
        data = _parse_args(args)
        path = data.get("path")
        if not path:
            return Result(content="缺少参数 path", is_error=True)

        p = Path(path)
        try:
            if p.is_dir():
                return Result(content=f"路径是目录，无法读取: {path}", is_error=True)
            text = p.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return Result(content=f"文件不存在: {path}", is_error=True)
        except PermissionError:
            return Result(content=f"文件不可读（权限不足）: {path}", is_error=True)
        except OSError as e:
            return Result(content=f"读取文件失败: {path}: {e}", is_error=True)

        # 带行号：f"{n:6d}\t{line}" 风格，便于模型引用具体行
        offset = data.get("offset", 0)
        limit = data.get("limit", 2000)
        lines = text.splitlines()
        selected = lines[offset : offset + limit]
        numbered = [f"{(i + offset):6d}\t{line}" for i, line in enumerate(selected, 1)]
        return Result(content=_truncate("\n".join(numbered), READ_MAX_LINES, READ_MAX_CHARS))
