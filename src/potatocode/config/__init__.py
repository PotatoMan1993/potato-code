"""config 包：对外暴露配置类型、加载入口与上下文窗口派生函数。"""

from .config import Config, ConfigError, ProviderConfig, effective_context_window, load
from .locate import (
    CONFIG_PATH_ENV_VARIABLE,
    config_candidate_paths,
    load_for_workspace,
)
from .protocol_defaults import DEFAULT_ANTHROPIC_CONTEXT_WINDOW, DEFAULT_OPENAI_CONTEXT_WINDOW

__all__ = [
    "CONFIG_PATH_ENV_VARIABLE",
    "DEFAULT_ANTHROPIC_CONTEXT_WINDOW",
    "DEFAULT_OPENAI_CONTEXT_WINDOW",
    "Config",
    "ConfigError",
    "ProviderConfig",
    "config_candidate_paths",
    "effective_context_window",
    "load",
    "load_for_workspace",
]
