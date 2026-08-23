"""read_file 工具：读取文件文本（带行号，便于模型引用）。"""

from __future__ import annotations

from pathlib import Path

from . import READ_MAX_CHARS, READ_MAX_LINES, Result, _parse_args, _truncate


class ReadFileTool:
    """读文件工具：给定路径返回带行号的文本内容（N5：上限内截断）。"""

    def name(self) -> str:
        return "read_file"

    def description(self) -> str:
        return "读取指定文件并返回其内容（带行号）。文件不存在或不可读时返回错误。"

    def read_only(self) -> bool:
        return True

    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要读取的文件路径"},
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
        numbered = "\n".join(f"{n:6d}\t{line}" for n, line in enumerate(text.splitlines(), 1))
        return Result(content=_truncate(numbered, READ_MAX_LINES, READ_MAX_CHARS))
