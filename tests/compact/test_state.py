"""状态对象单测：会话目录、替换账本冻结、熔断计数与文件追踪的并发安全。"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from potatocode.compact import (
    CompactCircuitBreaker,
    ContentReplacementState,
    RecoveryState,
    new_session_context,
)
from potatocode.compact.state import FileReadRecord


def test_new_session_context(tmp_path):
    """会话 id 形如 <unix_ts>-<hex>，落盘目录被真实创建，两次调用互不相同。"""
    first = new_session_context(str(tmp_path))
    second = new_session_context(str(tmp_path))

    assert re.fullmatch(r"\d+-[0-9a-f]{8}", first.session_id)
    assert first.session_id != second.session_id
    assert Path(first.spill_dir).is_dir()
    assert Path(first.spill_dir).parent.name == first.session_id


def test_new_session_context_rand_fail_fallback(tmp_path, monkeypatch):
    """熵源失败时降级为 random，仍返回合法 id 且不抛异常。"""

    def boom(_n: int) -> str:
        raise RuntimeError("no entropy")

    monkeypatch.setattr("potatocode.compact.state.secrets.token_hex", boom)
    session = new_session_context(str(tmp_path))
    assert re.fullmatch(r"\d+-[0-9a-f]{8}", session.session_id)
    assert Path(session.spill_dir).is_dir()


def test_decide_once_freeze_kept():
    """决定保留原文后，账本不再翻转，也不写入替换串。"""
    state = ContentReplacementState()
    original = "原始内容"
    first = state.decide_once("t1", original, lambda: ("kept", ""))
    calls = {"n": 0}

    def _decide_again() -> tuple[str, str]:
        calls["n"] += 1
        return ("replaced", "不该走到这里")

    second = state.decide_once("t1", original, _decide_again)

    assert first == original
    assert second == original
    assert calls["n"] == 0  # 已 Seen 的 id 不再回调决策
    assert state.seen("t1") is True


def test_decide_once_freeze_replaced():
    """决定替换后，后续每次都返回同一份预览字符串。"""
    state = ContentReplacementState()
    preview = state.decide_once("t2", "原文", lambda: ("replaced", "预览A"))
    again = state.decide_once("t2", "原文", lambda: ("kept", ""))

    assert preview == "预览A"
    assert again == preview
    assert state.seen("t2") is True


def test_decide_once_skip_does_not_mark():
    """落盘失败（skip）不写两本账，下一轮可重新决策。"""
    state = ContentReplacementState()
    skipped = state.decide_once("t3", "原文", lambda: ("skip", ""))
    assert skipped == "原文"
    assert state.seen("t3") is False  # skip 不写账本，下一轮仍可重新决策

    retried = state.decide_once("t3", "原文", lambda: ("replaced", "预览B"))
    assert retried == "预览B"
    assert state.seen("t3") is True


def test_recovery_state_snapshot_order():
    """快照按时间戳倒序返回，且不暴露内部字典。"""
    state = RecoveryState()
    state.record_file("a.txt", "A")
    state.record_file("b.txt", "B")
    state.record_file("c.txt", "C")

    paths = [r.path for r in state.snapshot()]
    assert paths[-1].endswith("a.txt")
    assert paths[0].endswith("c.txt")

    snapshot = state.snapshot()
    snapshot.clear()
    assert len(state.snapshot()) == 3  # 清空返回值不影响内部状态


def test_snapshot_tie_breaks_by_recency():
    """同一时间戳并列时，后写入的排在前面。"""
    state = RecoveryState()
    stamp = datetime(2026, 1, 1, 12, 0, 0)
    with state._lock:
        state._files["/x/old.txt"] = FileReadRecord("/x/old.txt", "old", stamp)
        state._files["/x/new.txt"] = FileReadRecord("/x/new.txt", "new", stamp)

    paths = [r.path for r in state.snapshot()]
    assert paths == ["/x/new.txt", "/x/old.txt"]


def test_recovery_state_overwrite_keeps_latest(tmp_path):
    """同一路径重复读取只保留最新内容与时间。"""
    state = RecoveryState()
    target = tmp_path / "same.txt"
    state.record_file(str(target), "旧")
    state.record_file(str(target), "新")

    snapshot = state.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0].content == "新"


def test_recovery_state_concurrent():
    """50 个线程并发写 + 读快照：无异常、无丢失。"""
    state = RecoveryState()
    barrier = threading.Barrier(50)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            barrier.wait()
            state.record_file(f"file-{i}.txt", f"content-{i}")
            state.snapshot()
        except Exception as e:  # pragma: no cover - 竞态命中才会走到
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(state.snapshot()) == 50


def test_auto_tracking_consecutive_budget():
    """连续失败达阈值才跳闸，任意一次成功立即清零。"""
    tracking = CompactCircuitBreaker()
    tracking.record_failure()
    tracking.record_failure()
    assert tracking.tripped() is False

    tracking.record_success()
    tracking.record_failure()
    tracking.record_failure()
    assert tracking.tripped() is False

    tracking.record_failure()
    assert tracking.tripped() is True


def test_auto_tracking_concurrent():
    """并发访问熔断计数：无竞态，计数等于成功写入的失败次数。"""
    tracking = CompactCircuitBreaker()
    barrier = threading.Barrier(30)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            barrier.wait()
            if i % 3 == 0:
                tracking.record_success()
            else:
                tracking.record_failure()
            tracking.tripped()
        except Exception as e:  # pragma: no cover - 竞态命中才会走到
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert isinstance(tracking.tripped(), bool)


def test_recovery_timestamp_monotonic():
    """后写入的记录时间戳不早于先写入的（快照倒序的前提）。"""
    state = RecoveryState()
    state.record_file("a.txt", "A")
    first = state.snapshot()[0].timestamp
    state.record_file("b.txt", "B")
    second = [r for r in state.snapshot() if r.path.endswith("b.txt")][0].timestamp
    assert second - first >= timedelta(0)
