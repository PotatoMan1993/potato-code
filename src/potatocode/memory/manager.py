"""记忆管理器：编排项目级与用户级两级笔记的加载与异步更新。

读取路径（启动时、每次注入系统提示前）与写入路径（每轮对话结束后异步触发）分开：
读是纯文件读取，任何时刻都可调用；写要经过 LLM，因此只在后台 task 里跑，且用
`asyncio.Lock` 串行化，避免两次更新互相覆盖对方的读-写结果（N2/F47）。
所有失败都静默记日志，绝不影响主会话（F42/N5）。
"""

from __future__ import annotations

import asyncio
import json
import logging

from ..llm import Message, Provider, Request, System
from .const import LEVEL_PROJECT, LEVEL_USER, MAX_INDEX_BYTES, TRUNCATED_MARK
from .prompts import MEMORY_UPDATE_SYSTEM_PROMPT, render_update_request
from .store import Store
from .types import UpdateAction

logger = logging.getLogger(__name__)


class Manager:
    """两级笔记仓库的编排者。"""

    def __init__(
        self,
        project_dir: str,
        user_dir: str,
        provider: Provider | None = None,
        model: str = "",
    ) -> None:
        self._project = Store(project_dir)
        self._user = Store(user_dir)
        self._provider = provider
        self._model = model
        self._lock = asyncio.Lock()

    @property
    def provider(self) -> Provider | None:
        """当前用于记忆更新的 provider（未选定时为 None）。"""
        return self._provider

    @property
    def model(self) -> str:
        """当前用于记忆更新的模型名。"""
        return self._model

    @property
    def project_store(self) -> Store:
        """项目级笔记仓库。"""
        return self._project

    @property
    def user_store(self) -> Store:
        """用户级笔记仓库。"""
        return self._user

    def set_provider(self, provider: Provider, model: str) -> None:
        """选定 provider 后延迟注入（启动时 provider 尚未确定）。"""
        self._provider = provider
        self._model = model

    def load_index(self) -> str:
        """拼接两级索引（项目级在前、用户级在后），超过 25KB 截断并标注（F32/F34）。"""
        parts = [
            text.strip()
            for text in (self._project.load_index(), self._user.load_index())
            if text.strip()
        ]
        merged = "\n".join(parts)
        data = merged.encode("utf-8")
        if len(data) <= MAX_INDEX_BYTES:
            return merged
        head = data[:MAX_INDEX_BYTES].decode("utf-8", errors="ignore")
        return f"{head}\n{TRUNCATED_MARK}"

    async def update_async(self, recent_msgs: list[Message]) -> None:
        """异步执行一次记忆更新；任何失败都吞掉（F42）。"""
        if self._provider is None or not recent_msgs:
            return
        try:
            async with self._lock:  # 串行化：两次更新不并发读-改-写同一批文件
                await self._update(recent_msgs)
        except Exception:
            logger.exception("记忆更新失败（已跳过，不影响本次会话）")

    # ────────────────────────── 内部 ──────────────────────────

    async def _update(self, recent_msgs: list[Message]) -> None:
        """请求 LLM 产出操作列表并分发到两级仓库。"""
        provider = self._provider
        assert provider is not None  # update_async 已挡掉未选定 provider 的情况
        req = Request(
            messages=[
                Message(
                    role="user",
                    content=render_update_request(self.load_index(), recent_msgs),
                )
            ],
            tools=None,  # F38：记忆更新不给工具定义
            system=System(stable=MEMORY_UPDATE_SYSTEM_PROMPT),
        )
        parts: list[str] = []
        async for ev in provider.stream(req):
            if ev.err is not None:
                raise ev.err
            if ev.text:
                parts.append(ev.text)

        actions = parse_actions("".join(parts))
        if not actions:
            logger.debug("本次记忆更新无操作")
            return
        project = [a for a in actions if a.level == LEVEL_PROJECT]
        user = [a for a in actions if a.level == LEVEL_USER]
        self._project.apply(project)
        self._user.apply(user)
        logger.info("记忆更新完成：project=%d user=%d", len(project), len(user))


def parse_actions(text: str) -> list[UpdateAction]:
    """解析模型返回的 JSON 数组；格式非法时返回空列表（F42 静默）。"""
    payload = _strip_fences(text)
    if not payload:
        return []
    try:
        data = json.loads(payload)
    except ValueError:
        logger.warning("记忆更新返回不是合法 JSON，已跳过")
        return []
    if not isinstance(data, list):
        logger.warning("记忆更新返回不是 JSON 数组，已跳过")
        return []
    actions: list[UpdateAction] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        action = UpdateAction.from_dict(item)
        if action is not None:
            actions.append(action)
    return actions


def _strip_fences(text: str) -> str:
    """剥掉可能出现的 Markdown 代码块围栏，只留 JSON 本体。"""
    body = text.strip()
    if not body.startswith("```"):
        return body
    lines = body.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    while lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


__all__ = ["Manager", "parse_actions"]
