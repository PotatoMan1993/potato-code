"""MCP server 配置加载、合并、环境变量展开与校验。"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

logger = logging.getLogger(__name__)

_ENV_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class ServerConfig:
    """单个已校验、已展开环境变量的 MCP server 定义。"""

    type: Literal["stdio", "http"]
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    """合并后的 MCP 配置。"""

    servers: dict[str, ServerConfig] = field(default_factory=dict)


@dataclass
class _RawServer:
    """尚未校验的 server 定义；保留原始类型以便统一降级。"""

    type: Any = None
    command: Any = None
    args: Any = None
    env: Any = None
    url: Any = None
    headers: Any = None


def _load_file(path: Path) -> dict[str, _RawServer]:
    """读取单层配置；文件或结构非法时把该层降级为空。"""
    if not path.exists():
        return {}

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        logger.warning(f"load {path} failed: {exc}")
        return {}

    if data is None:
        return {}
    if not isinstance(data, dict):
        logger.warning(f"load {path} failed: top-level YAML value must be a mapping")
        return {}

    raw_servers = data.get("mcp_servers")
    if raw_servers is None:
        return {}
    if not isinstance(raw_servers, dict):
        logger.warning(f"load {path} failed: mcp_servers must be a mapping")
        return {}

    servers: dict[str, _RawServer] = {}
    for name, value in raw_servers.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            logger.warning(f"skip server {name}: definition must be a mapping with a string name")
            continue
        servers[name] = _RawServer(
            type=value.get("type"),
            command=value.get("command"),
            args=value.get("args"),
            env=value.get("env"),
            url=value.get("url"),
            headers=value.get("headers"),
        )
    return servers


def _expand_vars(value: str) -> tuple[str, list[str]]:
    """展开字符串中的 ``${VAR}``，并返回未定义变量名。"""
    undefined: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            undefined.append(name)
            return ""
        return os.environ[name]

    return _ENV_VAR.sub(replace, value), undefined


def _apply_expansion(name: str, server: _RawServer) -> None:
    """只展开 env/headers 的值；同一 server 的同一变量只告警一次。"""
    undefined: set[str] = set()
    for attr in ("env", "headers"):
        values = getattr(server, attr)
        if not isinstance(values, dict):
            continue
        expanded: dict[Any, Any] = {}
        for key, value in values.items():
            if isinstance(value, str):
                expanded_value, missing = _expand_vars(value)
                expanded[key] = expanded_value
                undefined.update(missing)
            else:
                expanded[key] = value
        setattr(server, attr, expanded)

    for variable in sorted(undefined):
        logger.warning(f"undefined env var ${{{variable}}} referenced by server {name}")


def _merge_servers(
    user: dict[str, _RawServer], project: dict[str, _RawServer]
) -> dict[str, _RawServer]:
    """按 server 名合并；项目级定义完整覆盖用户级同名定义。"""
    merged = dict(user)
    merged.update(project)
    return merged


def _string_map(value: Any, field_name: str) -> tuple[dict[str, str] | None, str | None]:
    if value is None:
        return {}, None
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        return None, f"{field_name} must be a string map"
    return dict(value), None


def _string_list(value: Any, field_name: str) -> tuple[list[str] | None, str | None]:
    if value is None:
        return [], None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None, f"{field_name} must be a string array"
    return list(value), None


def _validate_server(name: str, server: _RawServer) -> ServerConfig | None:
    """把原始定义转换成安全、完整的 ServerConfig。"""
    if server.type not in ("stdio", "http"):
        logger.warning(f"skip server {name}: type must be 'stdio' or 'http'")
        return None

    if server.type == "stdio":
        if not isinstance(server.command, str) or not server.command:
            logger.warning(f"skip server {name}: stdio server requires a non-empty command")
            return None
        args, args_error = _string_list(server.args, "args")
        env, env_error = _string_map(server.env, "env")
        error = args_error or env_error
        if error is not None:
            logger.warning(f"skip server {name}: {error}")
            return None
        assert args is not None and env is not None
        return ServerConfig(type="stdio", command=server.command, args=args, env=env)

    if not isinstance(server.url, str) or not server.url:
        logger.warning(f"skip server {name}: http server requires a non-empty url")
        return None
    headers, headers_error = _string_map(server.headers, "headers")
    if headers_error is not None:
        logger.warning(f"skip server {name}: {headers_error}")
        return None
    assert headers is not None
    return ServerConfig(type="http", url=server.url, headers=headers)


def load_config(root: str) -> Config:
    """加载用户级与项目级 MCP 配置；所有错误均降级为告警。"""
    try:
        user_path = Path.home() / ".potatocode" / "mcp_servers.yaml"
        user = _load_file(user_path)
    except Exception as exc:  # Path.home 等平台异常也不能阻断启动
        logger.warning(f"load user config failed: {exc}")
        user = {}

    try:
        project = _load_file(Path(root) / ".potatocode" / "mcp_servers.yaml")
    except Exception as exc:
        logger.warning(f"load project config failed: {exc}")
        project = {}

    for name, server in user.items():
        _apply_expansion(name, server)
    for name, server in project.items():
        _apply_expansion(name, server)

    valid: dict[str, ServerConfig] = {}
    for name, server in _merge_servers(user, project).items():
        try:
            normalized = _validate_server(name, server)
        except Exception as exc:
            logger.warning(f"skip server {name}: validation failed: {exc}")
            continue
        if normalized is not None:
            valid[name] = normalized
    return Config(servers=valid)


__all__ = ["Config", "ServerConfig", "load_config"]
