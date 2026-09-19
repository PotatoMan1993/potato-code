"""请求级用量台账：原值和历史边界可查询，不累计历史输入充当上下文。"""

from __future__ import annotations

import copy
import hashlib

from ..conversation import HistorySnapshot
from ..llm import Provider, Request, Usage
from .types import RequestRecord, RequestStatus, TokenEstimate, UsageAnchor


class UsageLedger:
    def __init__(self) -> None:
        self._records: dict[int, RequestRecord] = {}
        self._pending: dict[int, tuple[str, tuple[tuple[str, str], ...], int]] = {}
        self.anchor: UsageAnchor | None = None

    def begin(
        self,
        provider: Provider,
        req: Request,
        snapshot: HistorySnapshot,
        start: int,
        end: int,
        attempt: int,
        estimate: TokenEstimate,
        character_cost: int | None = None,
    ) -> int:
        if not 0 <= start <= end <= len(snapshot.entries):
            raise ValueError("请求历史边界无效")
        selected = snapshot.entries[start:end]
        prefix = tuple((e.id, e.content_hash) for e in selected)
        request_id = len(self._records) + 1
        # 包括 system/tools/reminder 的真实协议输入，避免同一历史的不同请求混淆。
        fingerprint = hashlib.sha256(repr(provider.budget_shape(req)).encode("utf-8")).hexdigest()
        record = RequestRecord(
            request_id,
            req.purpose,
            attempt,
            provider.identity,
            provider.model,
            provider.protocol,
            snapshot.revision,
            snapshot.content_epoch,
            start,
            end,
            (selected[0].id if selected else None, selected[-1].id if selected else None),
            fingerprint,
            estimate.tokens,
        )
        if character_cost is None:
            from .estimate import TokenEstimator

            character_cost = TokenEstimator().estimate_fresh(provider, req)
        self._pending[request_id] = (snapshot.session_id, prefix, character_cost)
        self._records[request_id] = record
        return request_id

    def observe(self, request_id: int, usage: Usage) -> None:
        record = self._records[request_id]
        if record.status != "running":
            return
        if record.usage is None:
            record.usage = copy.deepcopy(usage)
        else:
            previous = record.usage
            if usage.input_tokens is not None:
                previous.input_tokens = usage.input_tokens
                previous.cache_read = usage.cache_read
                previous.cache_write = usage.cache_write
            if usage.output_tokens is not None:
                previous.output_tokens = usage.output_tokens
            previous.raw_counts.update(usage.raw_counts)
        record.input_tokens = record.usage.input_tokens
        record.output_tokens = record.usage.output_tokens
        record.raw_counts = dict(record.usage.raw_counts)

    def finish(
        self, request_id: int, status: RequestStatus, stop_reason: str | None = None
    ) -> RequestRecord:
        record = self._records[request_id]
        if record.status == "running":
            record.status = status
            record.stop_reason = stop_reason
            session_id, prefix, cost = self._pending.pop(request_id)
            if (
                status == "success"
                and record.purpose == "normal"
                and record.input_tokens is not None
            ):
                self.anchor = UsageAnchor(
                    record.request_id,
                    record.provider_key,
                    record.model,
                    record.protocol,
                    session_id,
                    record.content_epoch,
                    prefix,
                    cost,
                    record.input_tokens,
                )
        return copy.deepcopy(record)

    def get(self, request_id: int) -> RequestRecord:
        return copy.deepcopy(self._records[request_id])

    def records(self) -> tuple[RequestRecord, ...]:
        return tuple(copy.deepcopy(list(self._records.values())))
