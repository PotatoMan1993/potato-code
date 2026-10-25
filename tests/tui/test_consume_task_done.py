"""task-notification 注入单测（ch15 T26）：终态渲染与 reminder 队列注入（F19/N7）。"""

from __future__ import annotations

import asyncio

from potatocode.agent.runtime import new_runtime
from potatocode.task import Manager, Status
from potatocode.tui.tasks import TasksMixin, build_task_notification

from agent_fakes import ScriptAgent


class _Harness:
    """TasksMixin 消费方法的最小宿主桩（不启动 Textual App）。"""

    _consume_task_done = TasksMixin._consume_task_done

    def __init__(self, manager: Manager) -> None:
        self.task_mgr = manager
        self.runtime = new_runtime(workspace=".")


async def test_build_task_notification_completed() -> None:
    manager = Manager()
    agent = ScriptAgent(scripts=["统计完成：42 行"])
    task_id = await manager.launch(agent, agent.new_conv(), "worker", "统计行数")
    await asyncio.wait_for(manager.subscribe_done().get(), 5)
    notification = build_task_notification(manager.get(task_id))
    assert notification.startswith("<task-notification>")
    assert notification.rstrip().endswith("</task-notification>")
    assert f'Task {task_id} (name="worker"): completed' in notification
    assert "Result: 统计完成：42 行" in notification


async def test_build_task_notification_failed_and_cancelled() -> None:
    manager = Manager()
    failed = ScriptAgent(raise_on_run=RuntimeError("崩了"))
    failed_id = await manager.launch(failed, failed.new_conv(), "", "任务")
    await asyncio.wait_for(manager.subscribe_done().get(), 5)
    assert ": failed" in build_task_notification(manager.get(failed_id))
    assert "RuntimeError: 崩了" in build_task_notification(manager.get(failed_id))

    cancelled = ScriptAgent(sleep_seconds=30)
    cancelled_id = await manager.launch(cancelled, cancelled.new_conv(), "", "任务")
    await manager.stop(cancelled_id)
    await asyncio.wait_for(manager.subscribe_done().get(), 5)
    assert ": cancelled" in build_task_notification(manager.get(cancelled_id))
    assert manager.get(cancelled_id).status is Status.CANCELLED


async def test_consume_task_done_pushes_reminders() -> None:
    """消费协程把完成通知推进 runtime.pending_reminders（F19/AC12）。"""
    manager = Manager()
    harness = _Harness(manager)
    consumer = asyncio.create_task(harness._consume_task_done())
    try:
        agent = ScriptAgent(scripts=["任务完成"])
        task_id = await manager.launch(agent, agent.new_conv(), "worker", "任务")
        # 不能在测试里消费 done 队列（消费协程要读它），直接轮询 reminder 注入结果
        for _ in range(100):
            if harness.runtime.pending_reminders:
                break
            await asyncio.sleep(0.02)
        assert len(harness.runtime.pending_reminders) == 1
        assert task_id in harness.runtime.pending_reminders[0]
        assert "<task-notification>" in harness.runtime.pending_reminders[0]
    finally:
        consumer.cancel()


async def test_consume_task_done_ignores_unknown_id() -> None:
    """done 队列里的 id 查不到任务时静默跳过，不崩。"""
    manager = Manager()
    harness = _Harness(manager)
    consumer = asyncio.create_task(harness._consume_task_done())
    try:
        await manager.subscribe_done().put("task_ghost")
        await asyncio.sleep(0.05)
        assert harness.runtime.pending_reminders == []
    finally:
        consumer.cancel()
