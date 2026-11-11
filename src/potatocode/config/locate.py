"""配置文件定位：按「环境变量 → 项目级 → 用户级」三级顺序查找 config.yaml。

这样在任意工作目录都能启动 PotatoCode：项目内没有配置时回退到
`~/.potatocode/config.yaml`，不必把密钥复制到每个项目里。

「三级顺序」是候选顺序而非严格绑定：三个候选按优先级排好后逐个尝试，
**跳过不存在的和解析失败的**，第一个成功加载的生效。因此环境变量指向的文件
万一不存在（或内容非法），不会被当成致命错误，而是自然回退到项目级 / 用户级；
只有全部候选都不可用才报 ConfigError。
"""

from __future__ import annotations

import os
import logging
from pathlib import Path

from .config import Config, ConfigError, load

logger = logging.getLogger(__name__)

# 显式指定配置文件路径的环境变量（最高优先级，方便临时切换）
CONFIG_PATH_ENV_VARIABLE = "POTATOCODE_CONFIG_PATH"

# 项目级与用户级配置相对路径一致，只是根目录不同
CONFIG_RELATIVE_PATH = Path(".potatocode") / "config.yaml"


def _override_config_path() -> Path | None:
    """读取环境变量指定的配置路径；未设置或为空时返回 None。"""
    raw_value = os.environ.get(CONFIG_PATH_ENV_VARIABLE, "")
    stripped_value = raw_value.strip()
    if not stripped_value:
        return None
    return Path(stripped_value).expanduser()


def _user_config_path() -> Path | None:
    """用户级配置路径 `~/.potatocode/config.yaml`；取不到 home 时返回 None。"""
    try:
        return Path.home() / CONFIG_RELATIVE_PATH
    except (RuntimeError, OSError):  # 极端环境下 Path.home 可能失败，降级为无用户级
        return None


def _project_config_path(workspace_root: str | Path) -> Path:
    """项目级配置路径 `<workspace>/.potatocode/config.yaml`。"""
    return Path(workspace_root) / CONFIG_RELATIVE_PATH


def _deduplicate_paths(paths: list[Path]) -> list[Path]:
    """按规范化后的绝对路径去重，保持原有优先级顺序。"""
    unique_paths: list[Path] = []
    seen_keys: set[str] = set()
    for path in paths:
        normalized_key = os.path.normcase(os.path.abspath(str(path)))
        if normalized_key in seen_keys:
            continue
        seen_keys.add(normalized_key)
        unique_paths.append(path)
    return unique_paths


def config_candidate_paths(workspace_root: str | Path) -> list[Path]:
    """返回按优先级排列的候选路径（只排序、不判断可用性）。

    顺序：环境变量指定（若有）→ 项目级 → 用户级；同一路径只保留优先级最高的一次。

    实际加载时由 `load_for_workspace` 逐个尝试并跳过不可用的候选，
    这里不做 `is_file()` 过滤，便于错误信息完整列出「查过哪些路径」。
    """
    candidates = []
    override_path = _override_config_path()
    if override_path is not None:
        candidates.append(override_path)

    candidates.append(_project_config_path(workspace_root))
    user_path = _user_config_path()
    if user_path is not None:
        candidates.append(user_path)
    return _deduplicate_paths(candidates)


def _missing_config_error(candidates: list[Path]) -> ConfigError:
    """构造「配置缺失」的可读错误：列出查过的路径与解决方式。

    只有所有候选都不可用时才会走到这里，所以文案要一次说清两件事：
    查过哪些路径、怎么补上。若设置了环境变量，再额外点明它也尝试过且不可用，
    避免用户误以为环境变量没被读取。
    """
    listed_paths = "\n".join(f"  {index}. {path}" for index, path in enumerate(candidates, 1))
    override_path = _override_config_path()
    override_hint = ""
    if override_path is not None:
        override_hint = (
            f"注意：{CONFIG_PATH_ENV_VARIABLE} 指向 {override_path}，"
            f"但该文件不存在或内容非法，已继续查找其余候选。\n"
        )
    return ConfigError(
        f"未找到配置文件，已按以下顺序查找（均不存在或不可用）：\n{listed_paths}\n"
        f"{override_hint}"
        f"解决方式：在项目内创建 .potatocode/config.yaml（模板见 README），"
        f"或创建用户级 {CONFIG_RELATIVE_PATH.as_posix()}（位于用户主目录），"
        f"或用环境变量 {CONFIG_PATH_ENV_VARIABLE} 指向已有配置。"
    )


def load_for_workspace(workspace_root: str | Path) -> Config:
    """按三级优先级逐个尝试加载，返回第一个可用的配置。

    不可用的候选（文件不存在、YAML 语法错、字段校验不过）一律跳过并记 warning，
    优先级更低的候选因此有机会生效；全部候选都不可用时抛 `_missing_config_error`，
    错误信息里带上查过的全部路径。

    加载成功后把实际使用的路径记入 `Config.source_path`，便于日志追溯
    （调用方据此看清「这次到底读的是哪份配置」）。
    """
    candidates = config_candidate_paths(workspace_root)

    config: Config | None = None
    for candidate in candidates:
        if candidate.is_file():
            try:
                cfg = load(str(candidate))
            except ConfigError as e:
                # 该候选不可用：留痕后继续尝试下一优先级（不中断启动流程）
                logger.warning(f"load {candidate} error: {e}")
                continue
            config = cfg
            config.source_path = candidate
            break

    if config is None:
        raise _missing_config_error(candidates)
    return config


__all__ = [
    "CONFIG_PATH_ENV_VARIABLE",
    "CONFIG_RELATIVE_PATH",
    "config_candidate_paths",
    "load_for_workspace",
]
