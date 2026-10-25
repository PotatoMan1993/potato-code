"""后台任务通知与子 Agent 审批升级的 TUI 消费端（ch15 F19/F12-③）。

以 mixin 形式混入 PotatoCodeApp（与 StreamMixin 同模式）：

- `_consume_task_done`：消费 Manager 的 done 队列，把任务终态渲染成
  `<task-notification>` 块推进主对话 reminder 队列——只对模型可见、不打断
  当前对话、不占用工具调用配额（N7），主 Agent 下次 run 自然消费；
- `_consume_subagent_approvals`：消费 Manager 的审批升级队列，串行弹出
  「来自 SubAgent」的审批菜单，用户裁决后子 Agent 继续跑动（G6/F13）。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..agent import ApprovalRequest
from ..task.manager import BackgroundTask, Status
from .view import SessionState

if TYPE_CHECKING:
    from ..agent.runtime import SessionRuntime
    from ..task.manager import Manager


def build_task_notification(task: BackgroundTask) -> str:
    """把任务终态渲染为 `<task-notification>` 块（F19）。"""
    if task.status is Status.FAILED and task.err is not None:
        result = f"（执行失败）{type(task.err).__name__}: {task.err}"
    elif task.status is Status.CANCELLED:
        result = task.result or "（已被取消）"
    else:
        result = task.result or "（无文本结果）"
    return (
        "<task-notification>\n"
        f'Task {task.id} (name="{task.name or "-"}"): {task.status}\n'
        f"Result: {result}\n"
        "</task-notification>"
    )


class TasksMixin:
    """后台任务相关协程的消费与弹窗串行化。"""

    # —— 以下属性由 PotatoCodeApp 提供（仅类型声明，无运行时赋值）——
    task_mgr: Manager | None
    state: SessionState
    pending: ApprovalRequest | None
    approve_cursor: int
    approval_menu_lock: asyncio.Lock
    _approval_prev_state: SessionState

    if TYPE_CHECKING:
        runtime: SessionRuntime

        def _refresh_streaming_view(self) -> None: ...

    async def _consume_task_done(self) -> None:
        """消费任务完成通知：`<task-notification>` 注入主对话 reminder 队列（F19/AC12）。

        不主动唤醒主对话——等主 Agent 下次 run 自然取出（plan 模块 D）；
        不写 RichLog——通知只对模型可见（N7）。
        """
        assert self.task_mgr is not None
        done_queue = self.task_mgr.subscribe_done()
        while True:
            task_id = await done_queue.get()
            task = self.task_mgr.get(task_id)
            if task is None:
                continue
            self.runtime.append_reminders([build_task_notification(task)])

    async def _consume_subagent_approvals(self) -> None:
        """消费子 Agent 审批升级请求：串行弹菜单、回传 Outcome（F12-③/F13/AC8）。

        与主 Agent 审批共用 `approval_menu_lock`，保证同一时刻最多一个菜单；
        等待期间用户可随时 Esc/Ctrl+C 拒绝（走 _cancel_approval 同一路径）。
        """
        assert self.task_mgr is not None
        approval_queue = self.task_mgr.open_approval_queue()
        while True:
            req = await approval_queue.get()
            if req.respond.done():
                continue  # 等待期间已被取消/拒绝：跳过过期请求
            state_before = self.state
            async with self.approval_menu_lock:
                if req.respond.done():
                    continue
                self._approval_prev_state = state_before
                self.pending = req
                self.approve_cursor = 0
                self.state = SessionState.APPROVING
                self._refresh_streaming_view()
                try:
                    await req.respond
                except asyncio.CancelledError:
                    raise  # App 退出
                finally:
                    self.pending = None
                    self.state = self._approval_prev_state
                self._refresh_streaming_view()


__all__ = ["TasksMixin", "build_task_notification"]
