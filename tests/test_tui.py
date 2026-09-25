"""TUI 单测：Shift+Tab 模式切换、状态栏模式显示、人在回路菜单按键、取消（AC9/F7/F8/N4）。"""

from __future__ import annotations

import asyncio

from rich.console import Console
from textual.widgets import RichLog

from potatocode.agent import ApprovalRequest, CompactEvent, CompactPhase, Event, new_runtime
from potatocode.config import ProviderConfig
from potatocode.permission import Mode, Outcome, new_engine
from potatocode.prompt import EXECUTE_DIRECTIVE
from potatocode.tool import new_default_registry
from potatocode.tui.app import ChatTextArea, PotatoCodeApp
from potatocode.tui.commands import (
    BUILTIN_COMMANDS,
    dispatch_command,
    format_compact_notice,
)
from potatocode.tui.view import SessionState, statusbar_content


def _cfg() -> ProviderConfig:
    return ProviderConfig(
        name="fake-provider", protocol="anthropic", api_key="sk-test", model="fake-model"
    )


def _render(r) -> str:
    """把 Rich renderable 渲染为纯文本，供断言。"""
    c = Console(width=300)
    with c.capture() as capture:
        c.print(r)
    return capture.get()


def _new_app(tmp_path, mode_hint: str | None = None) -> PotatoCodeApp:
    engine, _ = new_engine(str(tmp_path))
    # 会话目录落在 tmp_path：用例不该往工作目录写会话副产物
    runtime = new_runtime(workspace=str(tmp_path))
    return PotatoCodeApp([_cfg()], "dev", new_default_registry(), engine, runtime=runtime)


# ────────── Shift+Tab 循环切换（AC9/F7） ──────────


async def test_shift_tab_cycles_mode(tmp_path):
    app = _new_app(tmp_path)
    async with app.run_test() as pilot:
        assert app.mode is Mode.DEFAULT
        await pilot.press("shift+tab")
        assert app.mode is Mode.ACCEPT_EDITS
        await pilot.press("shift+tab")
        assert app.mode is Mode.PLAN
        await pilot.press("shift+tab")
        assert app.mode is Mode.BYPASS
        await pilot.press("shift+tab")
        assert app.mode is Mode.DEFAULT  # 四档全循环
        assert app.state is SessionState.IDLE  # 停留 idle


async def test_mode_persists_and_rules_unchanged(tmp_path):
    """切换模式只改兜底裁决，不重置模式、不改已加载规则（AC9/F7）。"""
    app = _new_app(tmp_path)
    async with app.run_test() as pilot:
        before = [r.pattern for r in app.engine.local.allow]
        await pilot.press("shift+tab")
        assert app.mode is Mode.ACCEPT_EDITS  # 跨轮保持的基础：模式是持久字段
        # 切换不改已加载规则
        assert [r.pattern for r in app.engine.local.allow] == before


# ────────── 状态栏（AC9/F7） ──────────


def test_status_bar_shows_mode_not_provider():
    """左侧显示权限模式，且不含 provider 名。"""
    assert "DEFAULT" in _render(statusbar_content(Mode.DEFAULT, "fake-model"))
    assert "ACCEPT EDITS" in _render(statusbar_content(Mode.ACCEPT_EDITS, "fake-model"))
    assert "PLAN" in _render(statusbar_content(Mode.PLAN, "fake-model"))
    assert "BYPASS" in _render(statusbar_content(Mode.BYPASS, "fake-model"))
    # 不含 provider 名（新签名根本不再接收 name）
    text = _render(statusbar_content(Mode.DEFAULT, "fake-model"))
    assert "fake-provider" not in text


# ────────── 人在回路菜单（AC10/F8） ──────────


def _pending(outcome_future: asyncio.Future) -> ApprovalRequest:
    return ApprovalRequest(
        name="write_file",
        args='{"path": "a.txt"}',
        reason="default 模式下 文件写 类操作需确认",
        respond=outcome_future,
    )


async def test_approval_menu_navigation_and_enter(tmp_path):
    app = _new_app(tmp_path)
    async with app.run_test():
        loop = asyncio.get_running_loop()
        respond = loop.create_future()
        app.pending = _pending(respond)
        app.state = SessionState.APPROVING
        app.approve_cursor = 0

        app.update_approving("down")
        assert app.approve_cursor == 1
        app.update_approving("up")
        assert app.approve_cursor == 0
        app.update_approving("down")
        app.update_approving("enter")  # 光标在 1 → ALLOW_FOREVER
        assert respond.result() is Outcome.ALLOW_FOREVER
        assert app.state is SessionState.STREAMING
        assert app.pending is None


async def test_approval_number_keys(tmp_path):
    app = _new_app(tmp_path)
    async with app.run_test():
        loop = asyncio.get_running_loop()

        r1 = loop.create_future()
        app.pending = _pending(r1)
        app.state = SessionState.APPROVING
        app.update_approving("1")
        assert r1.result() is Outcome.ALLOW_ONCE

        r3 = loop.create_future()
        app.pending = _pending(r3)
        app.state = SessionState.APPROVING
        app.update_approving("3")
        assert r3.result() is Outcome.DENY_ONCE


