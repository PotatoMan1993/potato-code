"""预算锚点只绑定请求输入，历史变更与请求包装均影响估算。"""

from dataclasses import replace

from context_helpers import ContextProvider
from potatocode.context.estimate import TokenEstimator, character_cost
from potatocode.context.ledger import UsageLedger
from potatocode.context.types import TokenEstimate
from potatocode.conversation import Conversation, ToolResultPatch
from potatocode.llm import Request, System, ToolCall, ToolDefinition, ToolResult, Usage


def anchor_request(provider, conv, *, tokens=800, output=9000):
    estimator, ledger = TokenEstimator(), UsageLedger()
    snapshot = conv.snapshot()
    request = Request(messages=snapshot.messages(), system=System("固定"))
    cost = estimator.estimate_fresh(provider, request)
    request_id = ledger.begin(
        provider, request, snapshot, 0, len(snapshot.entries), 1, TokenEstimate(cost), cost
    )
    ledger.observe(request_id, Usage(tokens, output))
    ledger.finish(request_id, "success", "stop")
    return estimator, ledger, request, cost


def test_character_rule_counts_unicode_characters_not_utf8_bytes():
    assert character_cost("") == 0
    assert character_cost("abcde") == 2
    assert character_cost("abcd中文🙂") == 7


def test_anchor_uses_visible_increment_and_changed_request_parts():
    provider, conv = ContextProvider(), Conversation()
    conv.add_user("最初要求")
    estimator, ledger, old_request, old_cost = anchor_request(provider, conv)
    conv.add_assistant("正文")
    conv.add_user("接着做")
    request = replace(
        old_request,
        messages=conv.messages(),
        system=System("新固定", "环境"),
        reminder="当前轮提醒",
        tools=[ToolDefinition("read", "读文件", {"type": "object"})],
    )
    expected = 800 + estimator.estimate_fresh(provider, request) - old_cost
    result = estimator.estimate_request(provider, request, conv.snapshot(), ledger)
    assert result.tokens == expected
    assert result.tokens < 9000  # 不把 API 计费输出直接加入可见历史
    assert result.source == "usage_anchored"
    assert result.anchor_request_id == 1


def test_tool_replacement_invalidates_usage_anchor():
    provider, conv = ContextProvider(), Conversation()
    conv.add_user("读取")
    conv.add_assistant_with_tool_calls("", [ToolCall("c", "read_file", "{}")])
    conv.add_tool_results([ToolResult("c", "旧结果" * 100)])
    estimator, ledger, request, _ = anchor_request(provider, conv)
    snapshot = conv.snapshot()
    conv.replace_tool_results(
        snapshot.revision,
        [ToolResultPatch(snapshot.entries[-1].id, 0, ToolResult("c", "已保存，预览"))],
    )
    request.messages = conv.messages()
    estimate = estimator.estimate_request(provider, request, conv.snapshot(), ledger)
    assert estimate.source == "character"
    assert estimate.tokens == estimator.estimate_fresh(provider, request)


def test_provider_and_session_changes_cannot_reuse_old_anchor():
    provider, conv = ContextProvider(), Conversation()
    conv.add_user("内容")
    estimator, ledger, request, _ = anchor_request(provider, conv)
    other_provider = ContextProvider()
    assert (
        estimator.estimate_request(other_provider, request, conv.snapshot(), ledger).source
        == "character"
    )
    other_conv = Conversation()
    other_conv.add_user("内容")
    assert (
        estimator.estimate_request(provider, request, other_conv.snapshot(), ledger).source
        == "character"
    )
    provider.model = "different-model"
    assert (
        estimator.estimate_request(provider, request, conv.snapshot(), ledger).source == "character"
    )


def test_request_fixed_parts_do_not_inflate_recent_suffix_cost():
    provider, conv = ContextProvider(), Conversation()
    conv.add_user("正文")
    conv.add_assistant("答复")
    estimator = TokenEstimator()
    prefix = estimator.history_prefix_costs(provider, conv.snapshot())
    assert prefix[0] == 0 and prefix[2] > prefix[1] > 0
    request = Request(
        messages=conv.messages(), system=System("系统" * 20000), reminder="提醒" * 20000
    )
    assert estimator.estimate_fresh(provider, request) > prefix[-1] + 100000
    assert estimator.history_prefix_costs(provider, conv.snapshot()) == prefix


def test_negative_anchor_delta_falls_back_to_character_estimate():
    provider, conv = ContextProvider(), Conversation()
    conv.add_user("正文")
    estimator, ledger, request, _ = anchor_request(provider, conv, tokens=0)
    request.system = System()
    result = estimator.estimate_request(provider, request, conv.snapshot(), ledger)
    assert result.source == "character"
