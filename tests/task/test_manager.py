"""task.Manager 单测（ch15 T22）：launch / stop / send_message / adopt_running 全生命周期。"""

from __future__ import annotations

import asyncio

import pytest

from potatocode.task import Manager, Status, TaskBusy, TaskNotFound
from potatocode.task.manager import PartialState

from agent_fakes import ScriptAgent


async def _wait_done(manager: Manager, timeout: float = 5.0) -> str:
    return await asyncio.wait_for(manager.subscribe_done().get(), timeout)


async def test_launch_completes_with_result() -> None:
    """launch：协程跑完写 COMPLETED + result，done 队列收到 id（F16）。"""
    manager = Manager()
    agent = ScriptAgent(scripts=["第一次结果"])
    conv = agent.new_conv()
    task_id = await manager.launch(agent, conv, "worker", "做任务")
    done_id = await _wait_done(manager)
    assert done_id == task_id
    task = manager.get(task_id)
    assert task is not None
    assert task.status is Status.COMPLETED
    assert task.result == "第一次结果"
    assert task.end_time >= task.start_time


async def test_launch_failure_becomes_failed_status() -> None:
    """子 Agent 抛异常：status=FAILED + err 非空，主程序不崩（N3/AC20）。"""
    manager = Manager()
    agent = ScriptAgent(raise_on_run=RuntimeError("子 Agent 炸了"))
    task_id = await manager.launch(agent, agent.new_conv(), "", "任务")
    await _wait_done(manager)
    task = manager.get(task_id)
    assert task is not None
    assert task.status is Status.FAILED
    assert isinstance(task.err, RuntimeError)


async def test_stop_cancels_running_task() -> None:
    """stop：触发取消，终态 CANCELLED（F20/AC14）。"""
    manager = Manager()
    agent = ScriptAgent(sleep_seconds=30)
    task_id = await manager.launch(agent, agent.new_conv(), "", "长任务")
    assert await manager.stop(task_id) is True
    await _wait_done(manager)
    task = manager.get(task_id)
    assert task is not None
    assert task.status is Status.CANCELLED


async def test_stop_unknown_id_returns_false() -> None:
    manager = Manager()
    assert await manager.stop("task_nope") is False


async def test_send_message_resumes_completed_task() -> None:
    """send_message：completed 任务同 id 续跑，新 user 消息追加到原子对话（F20/AC15）。"""
    manager = Manager()
    agent = ScriptAgent(scripts=["第一轮结果", "第二轮结果"])
    conv = agent.new_conv()
    task_id = await manager.launch(agent, conv, "worker", "任务一")
    await _wait_done(manager)

    resumed_id = await manager.send_message("worker", "任务二")
    assert resumed_id == task_id  # 同 id 复用
    assert "任务二" in conv.messages()[-1].content
    await _wait_done(manager)
    task = manager.get(task_id)
    assert task is not None
    assert task.status is Status.COMPLETED
    assert task.result == "第二轮结果"


async def test_send_message_errors() -> None:
    """找不到 name 抛 TaskNotFound；running 中抛 TaskBusy（F20）。"""
    manager = Manager()
    with pytest.raises(TaskNotFound):
        await manager.send_message("other", "hi")

    busy_agent = ScriptAgent(sleep_seconds=30)
    busy_id = await manager.launch(busy_agent, busy_agent.new_conv(), "busy", "长任务")
    with pytest.raises(TaskBusy):
        await manager.send_message("busy", "hi")
    await manager.stop(busy_id)  # 收尾清理


async def test_by_name_last_write_wins() -> None:
    """同名任务后启动覆盖前：SendMessage 寻址到最新（F1）。"""
    manager = Manager()
    first = ScriptAgent(sleep_seconds=30)
    second = ScriptAgent(scripts=["done"])
    id1 = await manager.launch(first, first.new_conv(), "worker", "一")
    id2 = await manager.launch(second, second.new_conv(), "worker", "二")
    # 等第二个任务完成（否则它是 running，SendMessage 会拒绝）
    for _ in range(200):
        task = manager.get(id2)
        if task is not None and task.status is Status.COMPLETED:
            break
        await asyncio.sleep(0.02)
    assert await manager.send_message("worker", "继续") == id2
    assert manager.get(id1) is not None
    await _wait_done(manager)
    await manager.stop(id1)  # 收尾清理


async def test_adopt_running_carries_partial_state() -> None:
    """adopt_running：接管前台任务，PartialState 并入快照，终态正常通知（F17-②）。"""
    manager = Manager()
    agent = ScriptAgent(scripts=["前台跑到一半后完成"])
    conv = agent.new_conv()
    events: asyncio.Queue = asyncio.Queue()
    handle = asyncio.create_task(agent.run_to_completion(conv, "", events))
    task_id = await manager.adopt_running(
        agent, conv, "adopted", events, handle, PartialState(tool_count=3, last_activity="bash")
    )
    await _wait_done(manager)
    task = manager.get(task_id)
    assert task is not None
    assert task.status is Status.COMPLETED
    assert task.result == "前台跑到一半后完成"
    assert task.tool_count == 3  # 前台聚合计数被并入
    assert task.last_activity == "bash"


async def test_aggregates_tool_count_and_usage() -> None:
    """launch 路径的事件聚合：工具次数 / 最近活动 / usage 累加（F15）。"""
    manager = Manager()
    agent = ScriptAgent(scripts=["聚合完成"], emit_events=True)
    task_id = await manager.launch(agent, agent.new_conv(), "", "任务")
    await _wait_done(manager)
    task = manager.get(task_id)
    assert task is not None
    assert task.tool_count == 2
    assert task.last_activity == "grep"
    assert task.usage.input == 100
    assert task.usage.output == 10
    assert task.usage.cache_read == 5


async def test_list_sorted_by_start_time() -> None:
    """list 按 start_time 升序（F14）。"""
    manager = Manager()
    ids: list[str] = []
    for index in range(3):
        agent = ScriptAgent(sleep_seconds=30)
        ids.append(await manager.launch(agent, agent.new_conv(), f"w{index}", "任务"))
    tasks = manager.list()
    assert [t.id for t in tasks] == ids  # dict 注册序即 start_time 升序
    assert all(t.status is Status.RUNNING for t in tasks)
    for task_id in ids:  # 收尾清理
        await manager.stop(task_id)