async def test_approval_escape_cancels(tmp_path):
    """approving 态按 Esc → respond 收到兜底 DENY_ONCE、应用未退出。"""
    app = _new_app(tmp_path)
    async with app.run_test():
        loop = asyncio.get_running_loop()
        respond = loop.create_future()
        app.pending = _pending(respond)
        app.state = SessionState.APPROVING

        app.action_cancel_turn()  # Esc 分派
        assert respond.result() is Outcome.DENY_ONCE
        assert app.pending is None
        assert app.state is SessionState.STREAMING


# ────────── ch08：命令分发与压缩状态提示 ──────────


class FakeAgent:
    """假 Agent：记录 run / run_force_compact 调用次数，按脚本吐事件。"""

    def __init__(
        self,
        events: list[Event] | None = None,
        before: int = 120000,
        after: int = 42000,
        err: Exception | None = None,
    ) -> None:
        self.events = list(events or [])
        self.before = before
        self.after = after
        self.err = err
        self.run_calls = 0
        self.compact_calls = 0

    async def run(self, conv, mode, cancel):
        """按脚本回放事件（测试用，不做真实请求）。"""
        self.run_calls += 1
        for ev in self.events:
            yield ev

    async def run_force_compact(self, conv, tool_defs=None):
        """记录一次手动压缩调用，返回（或抛出）预置结果。"""
        self.compact_calls += 1
        if self.err is not None:
            raise self.err
        return self.before, self.after


def _log_text(app: PotatoCodeApp) -> str:
    """把 scrollback（RichLog）内容拼成纯文本，供断言。"""
    log = app.query_one("#log", RichLog)
    return "\n".join(strip.text for strip in log.lines)


async def _submit(pilot, app: PotatoCodeApp, text: str) -> None:
    """在输入框键入文本并回车提交。"""
    app.query_one("#input", ChatTextArea).insert(text)
    await pilot.press("enter")
    await pilot.pause()


async def _drain_turn(app: PotatoCodeApp, pilot) -> None:
    """等 TUI 消费完本轮事件（回合可能已自行收尾，此时任务已被清空）。"""
    task = app._stream_task
    if task is not None:
        await task
    await pilot.pause()


def test_command_registry_has_four_builtin_commands():
    """I7：注册表恰好注册四项（三个迁移命令 + /compact）。"""
    assert set(BUILTIN_COMMANDS) == {"/exit", "/plan", "/do", "/compact"}


def test_dispatch_command_routing():
    """I7：非命令走对话路径；已注册命令返回处理器；未注册命令走兜底处理器。"""
    assert dispatch_command("你好") == (None, False)
    handler, is_command = dispatch_command("/compact")
    assert is_command is True and handler is BUILTIN_COMMANDS["/compact"]

    unknown, is_command = dispatch_command("/unknown")
    assert is_command is True and unknown is not None


def test_format_compact_notice_phases():
    """I15：四条文案按 phase 与 err 分派，自动 / 紧急 / 手动共用同一格式。"""
    assert (
        format_compact_notice(CompactEvent(phase=CompactPhase.BEFORE_AUTO)) == "正在压缩上下文..."
    )
    assert (
        format_compact_notice(CompactEvent(phase=CompactPhase.BEFORE_EMERGENCY))
        == "上下文撞墙，自动压缩中..."
    )
    done = CompactEvent(phase=CompactPhase.AFTER_AUTO, before=167000, after=12000)
    assert format_compact_notice(done) == "已压缩，token 从 167000 降至 12000"
    failed = CompactEvent(phase=CompactPhase.AFTER_EMERGENCY, err=RuntimeError("炸了"))
    assert format_compact_notice(failed) == "压缩失败：炸了"


async def test_tui_slash_compact_routes_to_command(tmp_path):
    """C14/I8：/compact 走命令路径，只调 run_force_compact，不发普通对话请求。"""
    app = _new_app(tmp_path)
    fake = FakeAgent(before=120000, after=42000)
    async with app.run_test() as pilot:
        app.agent = fake
        await _submit(pilot, app, "/compact")
        await app.compact_task  # 等后台压缩任务收尾
        text = _log_text(app)

    assert fake.compact_calls == 1
    assert fake.run_calls == 0
    assert app.conv.messages() == []  # 命令不写入对话历史
    assert "已压缩，token 从 120000 降至 42000" in text


async def test_tui_compact_message_matches_shared_formatter(tmp_path):
    """I15：手动压缩的完成态文案与统一格式化函数逐字节一致。"""
    app = _new_app(tmp_path)
    fake = FakeAgent(before=500, after=300)  # 无任何阈值校验拦截（AC13）
    async with app.run_test() as pilot:
        app.agent = fake
        await _submit(pilot, app, "/compact")
        await app.compact_task
        text = _log_text(app)

    expected = format_compact_notice(
        CompactEvent(phase=CompactPhase.AFTER_AUTO, before=500, after=300)
    )
    assert expected in text


