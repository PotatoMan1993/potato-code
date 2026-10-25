"""后台任务操控工具：TaskList / TaskGet / TaskStop / SendMessage（ch15 F20）。

四个工具都只持 Manager 引用，经统一 Tool 协议注册进 Registry；对子 Agent 不可
见——后台路径被 ASYNC_AGENT_ALLOWED_TOOLS 白名单剔除、前台定义式路径不在
其定义白名单（F26/F28，嵌套操控被工具过滤防线阻断）。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from ..tool import Result
from .manager import Manager, Status, TaskBusy, TaskNotFound


def _parse_object(args: str, hint: str) -> dict[str, Any] | None:
    """把工具入参解析为 dict；非法 JSON / 非对象返回 None（is_error 用）。"""
    try:
        data = json.loads(args) if args and args.strip() else {}
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _task_brief(task) -> dict[str, Any]:
    """TaskList 行：id / name / status / tool_count / last_activity（F20）。"""
    return {
        "id": task.id,
        "name": task.name,
        "status": str(task.status),
        "tool_count": task.tool_count,
        "last_activity": task.last_activity,
    }


def _task_full(task) -> dict[str, Any]:
    """TaskGet 全字段：简要字段 + task / result / err / usage / 时间（F20）。"""
    return {
        **_task_brief(task),
        "task": task.task,
        "result": task.result,
        "err": None if task.err is None else f"{type(task.err).__name__}: {task.err}",
        "usage": asdict(task.usage),
        "start_time": task.start_time,
        "end_time": task.end_time,
    }


class TaskListTool:
    """列出当前全部后台任务的简要状态（F20）。"""

    def __init__(self, manager: Manager) -> None:
        self._manager = manager

    def name(self) -> str:
        return "TaskList"

    def description(self) -> str:
        return "列出当前所有后台任务（id、name、status、tool_count、last_activity）。"

    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def read_only(self) -> bool:
        return True  # 只读查询：default 模式不弹审批

    async def execute(self, args: str) -> Result:
        tasks = [_task_brief(task) for task in self._manager.list()]
        return Result(content=json.dumps(tasks, ensure_ascii=False))


class TaskGetTool:
    """返回指定任务的完整状态（含 result / err，F20）。"""

    def __init__(self, manager: Manager) -> None:
        self._manager = manager

    def name(self) -> str:
        return "TaskGet"

    def description(self) -> str:
        return "按 task_id 查询一个后台任务的完整状态（含 result / err / usage）。"

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "任务 id"}},
            "required": ["task_id"],
        }

    def read_only(self) -> bool:
        return True

    async def execute(self, args: str) -> Result:
        data = _parse_object(args, "task_id")
        if data is None:
            return Result(content='参数不是合法 JSON，需要 {"task_id": "..."}', is_error=True)
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return Result(content="缺少参数 task_id", is_error=True)
        task = self._manager.get(task_id.strip())
        if task is None:
            return Result(content=f"未知任务 id: {task_id}", is_error=True)
        return Result(content=json.dumps(_task_full(task), ensure_ascii=False))


class TaskStopTool:
    """触发后台任务取消（F20）。"""

    def __init__(self, manager: Manager) -> None:
        self._manager = manager

    def name(self) -> str:
        return "TaskStop"

    def description(self) -> str:
        return "取消一个运行中的后台任务；任务状态稍后变为 cancelled。"

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "任务 id"}},
            "required": ["task_id"],
        }

    def read_only(self) -> bool:
        return False  # 有副作用（终止子 Agent）

    async def execute(self, args: str) -> Result:
        data = _parse_object(args, "task_id")
        if data is None:
            return Result(content='参数不是合法 JSON，需要 {"task_id": "..."}', is_error=True)
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return Result(content="缺少参数 task_id", is_error=True)
        found = await self._manager.stop(task_id.strip())
        if not found:
            return Result(content=f"未知任务 id: {task_id}", is_error=True)
        return Result(
            content=json.dumps(
                {"task_id": task_id.strip(), "status": "cancellation_requested"},
                ensure_ascii=False,
            )
        )


class SendMessageTool:
    """给已完成的存活后台任务续派新任务（F20/AC15）。"""

    def __init__(self, manager: Manager) -> None:
        self._manager = manager

    def name(self) -> str:
        return "SendMessage"

    def description(self) -> str:
        return (
            "给一个已完成的后台任务（按启动时的 name 寻址）追加新任务并重新跑动；"
            "跑完结果经 task-notification 回流。仅 completed 状态可续派。"
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "启动任务时指定的 name"},
                "message": {"type": "string", "description": "追加给子 Agent 的新任务"},
            },
            "required": ["name", "message"],
        }

    def read_only(self) -> bool:
        return False  # 有副作用（重启子 Agent 跑动）

    async def execute(self, args: str) -> Result:
        data = _parse_object(args, "name/message")
        if data is None:
            return Result(
                content='参数不是合法 JSON，需要 {"name": "...", "message": "..."}',
                is_error=True,
            )
        name = data.get("name")
        message = data.get("message")
        if not isinstance(name, str) or not name.strip():
            return Result(content="缺少参数 name（任务名）", is_error=True)
        if not isinstance(message, str) or not message.strip():
            return Result(content="缺少参数 message（新任务文本）", is_error=True)
        try:
            task_id = await self._manager.send_message(name.strip(), message)
        except TaskNotFound as error:
            return Result(content=str(error), is_error=True)
        except TaskBusy as error:
            return Result(content=str(error), is_error=True)
        return Result(
            content=json.dumps({"task_id": task_id, "status": "resumed"}, ensure_ascii=False)
        )


__all__ = [
    "SendMessageTool",
    "TaskGetTool",
    "TaskListTool",
    "TaskStopTool",
    "Status",
]
