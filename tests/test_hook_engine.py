"""Hook Engine 单测：分派顺序、拦截中断、prompt 注入、only_once、async 不阻塞（ch14 T13）。"""

from __future__ import annotations

import asyncio

import pytest

from potatocode.hook import DispatchResult, Engine, Event
from potatocode.hook.event import is_blocking
from potatocode.hook.rule import (
    Action,
    ActionType,
    AtomCondition,
    CombineMode,
    Condition,
    PromptAction,
    Rule,
    ShellAction,
)


def _shell_rule(
    name: str,
    command: str,
    event: Event = Event.PRE_TOOL_USE,
    *,
    condition: Condition | None = None,
    only_once: bool = False,
    asyncio_mode: bool = False,
    timeout_s: float = 10.0,
) -> Rule:
    return Rule(
        name=name,
        event=event,
        action=Action(type=ActionType.SHELL, shell=ShellAction(command=command)),
        condition=condition,
        only_once=only_once,
        asyncio_mode=asyncio_mode,
        timeout_s=timeout_s,
    )


def _prompt_rule(name: str, text: str, event: Event = Event.SESSION_START) -> Rule:
    return Rule(
        name=name,
        event=event,
        action=Action(type=ActionType.PROMPT, prompt=PromptAction(text=text)),
    )


def _exact_condition(field: str, value: str) -> Condition:
    from potatocode.permission.matcher import ExactMatcher

    return Condition(
        mode=CombineMode.ALL_OF,
        atoms=[AtomCondition(field=field, matcher=ExactMatcher(value))],
    )


def test_dispatch_runs_rules_in_declaration_order():
    """多 rule 同事件按声明序执行（G1/F31）。"""
    engine = Engine(
        [
            _prompt_rule("first", "one"),
            _prompt_rule("second", "two"),
        ],
        ["src.yaml"],
    )
    result = asyncio.run(engine.dispatch(Event.SESSION_START, {"event": "SessionStart"}))
    assert result.injected_prompts == ["one", "two"]
    assert result.blocked is False


def test_dispatch_blocking_event_first_block_stops_later_rules():
    """拦截类事件：首个 blocked 的 rule 生效后中断后续（F32）。"""
    engine = Engine(
        [
            _shell_rule("blocker", "echo stop >&2; exit 2"),
            _prompt_rule("never", "should-not-run", Event.PRE_TOOL_USE),
        ],
        [],
    )
    result = asyncio.run(
        engine.dispatch(Event.PRE_TOOL_USE, {"event": "PreToolUse", "tool_name": "x"})
    )
    assert result.blocked is True
    assert result.reason == "stop"
    assert result.blocking_hook_name == "blocker"
    assert result.injected_prompts == []  # 后续 prompt rule 未执行


def test_dispatch_non_blocking_event_does_not_propagate_block():
    """非拦截类事件：shell exit 2 不产生 blocked（T13-3/G6）。"""
    engine = Engine([_shell_rule("two", "exit 2", Event.STOP)], [])
    result = asyncio.run(engine.dispatch(Event.STOP, {"event": "Stop"}))
    assert result.blocked is False


def test_dispatch_condition_filters_rules():
    """条件不满足的 rule 跳过（F11-F13）。"""
    engine = Engine(
        [
            _shell_rule(
                "only-write",
                "echo hit >&2; exit 2",
                condition=_exact_condition("tool_name", "write_file"),
            ),
        ],
        [],
    )
    hit = asyncio.run(
        engine.dispatch(Event.PRE_TOOL_USE, {"tool_name": "write_file"})
    )
    assert hit.blocked is True
    miss = asyncio.run(
        engine.dispatch(Event.PRE_TOOL_USE, {"tool_name": "read_file"})
    )
    assert miss.blocked is False


def test_dispatch_failure_logs_and_continues(capsys):
    """hook 失败（非拦截信号）：stderr 日志、继续后续 rule、不拦截（G9/F29）。"""
    engine = Engine(
        [
            _shell_rule("failing", "exit 1"),
            _prompt_rule("after", "still-here", Event.PRE_TOOL_USE),
        ],
        [],
    )
    result = asyncio.run(engine.dispatch(Event.PRE_TOOL_USE, {}))
    captured = capsys.readouterr()
    assert result.blocked is False
    assert result.injected_prompts == ["still-here"]
    assert '[hook failing] PreToolUse failed:' in captured.err


def test_dispatch_prompt_accumulates_across_rules():
    """prompt 注入按声明顺序累加（F20）。"""
    engine = Engine(
        [_prompt_rule("a", "A"), _prompt_rule("b", "B"), _prompt_rule("c", "C")],
        [],
    )
    result = asyncio.run(engine.dispatch(Event.SESSION_START, {}))
    assert result.injected_prompts == ["A", "B", "C"]


