"""task 内置 4 工具单测（ch15 T24）：TaskList / TaskGet / TaskStop / SendMessage。"""

from __future__ import annotations

import asyncio
import json

from potatocode.task import Manager, Status
from potatocode.task.tools import SendMessageTool, TaskGetTool, TaskListTool, TaskStopTool

from agent_fakes import ScriptAgent


async def _wait_done(manager: Manager, timeout: float = 5.0) -> str:
    return await asyncio.wait_for(manager.subscribe_done().get(), timeout)


async def test_task_list_returns_all_tasks() -> None:
    manager = Manager()
    running = ScriptAgent(sleep_seconds=30)
    done_agent = ScriptAgent(scripts=["ok"])
    running_id = await manager.launch(running, running.new_conv(), "runner", "长任务")
    done_id = await manager.launch(done_agent, done_agent.new_conv(), "finisher", "快任务")
    await _wait_done(manager)

    result = await TaskListTool(manager).execute("")
    payload = json.loads(result.content)
    ids = {item["id"] for item in payload}
    assert ids == {running_id, done_id}
    by_id = {item["id"]: item for item in payload}
    assert by_id[running_id]["status"] == "running"
    assert by_id[done_id]["status"] == "completed"
    for field in ("id", "name", "status", "tool_count", "last_activity"):
        assert field in payload[0]
    await manager.stop(running_id)  # 收尾清理


async def test_task_get_known_and_unknown() -> None:
    manager = Manager()
    agent = ScriptAgent(scripts=["最终文本"])
    task_id = await manager.launch(agent, agent.new_conv(), "w", "任务")
    await _wait_done(manager)

    got = await TaskGetTool(manager).execute(json.dumps({"task_id": task_id}))
    assert got.is_error is False
    payload = json.loads(got.content)
    assert payload["result"] == "最终文本"
    assert payload["task"] == "任务"
    assert payload["usage"]["input"] == 0  # 无事件时 usage 为零值

    missing = await TaskGetTool(manager).execute(json.dumps({"task_id": "task_nope"}))
    assert missing.is_error
    assert "未知任务" in missing.content

    bad_args = await TaskGetTool(manager).execute("not-json")
    assert bad_args.is_error


async def test_task_stop_requests_cancellation() -> None:
    manager = Manager()
    agent = ScriptAgent(sleep_seconds=30)
    task_id = await manager.launch(agent, agent.new_conv(), "", "长任务")

    result = await TaskStopTool(manager).execute(json.dumps({"task_id": task_id}))
    assert result.is_error is False
    assert json.loads(result.content)["status"] == "cancellation_requested"

    await _wait_done(manager)
    assert manager.get(task_id).status is Status.CANCELLED  # type: ignore[union-attr]

    missing = await TaskStopTool(manager).execute(json.dumps({"task_id": "task_nope"}))
    assert missing.is_error


async def test_send_message_tool_resumes() -> None:
    manager = Manager()
    agent = ScriptAgent(scripts=["第一轮", "第二轮"])
    await manager.launch(agent, agent.new_conv(), "worker", "任务")
    await _wait_done(manager)

    result = await SendMessageTool(manager).execute(
        json.dumps({"name": "worker", "message": "下一轮"})
    )
    assert result.is_error is False
    payload = json.loads(result.content)
    assert payload["status"] == "resumed"
    await _wait_done(manager)
    task = manager.get(payload["task_id"])
    assert task is not None
    assert task.result == "第二轮"


async def test_send_message_tool_errors() -> None:
    manager = Manager()
    running = ScriptAgent(sleep_seconds=30)
    running_id = await manager.launch(running, running.new_conv(), "busy", "长任务")

    unknown = await SendMessageTool(manager).execute(
        json.dumps({"name": "ghost", "message": "hi"})
    )
    assert unknown.is_error

    busy = await SendMessageTool(manager).execute(
        json.dumps({"name": "busy", "message": "hi"})
    )
    assert busy.is_error

    missing_args = await SendMessageTool(manager).execute(json.dumps({"name": "busy"}))
    assert missing_args.is_error
    await manager.stop(running_id)  # 收尾清理


async def test_task_list_read_only_and_stop_not() -> None:
    """TaskList / TaskGet 只读；TaskStop / SendMessage 有副作用（权限分类用）。"""
    manager = Manager()
    assert TaskListTool(manager).read_only() is True
    assert TaskGetTool(manager).read_only() is True
    assert TaskStopTool(manager).read_only() is False
    assert SendMessageTool(manager).read_only() is False
