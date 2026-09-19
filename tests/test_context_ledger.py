"""每次 API 观测独立可查；流 usage 是累计快照而非增量费用。"""

import pytest

from context_helpers import ContextProvider
from potatocode.context.ledger import UsageLedger
from potatocode.context.types import TokenEstimate
from potatocode.conversation import Conversation
from potatocode.llm import Request, System, Usage


def begin(ledger, provider, conv, purpose="normal", attempt=1):
    snapshot = conv.snapshot()
    return ledger.begin(
        provider,
        Request(messages=conv.messages(), purpose=purpose),
        snapshot,
        0,
        len(snapshot.entries),
        attempt,
        TokenEstimate(50),
    )


def test_cumulative_usage_replaces_known_counts_and_keeps_missing_input():
    ledger, provider, conv = UsageLedger(), ContextProvider(), Conversation()
    conv.add_user("用户要求")
    request_id = begin(ledger, provider, conv)
    ledger.observe(request_id, Usage(60, 1, 20, 30, {"input_tokens": 10, "output_tokens": 1}))
    ledger.observe(request_id, Usage(None, 4, raw_counts={"output_tokens": 4}))
    record = ledger.finish(request_id, "success", "stop")
    assert (record.input_tokens, record.output_tokens) == (60, 4)
    assert (record.usage.cache_write, record.usage.cache_read) == (20, 30)
    assert record.raw_counts == {"input_tokens": 10, "output_tokens": 4}
    assert ledger.anchor.input_tokens == 60
    assert record.boundary_ids == (conv.snapshot().entries[0].id,) * 2


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_incomplete_request_keeps_usage_without_creating_anchor(status):
    ledger, provider, conv = UsageLedger(), ContextProvider(), Conversation()
    conv.add_user("要求")
    request_id = begin(ledger, provider, conv)
    ledger.observe(request_id, Usage(100, 2))
    record = ledger.finish(request_id, status)
    assert record.status == status and record.input_tokens == 100
    assert ledger.anchor is None


def test_summary_success_never_replaces_normal_input_anchor():
    ledger, provider, conv = UsageLedger(), ContextProvider(), Conversation()
    conv.add_user("要求")
    normal_id = begin(ledger, provider, conv)
    ledger.observe(normal_id, Usage(90, 5))
    ledger.finish(normal_id, "success", "stop")
    summary_id = begin(ledger, provider, conv, "summary", 2)
    ledger.observe(summary_id, Usage(20000, 300))
    ledger.finish(summary_id, "success", "stop")
    assert ledger.anchor.request_id == normal_id
    assert ledger.get(summary_id).attempt == 2
    assert ledger.get(summary_id).purpose == "summary"
    assert [r.input_tokens for r in ledger.records()] == [90, 20000]


def test_unknown_usage_remains_unknown_and_finish_is_idempotent():
    ledger, provider, conv = UsageLedger(), ContextProvider(), Conversation()
    request_id = begin(ledger, provider, conv)
    first = ledger.finish(request_id, "success", "stop")
    assert first.input_tokens is None and first.output_tokens is None and first.raw_counts is None
    assert ledger.anchor is None
    assert ledger.finish(request_id, "failed") == first
    ledger.observe(request_id, Usage(900, 900))
    assert ledger.get(request_id) == first
    first.status = "failed"
    assert ledger.get(request_id).status == "success"


def test_invalid_history_boundary_does_not_create_record():
    ledger, provider, conv = UsageLedger(), ContextProvider(), Conversation()
    with pytest.raises(ValueError, match="边界"):
        ledger.begin(provider, Request(), conv.snapshot(), 0, 1, 1, TokenEstimate(0))
    assert ledger.records() == ()


def test_snapshot_fingerprint_includes_request_system_and_tools():
    ledger, provider, conv = UsageLedger(), ContextProvider(), Conversation()
    conv.add_user("相同历史")
    first = begin(ledger, provider, conv)
    snapshot = conv.snapshot()
    second = ledger.begin(
        provider,
        Request(messages=conv.messages(), system=System("新系统")),
        snapshot,
        0,
        1,
        1,
        TokenEstimate(100),
    )
    assert ledger.get(first).input_snapshot_hash != ledger.get(second).input_snapshot_hash
