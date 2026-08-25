"""环境信息：采集运行环境并渲染为系统提示的独立第二段（F2/N4/N5）。

环境信息不进缓存、不读任何环境变量；git 状态外调带短超时，失败降级留空。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import date

# git 状态外调的超时上限（N4：快速且有界，不阻塞请求发起）
_GIT_TIMEOUT: float = 2.0


@dataclass
class Environment:
    """一次会话启动时采集的运行环境快照（F2）。"""

    working_dir: str = ""  # 工作目录（os.getcwd()）
    platform: str = ""  # 平台（sys.platform）
    date: str = ""  # 当前日期（ISO 格式）
    git_status: str = ""  # git 状态摘要；非 git 目录/取不到则留空
    version: str = ""  # 应用版本（从 agent 透传）
    model: str = ""  # 当前模型（provider.model）

    def render(self) -> str:
        """渲染为「环境信息」段：逐行 `Key: Value`，空值项省略。"""
        lines: list[str] = []
        if self.working_dir:
            lines.append(f"Working directory: {self.working_dir}")
        if self.platform:
            lines.append(f"Platform: {self.platform}")
        if self.date:
            lines.append(f"Date: {self.date}")
        if self.git_status:
            lines.append(f"Git status: {self.git_status}")
        if self.version:
            lines.append(f"Version: {self.version}")
        if self.model:
            lines.append(f"Model: {self.model}")
        return "\n".join(lines)


def _collect_git_status() -> str:
    """用一条 `git status --porcelain` 获取摘要；失败/非 git 目录返回空串。

    返回码非零、命令不存在、超时、OSError 一律降级为空串（N4）；不读环境变量（N5）。
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    out = proc.stdout.strip()
    if not out:
        return "干净"
    return f"{len(out.splitlines())} 个文件改动"


def gather_environment(version: str, model: str) -> Environment:
    """采集运行环境（工作目录/平台/日期/git 状态/版本/模型），单项失败降级留空。"""
    try:
        working_dir = os.getcwd()
    except OSError:
        working_dir = ""

    return Environment(
        working_dir=working_dir,
        platform=sys.platform,
        date=date.today().isoformat(),
        git_status=_collect_git_status(),
        version=version,
        model=model,
    )
