"""过期会话清理：删除启动时刻距当前超过阈值的会话目录。

只清理能解析出时间戳的新格式 id 目录；旧格式目录（ch08 遗留）一律保留（N3）。
单个目录删除失败只记日志，不影响其余目录（F26）。
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from ..compact.state import parse_session_time

logger = logging.getLogger(__name__)

# 会话保留期：超过即清理
MAX_AGE = timedelta(days=30)


def clean_expired(
    sessions_dir: str,
    max_age: timedelta = MAX_AGE,
    now: datetime | None = None,
) -> int:
    """删除超过 max_age 的会话目录，返回实际删除的目录数。"""
    root = Path(sessions_dir)
    try:
        children = list(root.iterdir())
    except OSError:  # 目录不存在：无事可做
        return 0

    now = now or datetime.now()
    removed = 0
    for child in children:
        if not child.is_dir():
            continue
        started = parse_session_time(child.name)
        if started is None:
            continue  # 旧格式 / 非法 id：不清理，避免误删
        if now - started <= max_age:
            continue
        try:
            shutil.rmtree(child)
            removed += 1
            logger.info("清理过期会话: %s", child.name)
        except OSError as e:  # 占用/权限问题：跳过该目录，继续清理其余的
            logger.warning("清理过期会话失败: %s: %s", child, e)
    return removed


__all__ = ["MAX_AGE", "clean_expired"]
