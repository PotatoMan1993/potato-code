"""PotatoCode 入口：加载配置、启动 TUI。

banner 由 TUI 在 on_mount 时写入对话区（见 tui/app.py），此处不重复打印。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from . import __version__, config, mcp, permission
from .config import ConfigError
from .tool import new_default_registry
from .tui import PotatoCodeApp

# 运行期配置文件路径（含真实密钥，已被 .gitignore 忽略）
CONFIG_PATH = ".potatocode/config.yaml"


async def _amain() -> int:
    """异步入口：让 MCP 会话覆盖完整的 TUI 生命周期。"""
    try:
        cfg = config.load(CONFIG_PATH)
    except ConfigError as e:
        # N4：可读错误提示，而非崩溃堆栈
        print(f"配置错误: {e}", file=sys.stderr)
        return 1

    root = str(Path.cwd().resolve())
    registry = new_default_registry()
    manager = await mcp.new_manager(mcp.load_config(root), version=__version__)
    try:
        for remote_tool in manager.tools():
            registry.register(remote_tool)
        # 权限引擎（五层防御的前四层）：根于项目目录；解析失败仅降级提示、继续启动
        engine, err = permission.new_engine(root)
        if err is not None:
            print(f"权限引擎降级: {err}", file=sys.stderr)
        await PotatoCodeApp(cfg.providers, __version__, registry, engine).run_async()
    except KeyboardInterrupt:
        pass  # 终端层的 Ctrl+C，安静退出
    finally:
        await manager.close()
    return 0


def main() -> None:
    """CLI 同步入口。"""
    raise SystemExit(asyncio.run(_amain()))
