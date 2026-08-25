"""agent：ReAct 循环编排（想 → 调工具 → 看结果 → 边做边调整，直到任务完成）。

只依赖 llm / tool / conversation / prompt，不 import anthropic/openai，保持协议无关。
对外以 async generator 吐出统一 `Event` 流，供 TUI 渲染（F3）。

停止条件（F2）：自然完成 / 迭代上限 / 用户取消 / 连续未知工具 / 流出错。
任何提前终止后对话历史保持合法（F6）：工具调用与结果配对、角色交替不被破坏。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import cast

from .. import prompt
from ..conversation import Conversation
from ..llm import (
    ROLE_ASSISTANT,
    Provider,
    Request,
    System,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from ..llm import Usage as LlmUsage
from ..tool import DEFAULT_TIMEOUT, Registry

# 工具行 / 结果摘要里展示的参数预览最大长度
_ARGS_PREVIEW_LIMIT = 80

# 迭代上限兜底（F2-2：避免失控烧 token；内置常量，不配置化）
MAX_ITERATIONS: int = 25
# 连续「整轮只产生未知工具调用」的迭代数上限（F2-4：避免对幻觉工具空转）
MAX_UNKNOWN_RUN: int = 3
# 规划模式提醒的完整版注入间隔轮数（F7：首轮完整、每 N 轮重复完整、其余精简）
PLAN_REMINDER_INTERVAL: int = 4

# 停止/收尾提示文案——既作为 Event(notice=...) 推给 UI，也作为历史兜底文本（F6）
NOTICE_MAX_ITER = "（已达最大迭代轮数 25，自动停止；可继续发消息推进。）"
NOTICE_UNKNOWN_TOOLS = "（连续多轮只请求到未注册的工具，自动停止。）"
NOTICE_STREAM_ERR = "（请求出错，本轮已中断。）"
NOTICE_CANCELLED = "（已取消。）"

# 模型回复既无文本也无工具调用时的占位最终答复（避免空 assistant 回合）
_EMPTY_FINAL = "（本轮无回复内容。）"


class Phase(Enum):
    """工具调用阶段：START 开始执行，END 执行完毕。"""

    START = "start"
    END = "end"


class Mode(IntEnum):
    """运行模式：NORMAL 全工具；PLAN 仅只读工具 + 按轮次注入计划态提醒（F7/F10）。"""

    NORMAL = 0
    PLAN = 1


@dataclass
class ToolEvent:
    """一次工具调用的开始/结束（供 TUI 渲染工具行与结果摘要）。"""

    name: str
    args: str = ""  # 参数预览（用于 ● name(args)）
    phase: Phase = Phase.START
    result: str = ""  # phase=END：结果摘要
    is_error: bool = False  # phase=END：是否错误


@dataclass
class Usage:
    """一轮请求的 token 用量（透传 llm.Usage 的语义，含缓存命中，F4）。"""

    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0


@dataclass
class Event:
    """Agent Loop 对外事件流元素，TUI 据非默认字段分派渲染（F3）。"""

    text: str = ""  # 模型文本增量（preamble 或最终答复）
    tool: ToolEvent | None = None  # 工具调用开始/结束
    usage: Usage | None = None  # 本轮 token 用量（每轮 stream 结束后一次）
    iter: int = 0  # >0：进入第 iter 轮迭代（进度提示，F9）
    notice: str = ""  # 系统提示（停止原因等），仅用于 UI 展示，不入对话历史
    done: bool = False  # 本轮（整个 Loop）结束
    err: Exception | None = None  # 出错（不中断会话）


@dataclass
class _StreamOutcome:
    """_stream_once 的最终结果：一次请求收集到的完整响应。"""

    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    usage: LlmUsage | None = None
    ok: bool = True  # False：流出错或已取消
    err: Exception | None = None  # ok=False 且非取消：流出错的异常（由 run 收尾后上抛）


@dataclass
class _BatchOutcome:
    """_execute_batched 的最终结果：全部工具结果与是否完成。"""

    results: list[ToolResult] = field(default_factory=list)
    completed: bool = True  # False：执行中被取消


def _preview(s: str, limit: int = _ARGS_PREVIEW_LIMIT) -> str:
    """参数预览：压缩换行、截断到 limit 字符，用于工具行展示。"""
    s = s.replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


def _ensure_assistant_tail(conv: Conversation, fallback: str) -> None:
    """保证历史以 assistant 文本回合收尾（F6）。

    取消/出错/上限后角色仍交替、无悬空工具调用，下一轮请求不会被 API 拒（400）。
    """
    if conv.last_role() != ROLE_ASSISTANT:
        conv.add_assistant(fallback)


def _finalized(results: list[ToolResult | None]) -> list[ToolResult]:
    """把已全填充的结果列表转为非 None 列表（执行路径保证每个下标都被填充）。"""
    out: list[ToolResult] = []
    for r in results:
        assert r is not None  # 保序分批的每条路径都会填满所有下标
        out.append(r)
    return out


class Agent:
    """持有 provider 与注册中心，执行 ReAct 循环（F1）。

    `mode` 为 run 的每次调用入参，不写入 Agent 状态（同一 Agent 可被不同 mode 复用）；
    `cancel` 由调用方持有，触发 `cancel.set()` 即中断本轮。
    """

    def __init__(self, provider: Provider, registry: Registry, version: str = "") -> None:
        self._provider = provider
        self._registry = registry
        self._version = version

    async def run(
        self,
        conv: Conversation,
        mode: Mode,
        cancel: asyncio.Event,
    ) -> AsyncIterator[Event]:
        """执行 Agent Loop，以 async generator 吐出事件流。"""
        # 每次 run 起始采集环境、装配稳定系统提示（普通/规划一致；规划提醒已移出系统通道）
        env_text = prompt.gather_environment(self._version, self._provider.model).render()
        sys = prompt.build_system_prompt()
        if mode is Mode.PLAN:
            defs = self._registry.read_only_definitions()
        else:
            defs = self._registry.definitions()

        unknown_run = 0
        for it in range(1, MAX_ITERATIONS + 1):
            yield Event(iter=it)
            if cancel.is_set():
                self._finish_cancelled(conv)
                return

            # 规划模式按轮次注入 reminder：首轮完整、间隔轮完整、其余精简（F7）
            reminder = ""
            if mode is Mode.PLAN:
                full = it == 1 or (it - 1) % PLAN_REMINDER_INTERVAL == 0
                reminder = prompt.plan_reminder(full)

            # —— 请求本轮：转发文本增量，收集完整响应（含工具调用与用量）——
            outcome = _StreamOutcome()
            async for item in self._stream_once(conv, defs, sys, env_text, reminder, cancel):
                if isinstance(item, _StreamOutcome):
                    outcome = item
                else:
                    yield item

            if not outcome.ok:
                if cancel.is_set():
                    self._finish_cancelled(conv)
                else:
                    # 流出错：先收尾历史，再把 err 作为本轮终止事件上抛（F2-5/F6）。
                    # 顺序关键：消费者见到 err 即收尾本轮，收尾后不能再有遗留动作。
                    _ensure_assistant_tail(conv, NOTICE_STREAM_ERR)
                    if outcome.err is not None:
                        yield Event(err=outcome.err)
                return

            if outcome.usage is not None:
                yield Event(
                    usage=Usage(
                        input=outcome.usage.input_tokens,
                        output=outcome.usage.output_tokens,
                        cache_write=outcome.usage.cache_write,
                        cache_read=outcome.usage.cache_read,
                    )
                )

            if not outcome.calls:
                # 自然完成：无工具调用的纯文本即最终答复（F2-1）
                final = outcome.text
                if not final.strip():
                    final = _EMPTY_FINAL
                    yield Event(text=final)
                conv.add_assistant(final)
                yield Event(done=True)
                return

            # —— 有工具调用：回合入历史 → 统计未知 → 保序分批执行 → 结果回灌 ——
            conv.add_assistant_with_tool_calls(outcome.text, outcome.calls)
            unknown_run = unknown_run + 1 if self._all_unknown(outcome.calls) else 0

            results: list[ToolResult] = []
            completed = True
            async for batch_item in self._execute_batched(outcome.calls, cancel):
                if isinstance(batch_item, _BatchOutcome):  # 该批次所有工具的执行结果
                    results = batch_item.results
                    completed = batch_item.completed
                else:  # 单个工具的执行结果Event
                    yield batch_item

            conv.add_tool_results(results)  # 无条件回灌（含已取消占位，F6）

            if not completed:
                # 执行中被取消：最高优先级终止，跳过未知工具与迭代上限检查
                self._finish_cancelled(conv)
                return

            if unknown_run >= MAX_UNKNOWN_RUN:
                yield Event(notice=NOTICE_UNKNOWN_TOOLS)
                _ensure_assistant_tail(conv, NOTICE_UNKNOWN_TOOLS)
                yield Event(done=True)
                return

        # 循环走完：触达迭代上限（F2-2）
        yield Event(notice=NOTICE_MAX_ITER)
        _ensure_assistant_tail(conv, NOTICE_MAX_ITER)
        yield Event(done=True)

    async def _stream_once(
        self,
        conv: Conversation,
        defs: list[ToolDefinition],
        sys: str,
        env_text: str,
        reminder: str,
        cancel: asyncio.Event,
    ) -> AsyncIterator[Event | _StreamOutcome]:
        """发起一次请求：转发文本增量事件，累积完整文本与工具调用，记录本轮用量。

        组装 `llm.Request`（稳定系统提示 + 环境段 + 本轮 reminder）发起请求；
        最后 yield `_StreamOutcome`；ok=False 表示流出错或已取消
        （出错时异常随 outcome.err 带回，由 run 收尾历史后再上抛）。
        双路收集：文本增量实时推给事件流，完整响应（含拼齐 JSON 参数的工具调用）
        供循环判断（F4）。
        """
        if cancel.is_set():
            yield _StreamOutcome(ok=False)
            return

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        usage: LlmUsage | None = None
        req = Request(
            messages=conv.messages(),
            tools=defs,
            system=System(stable=sys, environment=env_text),
            reminder=reminder,
        )
        async for ev in self._provider.stream(req):
            if cancel.is_set():
                yield _StreamOutcome(ok=False)
                return
            if ev.err is not None:
                # 异常随 outcome 带回，由 run 收尾历史后再作为终止事件上抛
                yield _StreamOutcome(ok=False, err=ev.err)
                return
            if ev.usage is not None:
                usage = ev.usage
            if ev.tool_calls:
                calls.extend(ev.tool_calls)
            if ev.text:
                text_parts.append(ev.text)
                yield Event(text=ev.text)

        if cancel.is_set():
            yield _StreamOutcome(ok=False)
            return
        yield _StreamOutcome(text="".join(text_parts), calls=calls, usage=usage, ok=True)

    async def _execute_batched(
        self,
        calls: list[ToolCall],
        cancel: asyncio.Event,
    ) -> AsyncIterator[Event | _BatchOutcome]:
        """保序分批执行（F5）：连续只读合并为并发批，有副作用单个串行，保持调用序。

        事件时序：每批先按序发 PHASE_START → 执行 → 再按序发 PHASE_END，
        即「开始事件按序、结束事件按序」，并发只发生在执行环节（N3）。
        """
        n = len(calls)
        results: list[ToolResult | None] = [None] * n
        i = 0
        while i < n:
            if cancel.is_set():
                # 取消：尚未执行的调用补「已取消」结构化结果，保持历史配对合法（F6）
                for k in range(i, n):
                    results[k] = ToolResult(
                        tool_call_id=calls[k].id, content=NOTICE_CANCELLED, is_error=True
                    )
                yield _BatchOutcome(results=_finalized(results), completed=False)
                return

            if self._registry.is_read_only(calls[i].name):
                # 向前吃连续只读的最长区间 [i, j)
                j = i
                while j < n and self._registry.is_read_only(calls[j].name):
                    j += 1
            else:
                j = i + 1  # 有副作用：单独串行

            for k in range(i, j):  # 按模型输出的工具顺序显示工具执行预览
                yield Event(
                    tool=ToolEvent(
                        name=calls[k].name,
                        args=_preview(calls[k].input),
                        phase=Phase.START,
                    )
                )

            completed = await self._run_batch(calls, i, j, results, cancel)

            for k in range(i, j):  # 按照模型输出的工具顺序来显示工具执行结果
                r = results[k]
                assert r is not None  # _run_batch 保证区间内每个下标都已填充
                yield Event(
                    tool=ToolEvent(
                        name=calls[k].name,
                        args=_preview(calls[k].input),
                        phase=Phase.END,
                        result=r.content,
                        is_error=r.is_error,
                    )
                )

            if not completed:
                # 执行中被取消：未开始的后续调用补「已取消」结果
                for k in range(j, n):
                    results[k] = ToolResult(
                        tool_call_id=calls[k].id, content=NOTICE_CANCELLED, is_error=True
                    )
                yield _BatchOutcome(results=_finalized(results), completed=False)
                return

            i = j

        yield _BatchOutcome(results=_finalized(results), completed=True)

    async def _run_batch(
        self,
        calls: list[ToolCall],
        start: int,
        end: int,
        results: list[ToolResult | None],
        cancel: asyncio.Event,
    ) -> bool:
        """并发执行区间 [start, end) 内的调用（长度 1 退化为串行）。

        每个 task 只写自己下标 results[k]（asyncio 单线程模型下无需锁，N6）；
        每个工具仍受 per-tool 超时约束（N1，沿用 Registry.execute）。
        cancel 触发时尽快取消未完成的工具任务并填「已取消」结果。
        返回该批是否全部完成（False 表示执行中被取消）。
        """
        tasks: dict[asyncio.Task[ToolResult], int] = {
            asyncio.create_task(self._run_tool(calls[k])): k for k in range(start, end)
        }
        cancel_task = asyncio.create_task(cancel.wait())
        pending: set[asyncio.Task[ToolResult]] = set(tasks)  # 只跟踪工具任务
        try:
            while pending:
                done, _ = await asyncio.wait(
                    pending | {cancel_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for t in done:
                    if t is cancel_task:
                        continue
                    tool_task = cast("asyncio.Task[ToolResult]", t)  # 类型检查收窄
                    results[tasks[tool_task]] = tool_task.result()
                    pending.discard(tool_task)
                if cancel_task in done or cancel.is_set():
                    for t in pending:
                        t.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    break
        finally:
            cancel_task.cancel()  # 无泄漏：不残留监听任务（N5）

        if cancel.is_set():
            for k in range(start, end):
                if results[k] is None:
                    results[k] = ToolResult(
                        tool_call_id=calls[k].id, content=NOTICE_CANCELLED, is_error=True
                    )
            return False
        return True

    async def _run_tool(self, call: ToolCall) -> ToolResult:
        """执行单个工具调用：受 per-tool 超时约束，恒返回结构化结果（N1）。"""
        r = await self._registry.execute(call.name, call.input, timeout=DEFAULT_TIMEOUT)
        return ToolResult(tool_call_id=call.id, content=r.content, is_error=r.is_error)

    def _all_unknown(self, calls: list[ToolCall]) -> bool:
        """整轮调用是否全部未注册（混入任一已注册工具即视为有进展，计数重置）。"""
        return all(self._registry.get(c.name) is None for c in calls)

    def _finish_cancelled(self, conv: Conversation) -> None:
        """取消路径统一收尾：补 assistant 文本尾巴（F6）。

        不 yield notice——cancel 已被消费方感知，generator 终结即视为本轮结束。
        """
        _ensure_assistant_tail(conv, NOTICE_CANCELLED)


__all__ = [
    "MAX_ITERATIONS",
    "MAX_UNKNOWN_RUN",
    "NOTICE_CANCELLED",
    "NOTICE_MAX_ITER",
    "NOTICE_STREAM_ERR",
    "NOTICE_UNKNOWN_TOOLS",
    "PLAN_REMINDER_INTERVAL",
    "Agent",
    "Event",
    "Mode",
    "Phase",
    "ToolEvent",
    "Usage",
]
