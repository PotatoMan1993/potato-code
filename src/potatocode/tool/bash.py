"""bash 工具：执行 shell 命令，返回 stdout/stderr/退出码（AC5/N1）。"""

from __future__ import annotations

import asyncio

from . import BASH_MAX_CHARS, BASH_MAX_LINES, Result, _parse_args, _truncate


class BashTool:
    """执行命令工具：在工作目录下执行 shell 命令，受外层超时约束。

    超时由 Registry.execute 的 asyncio.wait_for 控制；此处捕获 CancelledError
    确保子进程被终止（避免超时后残留孤儿进程）。
    """

    def name(self) -> str:
        return "bash"

    def description(self) -> str:
        return (
            "在终端执行一条 shell 命令（支持管道/重定向），返回标准输出、标准错误与退出码。"
            "可用于查看文件、运行脚本、执行构建等。"
            "读文件、找文件、搜内容请优先用 read_file/glob/grep，不要用 bash 拼凑。"
        )

    def read_only(self) -> bool:
        # 可执行任意副作用命令，保守归为有副作用、串行执行
        return False

    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 shell 命令"},
            },
            "required": ["command"],
        }

    async def execute(self, args: str) -> Result:
        data = _parse_args(args)
        cmd = data.get("command")
        if not cmd:
            return Result(content="缺少参数 command", is_error=True)
        if not isinstance(cmd, str):
            return Result(content="参数 command 应为字符串", is_error=True)

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await proc.communicate()
        except asyncio.CancelledError:
            # 被 Registry 层 wait_for 超时取消：终止子进程后重抛
            try:
                proc.kill()
            except OSError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (TimeoutError, OSError):
                pass
            raise

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        text = f"exit_code: {proc.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
        # 非零退出不设 is_error：按结果回灌，让模型自行判断（F9 只对超时/异常标错）
        return Result(content=_truncate(text, BASH_MAX_LINES, BASH_MAX_CHARS))
