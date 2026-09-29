"""测试共享夹具。

本项目的日志出口挂在包级日志器 `potatocode` 上，并关闭了它向上的传播（见
`potatocode.logging_setup`），因此测试框架默认挂在根日志器上的捕获器收不到记录。
需要断言日志内容的用例统一改用下面的 `pkg_logs` 夹具。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

# 包级日志器名，与 potatocode.logging_setup.PACKAGE_LOGGER 一致
PACKAGE_LOGGER = "potatocode"


class LogCollector(logging.Handler):
    """测试用日志收集器：把记录收进内存列表，绕开包级日志器的传播关闭。"""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)  # 处理器本身不过滤，级别统一由夹具控制
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """收下一条记录，保留原始对象供断言读取级别、来源等字段。"""
        self.records.append(record)

    def text(self) -> str:
        """把所有记录的消息拍平成换行分隔的字符串，便于沿用子串断言的写法。"""
        return "\n".join(r.getMessage() for r in self.records)


@pytest.fixture
def pkg_logs() -> Iterator[LogCollector]:
    """在包级日志器上临时挂一个收集器，并把级别放宽到调试级。

    收尾时摘除收集器并还原原级别，保证用例之间互不影响。
    """
    collector = LogCollector()
    logger = logging.getLogger(PACKAGE_LOGGER)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(collector)
    try:
        yield collector
    finally:
        logger.removeHandler(collector)
        logger.setLevel(previous_level)