def test_only_once_skips_execution_second_time(tmp_path):
    """only_once 第二次 dispatch 不再执行命令（marker 文件只被创建一次）。

    Windows 上 capfd 抓不到 asyncio 子进程的 fd 输出，用文件副作用验证执行与否。
    """
    marker = tmp_path / "only-once-marker"
    command = f'touch "{marker.as_posix()}"'
    engine = Engine(
        [_shell_rule("once", command, Event.PRE_USER_MESSAGE, only_once=True)],
        [],
    )
    asyncio.run(engine.dispatch(Event.PRE_USER_MESSAGE, {}))
    assert marker.exists()  # 首次执行了
    marker.unlink()
    asyncio.run(engine.dispatch(Event.PRE_USER_MESSAGE, {}))
    assert not marker.exists()  # 第二次被 only_once 跳过


def test_reset_for_new_session_clears_once_set(tmp_path):
    """会话重置后 only_once 集合清空、hook 可重新触发（F27/AC9）。"""
    marker = tmp_path / "reset-marker"
    command = f'touch "{marker.as_posix()}"'
    engine = Engine(
        [_shell_rule("once", command, Event.PRE_USER_MESSAGE, only_once=True)],
        [],
    )
    asyncio.run(engine.dispatch(Event.PRE_USER_MESSAGE, {}))
    assert marker.exists()
    marker.unlink()
    engine.reset_for_new_session()
    assert engine._once_fired == set()  # 重置后集合清空
    asyncio.run(engine.dispatch(Event.PRE_USER_MESSAGE, {}))
    assert marker.exists()  # hook 重新执行了


def test_async_rule_starts_task_and_does_not_block():
    """async rule：起 task 后立即继续，不进入 blocked / injected_prompts（G8）。"""
    started = asyncio.Event()

    class _SignalingExecutor:
        """替换 Executor：记录 run 被调用并置位事件。"""

        def __init__(self) -> None:
            self.call_count = 0

        async def run(self, rule: Rule, payload, *, blocking: bool):
            self.call_count += 1
            started.set()
            from potatocode.hook.executor import ExecutionResult

            return ExecutionResult()

    engine = Engine(
        [_shell_rule("bg", "echo bg", Event.POST_TOOL_USE, asyncio_mode=True)],
        [],
    )
    engine._executor = _SignalingExecutor()  # type: ignore[assignment]

    async def _scenario() -> DispatchResult:
        result = await engine.dispatch(Event.POST_TOOL_USE, {})
        # dispatch 返回时 task 已被创建；给事件循环一个调度点让其执行
        await asyncio.sleep(0.01)
        return result

    result = asyncio.run(_scenario())
    assert result.blocked is False
    assert result.injected_prompts == []
    assert engine._executor.call_count == 1  # type: ignore[attr-defined]
    assert started.is_set()


def test_async_blocking_event_combination_is_rejected_at_load_layer():
    """拦截类 + async 的组合在 Loader 已拒绝；引擎侧仅做行为兜底验证（G8）。"""
    # 该校验属 loader（见 test_hook_loader），此处验证 is_blocking 判定边界
    assert is_blocking(Event.PRE_TOOL_USE)
    assert is_blocking(Event.USER_PROMPT_SUBMIT)
    assert not is_blocking(Event.STOP)
    assert not is_blocking(Event.SESSION_START)


def test_cancelled_error_propagates_from_dispatch():
    """CancelledError 从 dispatch 传播，不吞掉（N2）。"""
    from potatocode.hook.executor import ExecutionResult

    class _HangingExecutor:
        async def run(self, rule: Rule, payload, *, blocking: bool):
            await asyncio.sleep(10)
            return ExecutionResult()

    engine = Engine([_shell_rule("slow", "sleep 10")], [])
    engine._executor = _HangingExecutor()  # type: ignore[assignment]

    async def _cancelled_dispatch():
        task = asyncio.create_task(engine.dispatch(Event.PRE_TOOL_USE, {}))
        await asyncio.sleep(0.05)
        task.cancel()
        await task  # 应抛 CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_cancelled_dispatch())


def test_rules_and_sources_properties_return_copies():
    """rules / sources property 返回副本（T10-5）。"""
    engine = Engine([_prompt_rule("a", "x")], ["f.yaml"])
    engine.rules.clear()
    engine.sources.clear()
    assert len(engine.rules) == 1
    assert engine.sources == ["f.yaml"]
