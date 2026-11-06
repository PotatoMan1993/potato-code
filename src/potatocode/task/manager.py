"""后台任务管理：SubAgent 的生命周期、状态快照与完成通知（ch15 F14-F21）。

Manager 持有全部后台子 Agent 任务：launch 启动、adopt_running 接管前台超时
任务、stop 取消、send_message 给已完成任务续派、subscribe_done 供 TUI 消费
`<task-notification>`。所有协程在单事件循环内运行；子 Agent 异常一律转
status=failed 回灌，绝不影响主程序（N3）。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING

from ..agent import ApprovalRequest, Event, Phase
from ..agent import Usage as AgentUsage
from ..permission import Outcome

if TYPE_CHECKING:
    from ..agent import Agent
    from ..conversation import Conversation

logger = logging.getLogger(__name__)

# done 通知队列容量：正常场景远用不满；真满则丢弃通知并 stderr 警告（漏一条不致命）
_DONE_QUEUE_SIZE = 32
# 事件转发队列容量：run_to_completion 的进度事件经此聚合；满则丢弃（进度类事件）
_EVENTS_QUEUE_SIZE = 64


class Status(IntEnum):
    """后台任务生命周期状态（F15）。"""

    RUNNING = 0
    COMPLETED = 1
    FAILED = 2
    CANCELLED = 3

    def __str__(self) -> str:
        return {
            Status.RUNNING: "running",
            Status.COMPLETED: "completed",
            Status.FAILED: "failed",
            Status.CANCELLED: "cancelled",
        }.get(self, "unknown")


class TaskError(Exception):
    """后台任务操作失败基类。"""


class TaskNotFound(TaskError):
    """按 id / name 找不到任务。"""


class TaskBusy(TaskError):
    """任务不处于可续派状态（仅 completed 可 SendMessage，F20）。"""


@dataclass
class PartialState:
    """前台 → 后台移交时已收集的中间状态（F17-②，adopt_running 时并入任务快照）。"""

    tool_count: int = 0  # 前台阶段已完成的工具调用次数
    last_activity: str = ""  # 前台阶段最近一次工具名
    usage: AgentUsage = field(default_factory=AgentUsage)  # 前台阶段累计 token 用量


@dataclass
class BackgroundTask:
    """一个后台子 Agent 的完整状态快照（F15）。"""

    id: str  # manager 生成，形如 "task_<8 位十六进制>"
    name: str  # Agent 工具的 name 参数（可空；SendMessage 按此寻址）
    sub_agent: Agent  # 子 Agent 实例（send_message 续派时复用）
    conv: Conversation  # 子对话（续派时在其上追加新 user 消息）
    task: str  # 初始任务文本（续派不更新此字段）
    status: Status = Status.RUNNING
    result: str = ""  # 跑完的最终文本（completed 时有效）
    err: BaseException | None = None  # failed 时的异常
    start_time: float = field(default_factory=time.monotonic)
    end_time: float = 0.0
    handle: asyncio.Task | None = None  # 跑动协程（run_to_completion 的 Task）；stop 时 cancel
    usage: AgentUsage = field(default_factory=AgentUsage)  # 累计 token 计数（F15）
    tool_count: int = 0  # 工具调用次数计数器
    last_activity: str = ""  # 最近一次工具名


class Manager:
    """后台任务管理器：注册、取消、续派与完成通知（协程安全，单事件循环）。"""

    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._by_name: dict[str, str] = {}  # name -> id 弱引用，后启动覆盖（F1）
        self._done: asyncio.Queue[str] = asyncio.Queue(maxsize=_DONE_QUEUE_SIZE)
        self._monitors: set[asyncio.Task] = set()  # 持引用防监视协程被回收
        self._approval_q: asyncio.Queue[ApprovalRequest] | None = None  # TUI 消费时创建

    # ────────────────────────── 查询 ──────────────────────────

    def get(self, task_id: str) -> BackgroundTask | None:
        """按 id 取任务快照；未命中返回 None。"""
        return self._tasks.get(task_id)

    def list(self) -> list[BackgroundTask]:
        """全部任务，按 start_time 升序（TaskList 数据源）。"""
        return sorted(self._tasks.values(), key=lambda t: t.start_time)

    def subscribe_done(self) -> asyncio.Queue[str]:
        """返回完成通知队列：任务进入终态时 push task_id（F16/F19）。"""
        return self._done

    def open_approval_queue(self) -> asyncio.Queue[ApprovalRequest]:
        """创建（或复用）子 Agent 审批升级队列；TUI 启动消费协程后子 Agent 的
        Ask 才会真正升级到主 TUI，未创建时 upgrade_approval 直接拒绝回退（F13）。"""
        if self._approval_q is None:
            self._approval_q = asyncio.Queue(maxsize=16)
        return self._approval_q

    def __iter__(self) -> Iterator[BackgroundTask]:
        return iter(self.list())

    def __len__(self) -> int:
        return len(self._tasks)

    # ────────────────────────── 生命周期 ──────────────────────────

    async def launch(self, agent: Agent, conv: Conversation, name: str, task_text: str) -> str:
        """后台启动一个子 Agent（F16）：起协程跑 run_to_completion，终态推 done 队列。

        返回 task_id；调用方（Agent 工具）通常已处在子 Agent 深度上下文中，
        create_task 会把深度标记一并继承给跑动协程（嵌套阻断随之生效）。
        """
        task_id = self._next_id()
        background_task = BackgroundTask(
            id=task_id, name=name, sub_agent=agent, conv=conv, task=task_text
        )
        self._register(background_task)
        events: asyncio.Queue = asyncio.Queue(maxsize=_EVENTS_QUEUE_SIZE)
        run_handle = asyncio.create_task(agent.run_to_completion(conv, task_text, events))
        self._start(background_task, run_handle, events)
        return task_id

    async def adopt_running(
        self,
        agent: Agent,
        conv: Conversation,
        name: str,
        events: asyncio.Queue,
        handle: asyncio.Task,
        partial: PartialState,
    ) -> str:
        """接管一个已在前台跑动的子 Agent（F17-②：超时自动切后台）。

        不重启 run_to_completion（前台协程仍在跑），只注册任务快照、并入前台
        聚合的部分状态、换 Manager 的聚合协程继续消费事件流、等终态推 done。
        """
        task_id = self._next_id()
        background_task = BackgroundTask(
            id=task_id,
            name=name,
            sub_agent=agent,
            conv=conv,
            task="",
            usage=partial.usage,
            tool_count=partial.tool_count,
            last_activity=partial.last_activity,
        )
        self._register(background_task)
        self._start(background_task, handle, events)
        return task_id

    async def stop(self, task_id: str) -> bool:
        """触发任务取消（F20）：cancel 跑动协程；终态（cancelled）由监视协程写入。

        返回是否找到任务；重复 stop / 已终态的任务是幂等空操作。
        """
        background_task = self._tasks.get(task_id)
        if background_task is None:
            return False
        if background_task.handle is not None and not background_task.handle.done():
            background_task.handle.cancel()
        return True

    async def send_message(self, name: str, message: str) -> str:
        """给已完成（completed）的后台任务续派新任务（F20/AC15）。

        新 user 消息追加到原子对话（历史保留，子 Agent 带着前次结果继续），
        同 id 复用、status 重置回 running（F20/plan 技术决策）。
        找不到 name 抛 `TaskNotFound`；非 completed（running/failed/cancelled）抛 `TaskBusy`。
        """
        task_id = self._by_name.get(name)
        if task_id is None:
            raise TaskNotFound(f"不存在名为 {name!r} 的后台任务")
        background_task = self._tasks[task_id]
        if background_task.status is not Status.COMPLETED:
            raise TaskBusy(f"任务 {task_id} 当前状态为 {background_task.status}，无法续派")

        # 续派一轮跑动也发生在某个（主或子）Agent 的工具调用里：标记子 Agent 深度，
        # 防止被续派的子 Agent 再经 Agent 工具嵌套启动（F24 与 launch 同一防线）
        from ..agent.subagent_context import SUBAGENT_DEPTH

        token = SUBAGENT_DEPTH.set(SUBAGENT_DEPTH.get() + 1)
        try:
            background_task.conv.add_user(message)
            background_task.status = Status.RUNNING
            background_task.result = ""
            background_task.err = None
            background_task.start_time = time.monotonic()
            background_task.end_time = 0.0
            events: asyncio.Queue = asyncio.Queue(maxsize=_EVENTS_QUEUE_SIZE)
            run_handle = asyncio.create_task(
                background_task.sub_agent.run_to_completion(background_task.conv, "", events)
            )
            self._start(background_task, run_handle, events)
        finally:
            SUBAGENT_DEPTH.reset(token)
        return task_id

    # ────────────────────────── 审批升级（F13） ──────────────────────────

    async def upgrade_approval(self, req: ApprovalRequest) -> tuple[Outcome, bool]:
        """子 Agent 审批升级入口（F12-③）：把请求推给主 TUI 并等待用户裁决。

        队列未创建（TUI 未消费）或已满时返回 (DENY_ONCE, False)——调用方回退到
        默认 emit 路径，run_to_completion 自动拒绝，绝不悬死（N3）。
        """
        if self._approval_q is None:
            return Outcome.DENY_ONCE, False
        try:
            self._approval_q.put_nowait(req)
        except asyncio.QueueFull:
            logger.warning("子 Agent 审批队列已满，本次 Ask 回退为拒绝")
            return Outcome.DENY_ONCE, False
        outcome = await req.respond  # 子 Agent 在此暂停，TUI 弹窗后回传 Outcome（G6）
        return outcome, True

    # ────────────────────────── 内部 ──────────────────────────

    def _register(self, background_task: BackgroundTask) -> None:
        """登记任务快照；带 name 时按「后启动覆盖」写入弱引用表（F1）。"""
        self._tasks[background_task.id] = background_task
        if background_task.name:
            self._by_name[background_task.name] = background_task.id

    def _next_id(self) -> str:
        """生成不冲突的任务 id：task_ + 8 位十六进制随机串（F15）。"""
        while True:
            task_id = f"task_{secrets.token_hex(4)}"
            if task_id not in self._tasks:
                return task_id

    def _start(
        self, background_task: BackgroundTask, handle: asyncio.Task, events: asyncio.Queue
    ) -> None:
        """挂上跑动协程、起事件聚合与终态监视两个伴随协程（F16）。"""
        background_task.handle = handle
        aggregator = asyncio.create_task(self._aggregate_events(events, background_task))
        monitor = asyncio.create_task(self._await_terminal(background_task, aggregator))
        self._monitors.add(monitor)
        monitor.add_done_callback(self._monitors.discard)

    async def _aggregate_events(
        self, events: asyncio.Queue, background_task: BackgroundTask
    ) -> None:
        """消费子 Agent 事件流：工具次数 / 最近活动 / token 用量聚合进任务快照（F15）。"""
        try:
            while True:
                event: Event = await events.get()
                if event.tool is not None and event.tool.phase is Phase.START:
                    background_task.tool_count += 1
                    background_task.last_activity = event.tool.name
                if event.usage is not None:
                    background_task.usage.input += event.usage.input
                    background_task.usage.output += event.usage.output
                    background_task.usage.cache_write += event.usage.cache_write
                    background_task.usage.cache_read += event.usage.cache_read
        except asyncio.CancelledError:
            logger.warning(f"任务 {background_task.id} 的事件消费任务被取消")
            pass  # 终态监视协程在收尾时取消本聚合

    async def _await_terminal(
        self, background_task: BackgroundTask, aggregator: asyncio.Task
    ) -> None:
        """等跑动协程终态：写状态与结果，推 done 队列（F16/N3）。

        任何异常（含 MaxTurnsReached）都转 status=failed + 错误信息，主程序不崩（N3/AC20）。
        """
        handle = background_task.handle
        assert handle is not None  # _start 前已挂上
        try:
            text = await handle
            background_task.result = text
            background_task.status = Status.COMPLETED
            logger.info(f"任务 {background_task.id} 执行完成")
        except asyncio.CancelledError:
            background_task.status = Status.CANCELLED
            logger.info(f"任务 {background_task.id} 被取消")
        except BaseException as error:  # noqa: BLE001 - 子 Agent 任何异常都不外泄（N3）
            background_task.status = Status.FAILED
            background_task.err = error
            logger.info(f"任务 {background_task.id} 执行失败：{error}")
        finally:
            background_task.end_time = time.monotonic()
            aggregator.cancel()
            self._push_done(background_task.id)

    def _push_done(self, task_id: str) -> None:
        """终态通知入队；缓冲满则丢弃并 stderr 警告（漏一条通知不致命）。"""
        try:
            self._done.put_nowait(task_id)
        except asyncio.QueueFull:
            logger.error(f"task manager: done queue full, dropping notification for {task_id}")


__all__ = [
    "BackgroundTask",
    "Manager",
    "PartialState",
    "Status",
    "TaskBusy",
    "TaskError",
    "TaskNotFound",
]
