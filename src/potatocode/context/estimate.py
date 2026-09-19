"""按实际协议输入形状估算；API 锚点仅校准未改写的普通请求前缀。"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from ..conversation import HistorySnapshot
from ..llm import Provider, Request
from .types import TokenEstimate

if TYPE_CHECKING:
    from .ledger import UsageLedger


def character_cost(text: str) -> int:
    """char-v1：ASCII 每四字符一 token，非 ASCII 每字符两 token。"""
    ascii_count = sum(c.isascii() for c in text)
    return (ascii_count + 3) // 4 + 2 * (len(text) - ascii_count)


class TokenEstimator:
    def __init__(self) -> None:
        self._costs: dict[str, int] = {}

    def _cost(self, text: str) -> int:
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if key not in self._costs:
            if len(self._costs) >= 8192:
                self._costs.clear()
            self._costs[key] = character_cost(text)
        return self._costs[key]

    def estimate_fresh(self, provider: Provider, req: Request) -> int:
        shape = provider.budget_shape(req)
        fixed = sum(self._cost(part) for part in shape.fixed)
        messages = sum(self._cost(part) + 8 for group in shape.messages for part in group)
        trailing = sum(self._cost(part) + 8 for part in shape.trailing)
        return 16 + fixed + messages + trailing

    def history_prefix_costs(
        self, provider: Provider, snapshot: HistorySnapshot
    ) -> tuple[int, ...]:
        shape = provider.budget_shape(Request(messages=snapshot.messages()))
        if len(shape.messages) != len(snapshot.entries):
            raise ValueError("预算形状必须与逻辑消息一一对应")
        values = [0]
        for group in shape.messages:
            values.append(values[-1] + sum(self._cost(part) + 8 for part in group))
        return tuple(values)

    def estimate_request(
        self, provider: Provider, req: Request, snapshot: HistorySnapshot, ledger: UsageLedger
    ) -> TokenEstimate:
        fresh = self.estimate_fresh(provider, req)
        anchor = ledger.anchor
        if anchor is not None and req.purpose == "normal":
            prefix = tuple((e.id, e.content_hash) for e in snapshot.entries[: len(anchor.prefix)])
            if (
                anchor.provider_key == provider.identity
                and anchor.model == provider.model
                and anchor.protocol == provider.protocol
                and anchor.session_id == snapshot.session_id
                and anchor.content_epoch == snapshot.content_epoch
                and prefix == anchor.prefix
            ):
                estimate = anchor.input_tokens + fresh - anchor.character_cost
                if estimate >= 0:
                    return TokenEstimate(estimate, "usage_anchored", anchor.request_id)
        return TokenEstimate(fresh)
