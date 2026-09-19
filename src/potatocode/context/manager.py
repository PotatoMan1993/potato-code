"""统一预检与摘要事务：切分合法历史、有界重试、候选成功后原子提交。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace

from ..conversation import (
    Conversation,
    HistoryEntry,
    HistorySnapshot,
    legal_boundaries,
    message_hash,
)
from ..llm import Message, Provider
from ..llm.errors import ContextWindowExceeded
from ..permission import Mode
from ..permission.engine import Engine
from .artifacts import ArtifactStore
from .attachments import ContextAttachments
from .estimate import TokenEstimator
from .ledger import UsageLedger
from .summary import SummaryRunner, build_summary_request
from .types import (
    AttachmentBundle,
    CompactReason,
    CompactResult,
    ContextEvent,
    Cut,
    PreparedRequest,
    RequestFrame,
    RunScope,
    SummaryDocument,
    TokenEstimate,
)

RETRY_DELAYS = (0.5, 1.0)


def render_reconstruction(
    document: SummaryDocument, bundle: AttachmentBundle, m: int, reason: CompactReason
) -> str:
    why = "按用户要求整理上下文" if reason == "manual" else "因上下文空间不足"
    sections = [
        f"本会话延续之前的对话，{why}，已将早期 {m} 条消息压缩到本条消息中。",
        document.render(),
        "## 最近使用的文件及内容",
    ]
    if not bundle.files:
        sections.append("无可用记录")
    for item in bundle.files:
        note = item.omitted_reason or ("内容已截断" if item.truncated else "")
        sections.append(f"### {item.path}\n{note}\n{item.text}")
    sections.append("## 使用过的 Skill")
    if not bundle.skills:
        sections.append("无可用记录")
    for item in bundle.skills:
        note = item.omitted_reason or ("仅已加载片段" if not item.complete else "已加载定义")
        if item.truncated:
            note += "；内容已截断"
        sections.append(
            f"### {item.path}\n{note}；版本 {item.source_version or '未记录'}；"
            f"范围 {item.start_line}–{item.end_line}\n{item.text}"
        )
    sections.append("以上内容为摘要重建的上下文，如需精确的代码，请重新读取源文件。")
    return "\n\n".join(sections)


class ContextManager:
    def __init__(self, conv: Conversation, engine: Engine) -> None:
        self.conv = conv
        self.engine = engine
        self.estimator = TokenEstimator()
        self.ledger = UsageLedger()
        self.artifacts = ArtifactStore(engine, conv.session_id)
        self.attachments = ContextAttachments(engine)
        self.summary = SummaryRunner(self.ledger, self.estimator)
        self._lock = asyncio.Lock()

    async def _light(self, mode: Mode, cancel: asyncio.Event) -> HistorySnapshot:
        if cancel.is_set():
            raise asyncio.CancelledError
        snapshot = self.conv.snapshot()
        legal_boundaries(snapshot.entries)
        patches = await self.artifacts.prepare(snapshot, mode, cancel)
        if cancel.is_set():
            raise asyncio.CancelledError
        self.conv.replace_tool_results(snapshot.revision, patches)
        snapshot = self.conv.snapshot()
        self.attachments.confirm_delivered(snapshot, self.artifacts)
        return snapshot

    def select_cut(
        self, snapshot: HistorySnapshot, provider: Provider, previous_cut: Cut | None = None
    ) -> Cut | None:
        limits = provider.context_limits
        limits.validate()
        assert limits.context_window is not None
        boundaries = legal_boundaries(snapshot.entries)
        costs = self.estimator.history_prefix_costs(provider, snapshot)
        total = len(snapshot.entries)
        for m in range(total - 5, 0, -1):
            if m not in boundaries or snapshot.entries[m].message.role == "tool":
                continue
            recent = costs[-1] - costs[m]
            if recent <= 10_000:
                continue
            early = costs[m]
            if previous_cut is not None and (
                m >= previous_cut.m or early > previous_cut.early_tokens * 9 // 10
            ):
                continue
            request = build_summary_request(snapshot, m, limits.compact_max_output_tokens)
            summary_cost = self.estimator.estimate_fresh(provider, request)
            if summary_cost <= limits.context_window - 20_000:
                return Cut(m, total - m, early, recent, summary_cost)
        return None

    async def preflight(
        self,
        provider: Provider,
        frame: RequestFrame,
        mode: Mode,
        cancel: asyncio.Event,
        scope: RunScope,
    ) -> AsyncGenerator[ContextEvent | PreparedRequest, None]:
        prepared: PreparedRequest
        final_event: ContextEvent | None = None
        try:
            async with self._lock:
                provider.context_limits.validate()
                if scope.circuit_broken:
                    raise ValueError("本轮上下文压缩已熔断")
                snapshot = await self._light(mode, cancel)
                request = frame.request(snapshot)
                estimate = self.estimator.estimate_request(provider, request, snapshot, self.ledger)
                window = provider.context_limits.context_window
                assert window is not None
                if (
                    estimate.tokens >= window - 33_000
                    or estimate.tokens + frame.max_output_tokens > window
                ):
                    result = CompactResult("failed", "摘要未返回结果")
                    async for item in self._compact_locked(
                        provider, frame, mode, cancel, scope, "auto", snapshot
                    ):
                        if isinstance(item, CompactResult):
                            result = item
                        else:
                            yield item
                    final_event = self._result_event(result, "auto")
                    if result.status in ("failed", "cancelled"):
                        prepared = PreparedRequest(snapshot, None, estimate, result.reason)
                    else:
                        snapshot = self.conv.snapshot()
                        request = frame.request(snapshot)
                        estimate = self.estimator.estimate_request(
                            provider, request, snapshot, self.ledger
                        )
                        if estimate.tokens + frame.max_output_tokens > window:
                            prepared = PreparedRequest(
                                snapshot, None, estimate, "无法压缩：保留内容已超过请求窗口"
                            )
                        else:
                            prepared = PreparedRequest(snapshot, request, estimate)
                else:
                    prepared = PreparedRequest(snapshot, request, estimate)
        except asyncio.CancelledError:
            cancel.set()
            prepared = PreparedRequest(self.conv.snapshot(), None, TokenEstimate(0), "已取消压缩")
        except (ValueError, OSError) as error:
            prepared = PreparedRequest(self.conv.snapshot(), None, TokenEstimate(0), str(error))
        if final_event is not None:
            yield final_event
        yield prepared

    async def compact(
        self,
        provider: Provider,
        frame: RequestFrame,
        mode: Mode,
        cancel: asyncio.Event,
        scope: RunScope,
        reason: CompactReason,
    ) -> AsyncGenerator[ContextEvent | CompactResult, None]:
        result = CompactResult("failed", "摘要未返回结果")
        try:
            async with self._lock:
                provider.context_limits.validate()
                if scope.circuit_broken:
                    raise ValueError("本轮上下文压缩已熔断")
                snapshot = await self._light(mode, cancel)
                async for item in self._compact_locked(
                    provider, frame, mode, cancel, scope, reason, snapshot
                ):
                    if isinstance(item, CompactResult):
                        result = item
                    else:
                        yield item
        except asyncio.CancelledError:
            cancel.set()
            result = CompactResult("cancelled", "已取消压缩")
        except (ValueError, OSError) as error:
            result = CompactResult("failed", str(error))
        yield self._result_event(result, reason)
        yield result

    @staticmethod
    def _result_event(result: CompactResult, reason: CompactReason) -> ContextEvent:
        message = result.reason
        if result.status == "success":
            message = (
                f"压缩完成：早期 {result.m} 条，保留 {result.n} 条；"
                f"上下文约 {result.before:,} → {result.after:,} token（字符估算）"
            )
        return ContextEvent(result.status, reason, message, result=result)

    async def _compact_locked(
        self,
        provider: Provider,
        frame: RequestFrame,
        mode: Mode,
        cancel: asyncio.Event,
        scope: RunScope,
        reason: CompactReason,
        snapshot: HistorySnapshot,
    ) -> AsyncGenerator[ContextEvent | CompactResult, None]:
        cut = self.select_cut(snapshot, provider)
        if cut is None:
            yield CompactResult("skipped", "当前没有满足近期保留条件的可压缩早期消息")
            return
        before = self.estimator.estimate_fresh(provider, frame.request(snapshot))
        yield ContextEvent("start", reason, "正在压缩早期对话…")
        for attempt in range(1, 4):
            if cancel.is_set():
                yield CompactResult("cancelled", "已取消压缩", attempts=attempt - 1)
                return
            request = build_summary_request(
                snapshot, cut.m, provider.context_limits.compact_max_output_tokens
            )
            outcome = await self.summary.run(provider, request, snapshot, cut, cancel, attempt)
            yield ContextEvent("usage", reason, attempt=attempt, record=outcome.record)
            if outcome.cancelled:
                yield CompactResult("cancelled", "已取消压缩", attempts=attempt)
                return
            if outcome.document is not None:
                try:
                    bundle = await self.attachments.collect(mode, cancel)
                    candidate, after = self._fit(
                        provider, frame, snapshot, cut, outcome.document, bundle, reason, before
                    )
                    if cancel.is_set():
                        raise asyncio.CancelledError
                    self.conv.replace_prefix(
                        snapshot.revision, cut.m, candidate, outcome.document.user_quotes
                    )
                    yield CompactResult("success", "压缩完成", cut.m, cut.n, before, after, attempt)
                except asyncio.CancelledError:
                    cancel.set()
                    yield CompactResult("cancelled", "已取消压缩", attempts=attempt)
                except (ValueError, OSError) as error:
                    scope.circuit_broken = True
                    yield CompactResult("failed", str(error), attempts=attempt)
                return
            failure = outcome.error or ValueError("摘要返回无效内容")
            if attempt == 3:
                scope.circuit_broken = True
                yield CompactResult("failed", "摘要连续失败三次，已熔断", attempts=attempt)
                return
            if isinstance(failure, ContextWindowExceeded):
                next_cut = self.select_cut(snapshot, provider, cut)
                if next_cut is None:
                    scope.circuit_broken = True
                    yield CompactResult(
                        "failed", "无法在保留近期下限的同时再缩减10%早期内容", attempts=attempt
                    )
                    return
                cut = next_cut
            kind = (
                "输入超限" if isinstance(failure, ContextWindowExceeded) else type(failure).__name__
            )
            yield ContextEvent(
                "retry",
                reason,
                f"摘要失败（{kind}），准备第 {attempt + 1}/3 次尝试",
                attempt=attempt + 1,
            )
            try:
                await asyncio.wait_for(cancel.wait(), RETRY_DELAYS[attempt - 1])
                yield CompactResult("cancelled", "已取消压缩", attempts=attempt)
                return
            except TimeoutError:
                pass

    def _fit(
        self,
        provider: Provider,
        frame: RequestFrame,
        snapshot: HistorySnapshot,
        cut: Cut,
        document: SummaryDocument,
        bundle: AttachmentBundle,
        reason: CompactReason,
        before: int,
    ) -> tuple[str, int]:
        window = provider.context_limits.context_window
        assert window is not None
        removable = [*reversed(bundle.files), *reversed(bundle.skills)]
        while True:
            text = render_reconstruction(document, bundle, cut.m, reason)
            msg = Message(role="user", content=text)
            entry = HistoryEntry(
                "candidate", msg, "summary", message_hash(msg), document.user_quotes
            )
            candidate = replace(
                snapshot,
                entries=(entry, *snapshot.entries[cut.m :]),
                content_epoch=snapshot.content_epoch + 1,
            )
            after = self.estimator.estimate_fresh(provider, frame.request(candidate))
            if (
                after < window - 33_000
                and after + frame.max_output_tokens <= window
                and after < before
            ):
                return text, after
            while removable and not removable[0].text:
                removable.pop(0)
            if not removable:
                raise ValueError("摘要重建后仍超预算或没有缩减，已保留原历史")
            item = removable.pop(0)
            item.text = ""
            item.omitted_reason = "因上下文预算省略内容，请按需重新读取"
