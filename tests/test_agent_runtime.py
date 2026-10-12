"""SessionRuntime 单测：跨轮长生命周期状态与 /clear 的新会话重置（ch12 T0c）。"""

from __future__ import annotations

from potatocode.agent.runtime import new_runtime
from potatocode.compact import new_session_context
from potatocode.compact.state import DECISION_REPLACED


def test_reset_for_new_session_resets_compact_state(tmp_path):
    """reset_for_new_session：compact 子状态换新、锚点/轮数清零、session 指向新上下文。"""
    runtime = new_runtime(workspace=str(tmp_path))
    runtime.context_window = 12345
    runtime.usage_anchor = 999
    runtime.anchor_msg_len = 7
    runtime.turn_count = 3
    runtime.recovery.record_file("a.py", "print(1)")
    runtime.auto_tracking.record_failure()
    runtime.replacement.decide_once("t1", "orig", lambda: (DECISION_REPLACED, "preview"))
    assert runtime.auto_tracking.tripped() is False  # 前置：单次失败尚未熔断

    new_ctx = new_session_context(str(tmp_path))
    runtime.reset_for_new_session(new_ctx)

    assert runtime.session is new_ctx
    assert runtime.usage_anchor == 0
    assert runtime.anchor_msg_len == 0
    assert runtime.turn_count == 0
    assert runtime.context_window == 12345  # 跟随 provider 的窗口保留
    assert runtime.recovery.snapshot() == []  # 文件追踪清空
    assert not runtime.replacement.seen("t1")  # 替换账本清空
    assert not runtime.auto_tracking.tripped()  # 熔断计数清零


def test_reset_for_new_session_keeps_old_context_untouched(tmp_path):
    """重置只换 runtime 的引用：旧会话上下文对象不被修改（供 /resume 等场景复用）。"""
    runtime = new_runtime(workspace=str(tmp_path))
    old_ctx = runtime.session
    new_ctx = new_session_context(str(tmp_path))
    runtime.reset_for_new_session(new_ctx)
    assert runtime.session is new_ctx
    assert old_ctx.session_id != new_ctx.session_id
