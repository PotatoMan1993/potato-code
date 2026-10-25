"""配置层：读取并校验 `.potatocode/config.yaml`，产出 providers 列表。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from .protocol_defaults import DEFAULT_ANTHROPIC_CONTEXT_WINDOW, DEFAULT_OPENAI_CONTEXT_WINDOW


class ConfigError(Exception):
    """配置缺失或非法时抛出的可读错误。"""


@dataclass
class ProviderConfig:
    """单个 provider 的配置项。"""

    name: str  # 可读名称（状态栏左侧显示）
    protocol: Literal["anthropic", "openai"]  # 协议类型
    api_key: str  # 密钥
    model: str  # 模型名（状态栏右侧显示）
    base_url: str | None = None  # 自定义端点；None 则用 SDK 默认端点
    thinking: bool = False  # 是否开启扩展思考（仅 anthropic 生效）
    context_window: int = 0  # 上下文窗口（token）；0 表示走协议默认值


@dataclass
class Config:
    """应用配置：providers 列表 + 子 Agent 后台开关（ch15 N6）。"""

    providers: list[ProviderConfig] = field(default_factory=list)
    # ch15 N6：False 时所有 SubAgent 强制前台同步（超时切后台 / Fork 均失效）；None 视为 True
    enable_subagent_background: bool | None = None

    def effective_enable_subagent_background(self) -> bool:
        """后台能力是否可用：未配置默认开启（ch15 N6）。"""
        return True if self.enable_subagent_background is None else self.enable_subagent_background


# protocol 合法取值
_PROTOCOLS = {"anthropic", "openai"}


def effective_context_window(p: ProviderConfig) -> int:
    """取 provider 的上下文窗口：显式配置优先，否则按协议给默认值。"""
    if p.context_window > 0:
        return p.context_window
    if p.protocol == "openai":
        return DEFAULT_OPENAI_CONTEXT_WINDOW
    return DEFAULT_ANTHROPIC_CONTEXT_WINDOW  # anthropic 与未知协议都取保守默认


def load(path: str) -> Config:
    """加载并校验配置文件。

    失败时抛出携带可读信息的 ConfigError（指明哪个 provider 的哪个字段）。
    """
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"配置文件不存在: {path}")

    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"配置文件 YAML 格式错误: {path}: {e}") from e

    return _from_dict(raw, path)


def _from_dict(raw: object, path: str) -> Config:
    """把解析后的 dict 映射为 Config，逐项校验。"""
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件格式错误（应为含 providers 的映射）: {path}")

    providers_raw = raw.get("providers")
    if not isinstance(providers_raw, list) or not providers_raw:
        raise ConfigError("providers 不能为空（至少配置一个 provider）")

    providers: list[ProviderConfig] = []
    for i, item in enumerate(providers_raw):
        providers.append(_parse_provider(item, i))

    # 可选布尔开关（ch15 N6）：缺省 None（视为 True）
    enable_bg = raw.get("enable_subagent_background")
    if enable_bg is not None and not isinstance(enable_bg, bool):
        raise ConfigError("enable_subagent_background 应为布尔值")

    return Config(providers=providers, enable_subagent_background=enable_bg)


def _parse_provider(item: object, index: int) -> ProviderConfig:
    """解析并校验单个 provider 条目。"""
    if not isinstance(item, dict):
        raise ConfigError(f"providers[{index}] 应为映射（name/protocol/api_key/model）")

    prefix = f"providers[{index}]"

    # 必填字符串字段：非空校验
    name = _require_str(item, "name", prefix)
    protocol = _require_str(item, "protocol", prefix)
    api_key = _require_str(item, "api_key", prefix)
    model = _require_str(item, "model", prefix)

    if protocol not in _PROTOCOLS:
        raise ConfigError(f"{prefix}.protocol 非法: {protocol!r}（应为 'anthropic' 或 'openai'）")

    # 可选字段
    base_url = item.get("base_url")
    if base_url is not None and not isinstance(base_url, str):
        raise ConfigError(f"{prefix}.base_url 应为字符串")
    if isinstance(base_url, str) and not base_url.strip():
        base_url = None

    thinking = item.get("thinking", False)
    if not isinstance(thinking, bool):
        raise ConfigError(f"{prefix}.thinking 应为布尔值")

    context_window = item.get("context_window", 0)
    if not isinstance(context_window, int) or isinstance(context_window, bool):
        raise ConfigError(f"{prefix}.context_window 应为整数（单位 token）")
    if context_window < 0:
        raise ConfigError(f"{prefix}.context_window 不能为负数")

    return ProviderConfig(
        name=name,
        protocol=protocol,  # type: ignore[arg-type]  # 上面已校验取值范围
        api_key=api_key,
        model=model,
        base_url=base_url,
        thinking=thinking,
        context_window=context_window,
    )


def _require_str(item: dict, key: str, prefix: str) -> str:
    """取必填字符串字段，缺失或为空时报可读错误。"""
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{prefix}.{key} 不能为空")
    return value.strip()