async def test_tui_compact_failure_message(tmp_path):
    """I8：压缩失败时提示错误，程序不退出。"""
    app = _new_app(tmp_path)
    fake = FakeAgent(err=RuntimeError("摘要服务不可用"))
    async with app.run_test() as pilot:
        app.agent = fake
        await _submit(pilot, app, "/compact")
        await app.compact_task
        text = _log_text(app)

    assert "压缩失败：摘要服务不可用" in text


async def test_tui_unknown_slash_command_friendly(tmp_path):
    """C14/I7：未注册命令给出友好提示，且不发 LLM。"""
    app = _new_app(tmp_path)
    fake = FakeAgent()
    async with app.run_test() as pilot:
        app.agent = fake
        await _submit(pilot, app, "/unknown")
        text = _log_text(app)

    assert "未知命令: /unknown" in text
    assert "/compact" in text  # 提示里列出可用命令
    assert fake.run_calls == 0
    assert app.conv.messages() == []


async def test_tui_migrated_exit_still_works(tmp_path):
    """I7：迁移后 /exit 仍然退出（走命令路径，不开新一轮）。"""
    app = _new_app(tmp_path)
    fake = FakeAgent()
    quit_calls: list[bool] = []

    async def fake_quit() -> None:
        quit_calls.append(True)

    app.action_quit = fake_quit  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        app.agent = fake
        await _submit(pilot, app, "/exit")

    assert quit_calls == [True]
    assert fake.run_calls == 0
    assert app.conv.messages() == []


async def test_tui_migrated_plan_still_works(tmp_path):
    """I7：迁移后 /plan 仍然切到计划模式并给提示，不发请求。"""
    app = _new_app(tmp_path)
    fake = FakeAgent()
    async with app.run_test() as pilot:
        app.agent = fake
        await _submit(pilot, app, "/plan")
        text = _log_text(app)

    assert app.mode is Mode.PLAN
    assert "已进入计划模式" in text
    assert fake.run_calls == 0
    assert app.conv.messages() == []


async def test_tui_migrated_do_still_works(tmp_path):
    """I7：迁移后 /do 仍切回 default、注入执行指令并启动一轮。"""
    app = _new_app(tmp_path)
    fake = FakeAgent(events=[Event(text="按计划开跑。"), Event(done=True)])
    async with app.run_test() as pilot:
        app.mode = Mode.PLAN
        app.agent = fake
        await _submit(pilot, app, "/do")
        await _drain_turn(app, pilot)

    assert app.mode is Mode.DEFAULT
    assert [m.content for m in app.conv.messages()] == [EXECUTE_DIRECTIVE]
    assert fake.run_calls == 1


async def test_tui_renders_before_auto_notice(tmp_path):
    """I15/F24a：收到 BEFORE_AUTO 事件时立即显示"正在压缩上下文..."，不入对话。"""
    app = _new_app(tmp_path)
    fake = FakeAgent(
        events=[
            Event(compact=CompactEvent(phase=CompactPhase.BEFORE_AUTO)),
            Event(compact=CompactEvent(phase=CompactPhase.AFTER_AUTO, before=167000, after=12000)),
            Event(text="压缩后继续。"),
            Event(done=True),
        ]
    )
    async with app.run_test() as pilot:
        app.agent = fake
        app._start_turn()
        await _drain_turn(app, pilot)
        text = _log_text(app)

    assert "正在压缩上下文..." in text
    assert "已压缩，token 从 167000 降至 12000" in text
    assert app.conv.messages() == []  # 提示只进视图层


async def test_tui_renders_before_emergency_notice(tmp_path):
    """I15/F24b：紧急压缩提示前缀为"上下文撞墙，自动压缩中..."。"""
    app = _new_app(tmp_path)
    fake = FakeAgent(
        events=[
            Event(compact=CompactEvent(phase=CompactPhase.BEFORE_EMERGENCY)),
            Event(
                compact=CompactEvent(phase=CompactPhase.AFTER_EMERGENCY, before=180000, after=9000)
            ),
            Event(done=True),
        ]
    )
    async with app.run_test() as pilot:
        app.agent = fake
        app._start_turn()
        await _drain_turn(app, pilot)
        text = _log_text(app)

    assert "上下文撞墙，自动压缩中..." in text
    assert "已压缩，token 从 180000 降至 9000" in text


async def test_tui_renders_compact_failure_event(tmp_path):
    """I15：AFTER 事件带 err 时渲染失败文案，流程继续到 done。"""
    app = _new_app(tmp_path)
    fake = FakeAgent(
        events=[
            Event(compact=CompactEvent(phase=CompactPhase.BEFORE_AUTO)),
            Event(
                compact=CompactEvent(
                    phase=CompactPhase.AFTER_AUTO, err=RuntimeError("摘要请求失败")
                )
            ),
            Event(done=True),
        ]
    )
    async with app.run_test() as pilot:
        app.agent = fake
        app._start_turn()
        await _drain_turn(app, pilot)
        text = _log_text(app)

    assert "压缩失败：摘要请求失败" in text
