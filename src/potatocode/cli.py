"""PotatoCode 入口：加载配置、启动 TUI。

banner 由 TUI 在 on_mount 时写入对话区（见 tui/app.py），此处不重复打印。
"""

from __future__ import annotations

import sys

from . import config
from .config import ConfigError
from .tui import PotatoCodeApp

# 运行期配置文件路径（含真实密钥，已被 .gitignore 忽略）
CONFIG_PATH = ".potatocode/config.yaml"


def main() -> None:
    """CLI 入口：加载并校验配置，启动 Textual 应用。"""
    try:
        cfg = config.load(CONFIG_PATH)
    except ConfigError as e:
        # N4：可读错误提示，而非崩溃堆栈
        print(f"配置错误: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        PotatoCodeApp(cfg.providers).run()
    except KeyboardInterrupt:
        pass  # 终端层的 Ctrl+C，安静退出
