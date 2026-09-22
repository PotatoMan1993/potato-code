"""edit_file 工具：对"原文片段"做唯一匹配替换（AC4）。"""

from __future__ import annotations

from pathlib import Path

from . import Result, _parse_args


class EditFileTool:
    """改文件工具：old_string 在文件中唯一匹配时替换为 new_string。

    匹配 0 次或多于 1 次时返回携带匹配数的可区分错误，让模型据此调整。
    """

    def name(self) -> str:
        return "edit_file"

    def description(self) -> str:
        return (
            "修改文件：把文件中的一段原文替换为新文本。old_string 必须唯一匹配，"
            "否则返回错误，请提供足够长的上下文使其唯一。"
            "编辑前请先用 read_file 读取目标文件，确认 old_string 唯一。"
        )

    def read_only(self) -> bool:
        return False

    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要修改的文件路径"},
                "old_string": {"type": "string", "description": "要替换的原文片段（需唯一匹配）"},
                "new_string": {"type": "string", "description": "替换后的新文本"},
            },
            "required": ["path", "old_string", "new_string"],
        }

    async def execute(self, args: str) -> Result:
        data = _parse_args(args)
        path = data.get("path")
        old = data.get("old_string")
        new = data.get("new_string")
        if not path:
            return Result(content="缺少参数 path", is_error=True)
        if not old:
            return Result(content="缺少参数 old_string", is_error=True)
        if new is None:
            return Result(content="缺少参数 new_string", is_error=True)
        if not isinstance(old, str) or not isinstance(new, str):
            return Result(content="参数 old_string/new_string 应为字符串", is_error=True)

        p = Path(path)
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return Result(content=f"文件不存在: {path}", is_error=True)
        except PermissionError:
            return Result(content=f"文件不可读（权限不足）: {path}", is_error=True)
        except OSError as e:
            return Result(content=f"读取文件失败: {path}: {e}", is_error=True)

        n = content.count(old)
        if n == 0:
            return Result(content="未找到匹配的内容", is_error=True)
        if n > 1:
            return Result(
                content=f"匹配到 {n} 处，old_string 不唯一，请提供更长上下文使其唯一",
                is_error=True,
            )

        new_content = content.replace(old, new, 1)
        try:
            p.write_text(new_content, encoding="utf-8")
        except OSError as e:
            return Result(content=f"写入文件失败: {path}: {e}", is_error=True)
        return Result(content=f"已修改 {path}（1 处替换）")
