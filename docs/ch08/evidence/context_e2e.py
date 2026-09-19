"""运行可复现的本地端到端场景，复用验收断言，不读取密钥或访问真实 API。"""

from __future__ import annotations

import asyncio
import inspect
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))

from test_context_agent import (  # noqa: E402
    test_emergency_replays_request_once_without_repeating_tool,
    test_light_spills_result_before_tool_followup_request,
    test_second_overflow_stops_without_recursive_summary,
    test_usage_trigger_runs_summary_before_next_tool_iteration,
)
from test_context_protocol_e2e import (  # noqa: E402
    test_sdk_multi_tool_auto_compact_and_continue,
    test_sdk_textual_manual_cancel_failure_then_chat,
)
from test_context_tui import (  # noqa: E402
    test_compacting_blocks_submit_and_mode_change_escape_cancels_then_can_chat,
    test_manual_compact_keeps_mode_and_recent_text_without_empty_assistant,
    test_manual_three_failures_then_fresh_operation_succeeds,
    test_missing_window_reports_in_ui_and_later_configured_request_works,
)

SCENARIOS = [
    ("轻量存盘后工具续调", test_light_spills_result_before_tool_followup_request),
    ("工具执行后自动压缩并继续", test_usage_trigger_runs_summary_before_next_tool_iteration),
    (
        "实际超限后紧急压缩，工具不重复执行",
        test_emergency_replays_request_once_without_repeating_tool,
    ),
    ("再次超限停止，不递归摘要", test_second_overflow_stops_without_recursive_summary),
    (
        "Textual 手动压缩，模式和近期原文保留",
        test_manual_compact_keeps_mode_and_recent_text_without_empty_assistant,
    ),
    (
        "Textual 静默流取消后再次输入",
        test_compacting_blocks_submit_and_mode_change_escape_cancels_then_can_chat,
    ),
    ("Textual 三次失败熔断后新操作恢复", test_manual_three_failures_then_fresh_operation_succeeds),
    (
        "Textual 缺窗口阻止请求，补充配置后恢复",
        test_missing_window_reports_in_ui_and_later_configured_request_works,
    ),
]


async def main() -> None:
    cases = [(title, scenario, {}) for title, scenario in SCENARIOS]
    for protocol in ("anthropic", "openai"):
        cases.extend(
            [
                (
                    f"{protocol} SDK：多轮工具→自动摘要→继续",
                    test_sdk_multi_tool_auto_compact_and_continue,
                    {"protocol": protocol},
                ),
                (
                    f"{protocol} SDK + Textual：手动→取消/失败→再输入",
                    test_sdk_textual_manual_cancel_failure_then_chat,
                    {"protocol": protocol},
                ),
            ]
        )
    lines = [
        "# ch08 本地端到端执行结果",
        "",
        "受控 provider、真实 SDK + 内存 HTTP、Agent/文件/权限引擎/Textual App.run_test；"
        "无真实 API 请求。",
        "",
    ]
    for title, scenario, extra in cases:
        with tempfile.TemporaryDirectory(prefix="potatocode-context-") as directory:
            with pytest.MonkeyPatch.context() as monkeypatch:
                values = {"tmp_path": Path(directory), "monkeypatch": monkeypatch, **extra}
                await scenario(
                    **{name: values[name] for name in inspect.signature(scenario).parameters}
                )
        lines.append(f"- PASS：{title}")
        print(f"PASS: {title}")
    lines.extend(["", f"结果：{len(cases)}/{len(cases)} 通过。", ""])
    Path(__file__).with_name("context-e2e-results.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )


if __name__ == "__main__":
    asyncio.run(main())
