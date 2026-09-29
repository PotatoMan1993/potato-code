"""日志出口装配的行为测试：落盘、详细度、隔离、编码、堆栈与降级。"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from potatocode.logging_setup import (
    LEVEL_ENV,
    PACKAGE_LOGGER,
    close_logging,
    resolve_level,
    setup_logging,
)


@pytest.fixture
def log_path(tmp_path) -> Callable[..., Path]:
    """返回临时日志路径的构造函数，并在用例结束后清理出口、还原日志器状态。"""
    logger = logging.getLogger(PACKAGE_LOGGER)
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate

    yield lambda name="session.log": tmp_path / name

    # 收尾：关掉用例期间新增的出口，还原级别与传播，避免污染后续用例
    for handler in list(logger.handlers):
        if handler not in original_handlers:
            close_logging(handler)
    logger.setLevel(original_level)
    logger.propagate = original_propagate


def test_default_level_keeps_info_drops_debug(log_path):
    """默认详细度为信息级：信息级落盘，调试级被过滤。"""
    path = log_path()
    setup_logging(str(path), None)

    logger = logging.getLogger("potatocode.compact.layer2")
    logger.info("信息级消息")
    logger.debug("调试级消息")

    text = path.read_text(encoding="utf-8")
    assert "信息级消息" in text
    assert "调试级消息" not in text


def test_explicit_debug_level_keeps_debug(log_path):
    """显式指定调试级时，调试级记录也落盘。"""
    path = log_path()
    setup_logging(str(path), "DEBUG")

    logging.getLogger("potatocode.demo").debug("调试级消息")

    assert "调试级消息" in path.read_text(encoding="utf-8")


def test_env_var_raises_level_without_code_change(log_path, monkeypatch):
    """详细度可由环境变量提升，无需改动代码或配置文件。"""
    monkeypatch.setenv(LEVEL_ENV, "debug")
    path = log_path()
    setup_logging(str(path), None)

    logging.getLogger("potatocode.demo").debug("调试级消息")

    assert "调试级消息" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, logging.INFO),
        ("debug", logging.DEBUG),
        ("  WARNING ", logging.WARNING),
        ("bogus", logging.INFO),
        (123, logging.INFO),
    ],
)
def test_resolve_level(raw, expected):
    """级别名解析：大小写与空白不敏感，非法或缺省值回落到信息级。"""
    assert resolve_level(raw) == expected


def test_third_party_records_are_excluded(log_path):
    """第三方日志隔离：不属于本项目包的记录不落盘，即使详细度为调试级。"""
    path = log_path()
    setup_logging(str(path), "DEBUG")

    logging.getLogger("anthropic._client").debug("第三方调试内容")
    logging.getLogger("httpx").warning("第三方告警内容")

    assert path.read_text(encoding="utf-8") == ""


def test_chinese_round_trips_as_utf8(log_path):
    """中文内容按 UTF-8 落盘，读回无乱码。"""
    path = log_path()
    setup_logging(str(path), None)

    logging.getLogger("potatocode.demo").info("中文：上下文压缩完成")

    assert "中文：上下文压缩完成" in path.read_bytes().decode("utf-8")


def test_line_has_four_elements(log_path):
    """每行含时间、级别、来源模块与行号、消息四要素。"""
    path = log_path()
    setup_logging(str(path), None)

    logging.getLogger("potatocode.demo").info("正文")

    line = path.read_text(encoding="utf-8").strip()
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO\s+potatocode\.demo:\d+ — 正文", line
    )


def test_exception_stack_is_written(log_path):
    """以异常级记录时，完整堆栈落盘（本章的核心动机）。"""
    path = log_path()
    setup_logging(str(path), None)

    try:
        raise RuntimeError("压缩炸了")
    except RuntimeError:
        logging.getLogger("potatocode.compact.compact").exception("上下文压缩失败")

    text = path.read_text(encoding="utf-8")
    assert "上下文压缩失败" in text
    assert "Traceback" in text
    assert "RuntimeError" in text
    assert "压缩炸了" in text


def test_repeated_setup_does_not_duplicate(log_path):
    """重复装配不叠加：同一路径只保留一份出口，记录不重复。"""
    path = log_path()
    setup_logging(str(path), None)
    setup_logging(str(path), None)

    logging.getLogger("potatocode.demo").info("只应出现一次")

    assert path.read_text(encoding="utf-8").count("只应出现一次") == 1


def test_unwritable_path_degrades_without_raising(tmp_path):
    """路径不可写时返回 None 且不上抛；收尾函数接受 None。"""
    blocked = tmp_path / "as-directory"
    blocked.mkdir()

    assert setup_logging(str(blocked)) is None
    close_logging(None)


def test_propagation_is_disabled(log_path):
    """传播已关闭，记录不会上浮到根日志器触发兜底输出。"""
    path = log_path()
    setup_logging(str(path), None)

    assert logging.getLogger(PACKAGE_LOGGER).propagate is False
