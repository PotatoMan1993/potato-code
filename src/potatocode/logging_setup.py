"""日志出口装配：把 PotatoCode 自身的日志写到当前会话目录下的 session.log。

本模块是唯一决定「日志写往何处、写到多详细」的地方，且只依赖标准库——它必须在配置层
可用之前就能工作，因为配置错误、MCP 连接失败等启动早期事件也要落盘（见 docs/ch10/spec.md F2）。
"""

from __future__ import annotations

import logging
import os

# 包级日志器名。各模块统一用 logging.getLogger(__name__)，其公共祖先是这个名字
PACKAGE_LOGGER = "potatocode"
# 详细度环境变量名，取值是标准库级别名（大小写不敏感）
LEVEL_ENV = "POTATOCODE_LOG_LEVEL"
# 默认详细度：信息级。更详细的调试级需显式开启
DEFAULT_LEVEL = "INFO"
# 四要素格式：时间、级别、来源模块与行号、消息
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s:%(lineno)d — %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 当前已装配的出口。重复装配时先摘除旧的，避免出口叠加与文件句柄泄漏
_installed: logging.Handler | None = None


def resolve_level(raw: str | None) -> int:
    """把级别名解析为标准库级别值；空值、非字符串或无法识别时回落默认级别。"""
    levels = logging.getLevelNamesMapping()
    if not isinstance(raw, str):
        return levels[DEFAULT_LEVEL]
    return levels.get(raw.strip().upper(), levels[DEFAULT_LEVEL])


def setup_logging(log_path: str, level_name: str | None = None) -> logging.Handler | None:
    """在包级日志器上装配文件出口，返回装好的处理器；失败时返回 None 供调用方降级。

    level_name 为 None 时读环境变量。出口挂在包级日志器上并关闭其传播：依赖库的日志器
    不是它的后代，记录永远到不了这里，第三方隔离由此结构性成立（spec F5）；关闭传播同时
    避免记录上浮到根日志器——根上无处理器时标准库会把告警级写回标准错误流，那正是本次要
    消灭的花屏。
    """
    global _installed
    logger = logging.getLogger(PACKAGE_LOGGER)
    _detach(logger)  # 先摘掉上一次装配的出口，保证重复装配不叠加

    handler: logging.Handler | None = None
    try:
        # 显式 UTF-8：Windows 简体中文环境的本地编码是 GBK，否则日志中的中文会乱码
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        level = resolve_level(level_name if level_name is not None else os.environ.get(LEVEL_ENV))
        logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False
    except Exception:
        # 日志是观测手段而非启动前提：目录不可写等情况降级继续，不阻断应用（spec N4）
        if handler is not None:
            handler.close()
        return None

    _installed = handler
    return handler


def close_logging(handler: logging.Handler | None) -> None:
    """刷新并关闭装配时返回的处理器并从日志器摘除；传入 None 时直接返回。"""
    if handler is None:
        return
    logging.getLogger(PACKAGE_LOGGER).removeHandler(handler)
    handler.close()
    _forget(handler)


def _detach(logger: logging.Logger) -> None:
    """摘除并关闭本模块此前装上的出口，供重复装配前清理。"""
    installed = _installed
    _forget(None)
    if installed is not None:
        logger.removeHandler(installed)
        installed.close()


def _forget(handler: logging.Handler | None) -> None:
    """把模块记录的已装配出口置为指定值（None 表示当前无出口）。"""
    global _installed
    _installed = handler
