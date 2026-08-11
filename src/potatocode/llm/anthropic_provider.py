"""anthropic 适配器：封装 AsyncAnthropic，统一吐出文本增量。

- 注入内置 system prompt；
- 按配置开启扩展思考（thinking），思考增量接收即丢弃；
- 正常结束 yield done 事件，异常 yield err 事件（不抛出给上层）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import anthropic

from ..prompt import SYSTEM_PROMPT
from . import Message, StreamEvent

if TYPE_CHECKING:
    from ..config import ProviderConfig


class AnthropicProvider:
    """anthropic 协议适配器。"""

    def __init__(self, cfg: ProviderConfig) -> None:
        self._name = cfg.name
        self._model = cfg.model
        self._thinking = cfg.thinking
        # base_url 非空时覆盖默认端点（接入兼容服务）
        self._client = anthropic.AsyncAnthropic(
            api_key=cfg.api_key,
            base_url=cfg.base_url or None,
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model

    async def stream(self, msgs: list[Message]) -> AsyncIterator[StreamEvent]:
        """发起一轮流式对话，以 async generator 吐出 StreamEvent。"""
        messages = [{"role": m.role, "content": m.content} for m in msgs]
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 4096,
            "system": SYSTEM_PROMPT,
            "messages": messages,
        }
        if self._thinking:
            # 扩展思考：budget_tokens 必须小于 max_tokens
            params["thinking"] = {"type": "enabled", "budget_tokens": 2048}

        try:
            async with self._client.messages.stream(**params) as stream:
                async for event in stream:
                    # 只取正文文本增量；thinking_delta 与其他事件一律丢弃
                    if (
                        event.type == "content_block_delta"
                        and event.delta.type == "text_delta"
                    ):
                        yield StreamEvent(text=event.delta.text)
            yield StreamEvent(done=True)
        except asyncio.CancelledError:
            raise  # 调用方 cancel 时正常终止，SDK 流由 async with 清理
        except Exception as e:  # 鉴权失败 / 限流 / 网络中断 / 模型不存在等
            yield StreamEvent(err=e)
