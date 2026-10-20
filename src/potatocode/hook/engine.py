"""Hook 引擎：事件分派主流程、only_once 集合与会话重置（ch14 G1/F27/F31）。

dispatch 按加载声明顺序串行执行命中事件的规则；async hook 起 asyncio task 后
立即继续（不参与拦截与注入）；拦截类事件下首个表达拦截信号的同步 rule 生效后
跳过后续同事件 rule。only_once 集合的读写都在 asyncio 单线程事件循环内完成，
无需额外加锁（与 SessionRuntime 同约定）。
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field

from .event import Event, is_blocking
from .executor import Executor
from .matcher import eval_condition
from .rule import Payload, Rule


@dataclass
class DispatchResult:
    """一次事件分派的结果：拦截判定 + 待注入 prompt 集合（ch14 F31）。"""

    blocked: bool = False
    reason: str = ""
    blocking_hook_name: str = ""
    injected_prompts: list[str] = field(default_factory=list)


class Engine:
    """持有全部已加载规则与 only_once 状态，对 agent / tui 暴露唯一分派入口。"""

    def __init__(self, rules: list[Rule], sources: list[str]) -> None:
        self._rules = rules  # 按加载（yaml 声明）顺序
        self._sources = sources  # 加载来源文件列表，供 /hooks 显示
        self._once_fired: set[str] = set()  # only_once 已触发的 hook name（ch14 F27）
        self._executor = Executor()

    @property
    def rules(self) -> list[Rule]:
        """已加载规则的副本列表（/hooks 命令查询用）。"""
        return list(self._rules)

    @property
    def sources(self) -> list[str]:
        """加载来源文件路径列表（/hooks 命令展示用）。"""
        return list(self._sources)

    async def dispatch(self, event: Event, payload: Payload) -> DispatchResult:
        """把一个生命周期事件分派给全部命中规则，返回拦截判定与注入集合。

        失败的 hook 只记 stderr 日志、继续后续 rule，不中断 Agent 主流程
        （ch14 G9/F29）；CancelledError 原样传播（ch14 N2）。
        """
        result = DispatchResult()
        for rule in self._rules:
            if rule.event is not event:
                continue
            if rule.only_once and rule.name in self._once_fired:
                continue  # only_once 已触发：直接跳过（ch14 F27）
            if not eval_condition(rule.condition, payload):
                continue

            if rule.asyncio_mode:
                # async hook：起 task 后立即继续，不参与 blocked / injected_prompts（ch14 G8）
                asyncio.create_task(self._executor.run(rule, payload, blocking=False))
                self._mark_once_if_needed(rule)
                continue

            outcome = await self._executor.run(rule, payload, blocking=is_blocking(event))
            if outcome.err is not None:
                print(
                    f"[hook {rule.name}] {event.value} failed: {outcome.err}",
                    file=sys.stderr,
                )
                self._mark_once_if_needed(rule)  # 执行过即算消耗一次 only_once
                continue
            if outcome.prompt:
                result.injected_prompts.append(outcome.prompt)
            self._mark_once_if_needed(rule)
            if outcome.blocked and is_blocking(event):
                result.blocked = True
                result.reason = outcome.reason
                result.blocking_hook_name = rule.name
                break  # 首个拦截生效后跳过后续同事件 rule（ch14 F32）
        return result

    def reset_for_new_session(self) -> None:
        """清空 only_once 集合（/clear、/resume 切换会话时调用，ch14 F27/N5）。"""
        self._once_fired.clear()

    def _mark_once_if_needed(self, rule: Rule) -> None:
        """only_once 规则执行过（无论成败）后记入已触发集合（ch14 F27）。"""
        if rule.only_once:
            self._once_fired.add(rule.name)


__all__ = ["DispatchResult", "Engine"]
