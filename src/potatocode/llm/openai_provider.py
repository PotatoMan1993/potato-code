"""openai 适配器：封装 AsyncOpenAI chat.completions，统一吐出文本增量。

- 内置 system prompt 作为首条 system 消息注入；
- thinking 配置在 openai 协议下忽略；
- 正常结束 yield done 事件，异常 yield err 事件（不抛出给上层）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import openai

from ..prompt import SYSTEM_PROMPT
from . import Message, StreamEvent

if TYPE_CHECKING:
    from ..config import ProviderConfig


class OpenAIProvider:
    """openai 协议适配器（兼容各类 OpenAI 协议端点）。"""

    def __init__(self, cfg: ProviderConfig) -> None:
        self._name = cfg.name
        self._model = cfg.model
        # base_url 非空时覆盖默认端点（接入兼容服务）
        self._client = openai.AsyncOpenAI(
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
        # 首条注入内置 system prompt，其后是完整多轮历史
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages += [{"role": m.role, "content": m.content} for m in msgs]

        try:
            stream = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                stream=True,
            )
            async for chunk in stream:
                # 个别 chunk（如仅含 usage）可能没有 choices
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta:
                    yield StreamEvent(text=delta)
            yield StreamEvent(done=True)
        except asyncio.CancelledError:
            raise  # 调用方 cancel 时正常终止
        except Exception as e:  # 鉴权失败 / 限流 / 网络中断 / 模型不存在等
            yield StreamEvent(err=e)
