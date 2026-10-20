"""agent：ReAct 循环编排（想 → 调工具 → 看结果 → 边做边调整，直到任务完成）。

只依赖 llm / tool / conversation / prompt，不 import anthropic/openai，保持协议无关。
对外以 async generator 吐出统一 `Event` 流，供 TUI 渲染（F3）。

停止条件（F2）：自然完成 / 迭代上限 / 用户取消 / 连续未知工具 / 流出错。
任何提前终止后对话历史保持合法（F6）：工具调用与结果配对、角色交替不被破坏。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import cast

from .. import prompt
from ..compact import (
    AUTO_SAFETY_MARGIN,
    MANUAL_SAFETY_MARGIN,
    SUMMARY_RESERVE,
    ManageInput,
    ManageOutput,
    TriggerKind,
    estimate_tokens,
    manage_context,
    usage_anchor,
)
from ..conversation import Conversation
from ..llm import (
    ROLE_ASSISTANT,
    ROLE_USER,
    Message,
    PromptTooLongError,
    Provider,
    Request,
    System,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from ..llm import Usage as LlmUsage
from ..memory import Manager as MemoryManager
from ..permission import Decision, Mode, Outcome
from ..permission.engine import Engine
from ..tool import DEFAULT_TIMEOUT, Registry
from .event import CompactEvent, CompactPhase
from .runtime import SessionRuntime, new_runtime

logger = logging.getLogger(__name__)

# 工具行 / 结果摘要里展示的参数预览最大长度
_ARGS_PREVIEW_LIMIT = 80

# 需要记录文件追踪的工具名（供压缩后恢复段展示最近读过的文件）
READ_FILE_TOOL = "read_file"

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

# 自动记忆更新的轮次间隔（ch11 F35：每 5 轮自然结束触发一次）
MEMORY_UPDATE_INTERVAL: int = 5

# 显式记忆请求关键词：命中即额外触发一次更新，保证"记住 xxx"不被间隔漏掉（F35）
MEMORY_SIGNAL_KEYWORDS: tuple[str, ...] = ("记住", "记忆", "别忘", "remember", "memo")


class Phase(Enum):
    """工具调用阶段：START 开始执行，END 执行完毕。"""

    START = "start"
    END = "end"


@dataclass
class ApprovalRequest:
    """一次待批准的工具调用（F8）：agent 发此事件后 await respond 等用户决策。"""

    name: str  # 工具内部名（用于展示 ● name(args)）
    args: str  # 参数预览
    reason: str  # 触发 Ask 的原因（模式 + 类别）
    respond: asyncio.Future[Outcome]  # 单次未来量：TUI 回传用户选择


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
    approval: ApprovalRequest | None = None  # 待批准的工具调用（人在回路，F8）
    usage: Usage | None = None  # 本轮 token 用量（每轮 stream 结束后一次）
    iter: int = 0  # >0：进入第 iter 轮迭代（进度提示，F9）
    notice: str = ""  # 系统提示（停止原因等），仅用于 UI 展示，不入对话历史
    done: bool = False  # 本轮（整个 Loop）结束
    err: Exception | None = None  # 出错（不中断会话）
    compact: CompactEvent | None = None  # 上下文压缩生命周期事件（自动 / 紧急路径）


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


def recent_turn(msgs: list[Message]) -> list[Message]:
    """截取本轮对话：从最后一条 user 消息到末尾（供记忆更新使用）。"""
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].role == ROLE_USER:
            return msgs[i:]
    return []


def has_memory_signal(msgs: list[Message]) -> bool:
    """本轮用户消息是否包含显式记忆请求关键词（F35-②）。"""
    for msg in msgs:
        if msg.role != ROLE_USER:
            continue
        text = msg.content.lower()
        if any(kw in text for kw in MEMORY_SIGNAL_KEYWORDS):
            return True
    return False


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

    def __init__(
        self,
        provider: Provider,
        registry: Registry,
        engine: Engine,
        version: str = "",
        *,
        runtime: SessionRuntime | None = None,
        memory_manager: MemoryManager | None = None,
        instruction_text: str = "",
        memory_text: str = "",
        skill_catalog: str = "",
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._version = version
        self.engine = engine  # 权限引擎：前四层判定，实际运行由 cli/tui 注入
        # 长生命周期状态由 TUI 跨轮持有；未注入时自建一份（测试与脚本场景）
        self.runtime = runtime if runtime is not None else new_runtime()
        # 用 asyncio.Lock 保证 run 与 run_force_compact 不并发触发上下文管理
        self._run_lock = asyncio.Lock()
        # 项目指令与长期记忆：启动时加载一次，之后每轮请求组装时注入系统提示
        self._mem_mgr = memory_manager
        self._instruction_text = instruction_text
        self._memory_text = memory_text
        self._mem_tasks: set[asyncio.Task] = set()  # 后台记忆更新任务（持引用防被回收）
        # ch13 Skill 系统：Available Skills 目录文本（系统提示「可用Skill」槽）。
        # 启动期注入一次，/skill reload 或安装 skill 后由 TUI 刷新（F11）；inline
        # skill 的 SOP 经 toolResult / 注入消息进入对话，不再钉到 env 段。
        self._skill_catalog = skill_catalog

    def set_skill_catalog(self, catalog: str) -> None:
        """更新 Available Skills 目录文本（启动期注入，reload / 安装后刷新，F11）。"""
        self._skill_catalog = catalog

    async def run(
        self,
        conv: Conversation,
        mode: Mode,
        cancel: asyncio.Event,
    ) -> AsyncIterator[Event]:
        """执行 Agent Loop，以 async generator 吐出事件流。

        入口先持会话级锁：手动 `/compact` 必须等本轮 run 结束才能触发压缩，
        避免两条路径并发改写同一份对话历史。
        """
        async with self._run_lock:
            async for ev in self._run_iterations(conv, mode, cancel):
                yield ev

    async def run_force_compact(
        self,
        conv: Conversation,
        tool_defs: list[ToolDefinition] | None = None,
    ) -> tuple[int, int]:
        """手动 `/compact` 入口：跳过阈值与熔断，无条件做一次摘要。

        与 `run` 互斥；返回压缩前后的估算 token，失败时异常向上抛由调用方展示。
        """
        async with self._run_lock:
            defs = self._registry.definitions() if tool_defs is None else tool_defs
            in_ = self._manage_input(conv, defs, TriggerKind.MANUAL)
            out = await manage_context(in_)
            return out.before_tokens, out.after_tokens

    def _manage_input(
        self,
        conv: Conversation,
        defs: list[ToolDefinition],
        trigger: TriggerKind,
    ) -> ManageInput:
        """按当前锚点估算 token 并组装一次上下文管理入参。"""
        est = estimate_tokens(
            self.runtime.usage_anchor, conv.messages(), self.runtime.anchor_msg_len
        )
        return ManageInput(
            conv=conv,
            provider=self._provider,
            context_window=self.runtime.context_window,
            tool_defs=defs,
            replacement=self.runtime.replacement,
            recovery=self.runtime.recovery,
            auto_tracking=self.runtime.auto_tracking,
            session=self.runtime.session,
            usage_anchor=self.runtime.usage_anchor,
            anchor_msg_len=self.runtime.anchor_msg_len,
            estimated_token=est,
            trigger=trigger,
        )

    async def _run_iterations(
        self,
        conv: Conversation,
        mode: Mode,
        cancel: asyncio.Event,
    ) -> AsyncIterator[Event]:
        """ReAct 主循环本体：每轮先管理上下文，再请求、执行工具、回灌结果。"""
        # 每次 run 起始装配稳定系统提示（普通/规划一致；规划提醒已移出系统通道）
        sys = prompt.build_system_prompt(self._instruction_text, self._memory_text, self._skill_catalog)
        if mode == Mode.PLAN:
            defs = self._registry.read_only_definitions()
        else:
            defs = self._registry.definitions()

        unknown_run = 0
        for it in range(1, MAX_ITERATIONS + 1):
            yield Event(iter=it)
            # 环境段每轮迭代重建（F6）：git 状态等易变信息逐轮刷新
            env_text = prompt.gather_environment(self._version, self._provider.model).render()
            if cancel.is_set():
                self._finish_cancelled(conv)
                return

            # 规划模式按轮次注入 reminder：首轮完整、间隔轮完整、其余精简（F7）
            reminder = ""
            if mode == Mode.PLAN:
                full = it == 1 or (it - 1) % PLAN_REMINDER_INTERVAL == 0
                reminder = prompt.plan_reminder(full)

            # —— 请求本轮之前先管理上下文：第 1 层必跑，必要时触发第 2 层摘要 ——
            in_ = self._manage_input(conv, defs, TriggerKind.AUTO)
            will_summarize = in_.estimated_token >= self.runtime.context_window - (
                SUMMARY_RESERVE + AUTO_SAFETY_MARGIN
            )
            if will_summarize:  # 只跑第 1 层是静默操作，不发压缩事件
                yield Event(compact=CompactEvent(phase=CompactPhase.BEFORE_AUTO))
            out = ManageOutput(before_tokens=in_.estimated_token, after_tokens=in_.estimated_token)
            mc_err: Exception | None = None
            try:
                out = await manage_context(in_)
            except Exception as e:
                mc_err = e
            if will_summarize:
                yield Event(
                    compact=CompactEvent(
                        phase=CompactPhase.AFTER_AUTO,
                        before=out.before_tokens,
                        after=out.after_tokens,
                        err=mc_err if mc_err else out.executed_error,
                    )
                )
            if mc_err is not None:
                _ensure_assistant_tail(conv, NOTICE_STREAM_ERR)
                yield Event(err=mc_err)
                return

            # —— 请求本轮：转发文本增量，收集完整响应（含工具调用与用量）——
            emergency_retried = False
            while True:  # 撞墙重试环路：正常路径只跑一次
                outcome = _StreamOutcome()
                async for item in self._stream_once(conv, defs, sys, env_text, reminder, cancel):
                    if isinstance(item, _StreamOutcome):
                        outcome = item
                    else:
                        yield item
                logger.debug(f"Response text: {outcome.text}")
                logger.debug(f"Response calls: {outcome.calls}")
                logger.debug(f"Response ok: {outcome.ok}")
                logger.debug(f"Response err: {outcome.err}")
                logger.debug(f"Response usage: {outcome.usage}")
                if outcome.usage is not None:
                    # 主对话路径用真实用量「替换」锚点（不是累加）；摘要请求不走这里
                    self.runtime.usage_anchor = usage_anchor(outcome.usage)
                    self.runtime.anchor_msg_len = conv.length()

                if outcome.ok or cancel.is_set():
                    break

                err = outcome.err
                if isinstance(err, PromptTooLongError) and not emergency_retried:
                    # 撞上下文墙：先做一次紧急压缩，再用新消息列表重试本请求一次
                    yield Event(compact=CompactEvent(phase=CompactPhase.BEFORE_EMERGENCY))
                    emg = self._manage_input(conv, defs, TriggerKind.EMERGENCY)
                    emg_out = ManageOutput(before_tokens=0, after_tokens=0)
                    emg_err: Exception | None = None
                    try:
                        emg_out = await manage_context(emg)
                    except Exception as e:
                        emg_err = e
                    yield Event(
                        compact=CompactEvent(
                            phase=CompactPhase.AFTER_EMERGENCY,
                            before=emg_out.before_tokens,
                            after=emg_out.after_tokens,
                            err=emg_err if emg_err else out.executed_error,
                        )
                    )
                    if emg_err is not None:
                        _ensure_assistant_tail(conv, NOTICE_STREAM_ERR)
                        yield Event(err=emg_err)
                        return
                    # 会话已被重建：锚点清零后用新消息序列重新估算
                    self.runtime.usage_anchor = 0
                    self.runtime.anchor_msg_len = 0
                    est_after = estimate_tokens(0, conv.messages(), 0)
                    if est_after >= self.runtime.context_window - MANUAL_SAFETY_MARGIN:
                        # 压完还塞不下：视为不可恢复，不再做第二次紧急压缩
                        _ensure_assistant_tail(conv, NOTICE_STREAM_ERR)
                        yield Event(err=err)
                        return
                    emergency_retried = True
                    continue

                break  # 非 PTL 或已重试过：走下面的统一错误收尾

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
                self._maybe_update_memory(conv)  # 每 5 轮 / 显式请求时异步提炼记忆
                yield Event(done=True)
                return

            # —— 有工具调用：回合入历史 → 统计未知 → 保序分批执行 → 结果回灌 ——
            conv.add_assistant_with_tool_calls(outcome.text, outcome.calls)
            unknown_run = unknown_run + 1 if self._all_unknown(outcome.calls) else 0

            results: list[ToolResult] = []
            completed = True
            try:
                async for batch_item in self._execute_batched(outcome.calls, mode, cancel):
                    if isinstance(batch_item, _BatchOutcome):  # 该批次所有工具的执行结果
                        results = batch_item.results
                        completed = batch_item.completed
                    else:  # 单个工具的执行结果 Event（含 ApprovalRequest）
                        yield batch_item
            except asyncio.CancelledError:
                # 人在回路等待中被 task.cancel()：补齐「已取消」结果 + assistant 尾巴，
                # 保持历史配对合法（N4），generator 终结即本轮结束。
                results = [
                    ToolResult(tool_call_id=c.id, content=NOTICE_CANCELLED, is_error=True)
                    for c in outcome.calls
                ]
                conv.add_tool_results(results)
                self._finish_cancelled(conv)
                raise

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
        logger.debug(f"Request system.stabel: {req.system.stable}")
        logger.debug(f"Request system.environment: {req.system.environment}")
        logger.debug(f"Request messages: {req.messages}")
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
        mode: Mode,
        cancel: asyncio.Event,
    ) -> AsyncIterator[Event | _BatchOutcome]:
        """保序分批执行（F5/F6）：连续只读合并为并发批，有副作用单个串行，保持调用序。

        每个工具执行前先过权限引擎（前四层）：Allow 执行、Deny 产被拒结果、
        Ask 发 ApprovalRequest 事件并 await 用户决策（第五层，F8）。
        事件时序：每批先按序发 PHASE_START → 执行 → 再按序发 PHASE_END。
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
                # 向前吃连续只读的最长区间 [i, j)；只读批逐个 check，Deny 预置、Allow 并发
                j = i
                while j < n and self._registry.is_read_only(calls[j].name):
                    j += 1

                allow_indices: list[int] = []
                for k in range(i, j):  # 按模型输出的工具顺序显示工具执行预览
                    yield Event(
                        tool=ToolEvent(
                            name=calls[k].name,
                            args=_preview(calls[k].input),
                            phase=Phase.START,
                        )
                    )
                    decision, reason = self.engine.check(mode, calls[k], True)
                    if decision is Decision.DENY:
                        results[k] = ToolResult(
                            tool_call_id=calls[k].id, content=reason, is_error=True
                        )
                    else:
                        allow_indices.append(k)  # 只读永不 Ask，此处必为 Allow

                completed = await self._run_batch(calls, allow_indices, results, cancel)

                for k in range(i, j):  # 按模型输出的工具顺序显示工具执行结果
                    r = results[k]
                    assert r is not None  # 每个下标都被 Deny 预置或 _run_batch 填充
                    yield Event(
                        tool=ToolEvent(
                            name=calls[k].name,
                            args=_preview(calls[k].input),
                            phase=Phase.END,
                            result=r.content,
                            is_error=r.is_error,
                        )
                    )
            else:
                # 有副作用：单独串行
                j = i + 1
                k = i
                yield Event(
                    tool=ToolEvent(
                        name=calls[k].name,
                        args=_preview(calls[k].input),
                        phase=Phase.START,
                    )
                )
                decision, reason = self.engine.check(mode, calls[k], False)

                if decision is Decision.ALLOW:
                    completed = await self._run_batch(calls, [k], results, cancel)
                elif decision is Decision.DENY:
                    results[k] = ToolResult(tool_call_id=calls[k].id, content=reason, is_error=True)
                    completed = True
                else:
                    # 第五层人在回路（F8）：发事件 → await 用户决策
                    respond: asyncio.Future[Outcome] = asyncio.get_running_loop().create_future()
                    yield Event(
                        approval=ApprovalRequest(
                            name=calls[k].name,
                            args=_preview(calls[k].input),
                            reason=reason,
                            respond=respond,
                        )
                    )
                    outcome = await respond
                    if outcome is Outcome.ALLOW_FOREVER:
                        try:
                            self.engine.persist_local_allow(calls[k])
                        except Exception as e:  # 持久化失败不阻断执行
                            logger.warning("写入本地规则失败: %s", e)
                        completed = await self._run_batch(calls, [k], results, cancel)
                    elif outcome is Outcome.ALLOW_ONCE:
                        completed = await self._run_batch(calls, [k], results, cancel)
                    else:  # DENY_ONCE
                        results[k] = ToolResult(
                            tool_call_id=calls[k].id, content=reason, is_error=True
                        )
                        completed = True

                r = results[k]
                assert r is not None
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
        indices: list[int],
        results: list[ToolResult | None],
        cancel: asyncio.Event,
    ) -> bool:
        """并发执行 indices 指定的调用（长度 1 退化为串行）。

        每个 task 只写自己下标 results[k]（asyncio 单线程模型下无需锁，N6）；
        每个工具仍受 per-tool 超时约束（N1，沿用 Registry.execute）。
        cancel 触发时尽快取消未完成的工具任务并填「已取消」结果。
        返回该批是否全部完成（False 表示执行中被取消）。
        """
        if not indices:
            return True  # 全部被 Deny，无并发任务
        tasks: dict[asyncio.Task[ToolResult], int] = {
            asyncio.create_task(self._run_tool(calls[k])): k for k in indices
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
            for k in indices:
                if results[k] is None:
                    results[k] = ToolResult(
                        tool_call_id=calls[k].id, content=NOTICE_CANCELLED, is_error=True
                    )
            return False
        return True

    async def _run_tool(self, call: ToolCall) -> ToolResult:
        """执行单个工具调用：受 per-tool 超时约束，恒返回结构化结果（N1）。"""
        r = await self._registry.execute(call.name, call.input, timeout=DEFAULT_TIMEOUT)
        await self._track_file_read(call, r.is_error)
        return ToolResult(tool_call_id=call.id, content=r.content, is_error=r.is_error)

    async def _track_file_read(self, call: ToolCall, is_error: bool) -> None:
        """read_file 成功后用磁盘纯净字节记一次文件追踪（供压缩后恢复段使用）。

        工具结果带行号前缀，直接拿来当恢复段会让模型把行号当成代码，因此重读一次磁盘；
        本方法在工具 worker task 内、结果回灌会话之前 await 完成，保证下一轮
        `manage_context` 能看到本轮记录。
        """
        if is_error or call.name != READ_FILE_TOOL:
            return
        try:
            args = json.loads(call.input) if call.input else {}
        except ValueError:  # 参数不是合法 JSON：跳过追踪，不影响工具结果回灌
            return
        path = args.get("path") if isinstance(args, dict) else None
        if not isinstance(path, str) or not path:
            return
        try:
            abs_path = str(Path(path).resolve())
            data = await asyncio.to_thread(Path(abs_path).read_bytes)
        except OSError as e:  # 读盘失败只影响恢复段素材，不打断对话
            logger.warning("记录文件追踪失败: %s: %s", path, e)
            return
        self.runtime.recovery.record_file(abs_path, data.decode("utf-8", errors="replace"))

    def _current_memory(self) -> str:
        """取当前长期记忆索引文本。

        挂了记忆管理器就现读磁盘——上一轮异步更新写完笔记后，下一轮请求立刻
        能看到新索引（F32）；没有管理器时退回启动时注入的静态文本。
        """
        if self._mem_mgr is None:
            return self._memory_text
        try:
            return self._mem_mgr.load_index()
        except Exception:  # 索引读取失败降级为静态文本，不打断对话
            logger.exception("读取记忆索引失败，沿用启动时的索引")
            return self._memory_text

    def _maybe_update_memory(self, conv: Conversation) -> None:
        """按触发条件异步发起一次记忆更新（F35/F36）。

        触发条件是「或」关系：每 5 轮到了，或本轮用户说了"记住 xxx"。
        更新跑在独立 task 里，不阻塞用户的下一次输入（F36）。
        """
        self.runtime.turn_count += 1
        mgr = self._mem_mgr
        if mgr is None or mgr.provider is None:
            return
        recent = recent_turn(conv.messages())
        if not recent:
            return
        due = self.runtime.turn_count % MEMORY_UPDATE_INTERVAL == 0
        if not due and not has_memory_signal(recent):
            return
        try:
            task = asyncio.create_task(mgr.update_async(recent))
        except RuntimeError:  # 没有运行中的事件循环（同步调用场景）：跳过本次更新
            logger.debug("无事件循环，跳过记忆更新")
            return
        self._mem_tasks.add(task)
        task.add_done_callback(self._mem_tasks.discard)

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
    "MEMORY_SIGNAL_KEYWORDS",
    "MEMORY_UPDATE_INTERVAL",
    "NOTICE_CANCELLED",
    "NOTICE_MAX_ITER",
    "NOTICE_STREAM_ERR",
    "NOTICE_UNKNOWN_TOOLS",
    "PLAN_REMINDER_INTERVAL",
    "READ_FILE_TOOL",
    "Agent",
    "ApprovalRequest",
    "CompactEvent",
    "CompactPhase",
    "Event",
    "Mode",
    "Phase",
    "SessionRuntime",
    "ToolEvent",
    "Usage",
    "has_memory_signal",
    "new_runtime",
    "recent_turn",
]
