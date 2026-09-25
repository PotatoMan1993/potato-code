"""各协议的默认上下文窗口（token）。

未在 provider 配置里写 `context_window` 时按协议取这里的默认值；
常量放在 config 自身，避免 config 反向依赖上下文管理模块。
"""

from __future__ import annotations

# anthropic 协议默认上下文窗口
DEFAULT_ANTHROPIC_CONTEXT_WINDOW = 200000
# openai 协议默认上下文窗口
DEFAULT_OPENAI_CONTEXT_WINDOW = 128000
