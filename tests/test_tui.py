"""TUI 单测：Shift+Tab 模式切换、状态栏模式显示、人在回路菜单按键、取消（AC9/F7/F8/N4）。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from textual.widgets import RichLog, Static

from potatocode.agent import ApprovalRequest, CompactEvent, CompactPhase, Event, new_runtime
from potatocode.compact import sessions_root
from potatocode.config import ProviderConfig
from potatocode.llm import Message, ToolCall, ToolResult
from potatocode.permission import Mode, Outcome, new_engine
from potatocode.prompt import EXECUTE_DIRECTIVE
from potatocode.session import SessionInfo, Writer
from potatocode.tool import new_default_registry
from potatocode.tui.app import ChatTextArea, PotatoCodeApp
from potatocode.tui.commands import (
    BUILTIN_COMMANDS,
    dispatch_command,
    format_compact_notice,
)
from potatocode.tui.resume import ResumeSelect, build_span_reminder
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


def test_command_registry_has_five_builtin_commands():
    """I7：注册表恰好注册五项（三个迁移命令 + /compact + /resume）。"""
    assert set(BUILTIN_COMMANDS) == {"/exit", "/plan", "/do", "/compact", "/resume"}


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


# ────────── ch11：会话恢复界面 ──────────


def _new_app_with_writer(tmp_path) -> PotatoCodeApp:
    """建一个带会话存档的 App：存档落在 tmp_path，不污染工作目录。"""
    engine, _ = new_engine(str(tmp_path))
    runtime = new_runtime(workspace=str(tmp_path))
    writer = Writer(runtime.session.session_dir, model="fake-model")
    return PotatoCodeApp(
        [_cfg()],
        "dev",
        new_default_registry(),
        engine,
        runtime=runtime,
        writer=writer,
        sessions_dir=sessions_root(str(tmp_path)),
        workspace=str(tmp_path),
    )


def _seed_session(tmp_path, session_id: str, *user_texts: str) -> None:
    """在会话根目录下造一个历史会话（每条文本一轮 user + assistant）。"""
    path = Path(sessions_root(str(tmp_path))) / session_id
    with Writer(str(path), model="old-model") as writer:
        for text in user_texts:
            writer.append(Message(role="user", content=text))
            writer.append(Message(role="assistant", content=f"回复：{text}"))


async def test_resume_enters_list_and_shows_items(tmp_path):
    """`/resume` 不发 LLM，进入列表并展示每个会话的标题/时间/模型/大小（AC11/AC12）。"""
    _seed_session(tmp_path, "20260601-100000-aaaa", "alpha project")
    _seed_session(tmp_path, "20260602-100000-bbbb", "beta project")
    app = _new_app_with_writer(tmp_path)
    fake = FakeAgent()
    async with app.run_test() as pilot:
        app.agent = fake
        await _submit(pilot, app, "/resume")
        select = app.query_one(ResumeSelect)
        prompts = [str(o.prompt) for o in select.options]
        hint = str(app.query_one("#resume-hint", Static).content)
        text = _log_text(app)

        assert app.state is SessionState.RESUMING
        assert fake.run_calls == 0  # 命令不发 LLM
        assert select.total == 2
        assert select.session_at(0).id == "20260602-100000-bbbb"  # 最新在前
        assert "beta project" in prompts[0]
        assert "old-model" in prompts[0]  # 模型标签
        assert "just now" in prompts[0]  # 相对时间
        assert "KB" in prompts[0] or "B" in prompts[0]  # 文件大小
        assert "2/2 个会话" in hint
        assert text  # banner 仍然在


async def test_resume_escape_returns_to_idle(tmp_path):
    """列表里按 Esc → 回空闲态，当前会话不变（AC11）。"""
    _seed_session(tmp_path, "20260601-100000-aaaa", "alpha project")
    app = _new_app_with_writer(tmp_path)
    async with app.run_test() as pilot:
        await _submit(pilot, app, "/resume")
        assert app.state is SessionState.RESUMING
        await pilot.press("escape")

        assert app.state is SessionState.IDLE
        assert app.conv.messages() == []


async def test_resume_search_filters_list(tmp_path):
    """输入搜索词后列表只展示标题匹配的会话（AC13）。"""
    _seed_session(tmp_path, "20260601-100000-aaaa", "alpha project")
    _seed_session(tmp_path, "20260602-100000-bbbb", "beta project")
    app = _new_app_with_writer(tmp_path)
    async with app.run_test() as pilot:
        await _submit(pilot, app, "/resume")
        select = app.query_one(ResumeSelect)
        assert select.shown_count == 2

        await pilot.press("a")
        await pilot.press("l")
        assert select.filter_text == "al"
        assert select.shown_count == 1
        assert select.session_at(0).id == "20260601-100000-aaaa"

        await pilot.press("backspace")
        assert select.filter_text == "a"
        assert select.shown_count == 2  # 退一格后 "a" 同时命中 alpha 与 beta

        await pilot.press("escape")
        assert app.state is SessionState.IDLE


async def test_resume_enter_restores_history(tmp_path):
    """Enter 恢复：对话历史被换回，且新消息接着写进被恢复会话的存档（AC17/AC18）。"""
    _seed_session(tmp_path, "20260601-100000-aaaa", "alpha project")
    journal = Path(sessions_root(str(tmp_path))) / "20260601-100000-aaaa" / "conversation.jsonl"
    before_lines = len(journal.read_text(encoding="utf-8").splitlines())

    app = _new_app_with_writer(tmp_path)
    fake = FakeAgent(events=[Event(text="接着聊。"), Event(done=True)])
    async with app.run_test() as pilot:
        app.agent = fake
        await _submit(pilot, app, "/resume")
        await pilot.press("enter")
        await pilot.pause()
        text = _log_text(app)

        assert app.state is SessionState.IDLE
        assert [m.content for m in app.conv.messages()] == [
            "alpha project",
            "回复：alpha project",
        ]
        assert "已恢复会话 20260601-100000-aaaa，共 2 条消息" in text

        await _submit(pilot, app, "继续")
        await _drain_turn(app, pilot)

    after_lines = journal.read_text(encoding="utf-8").splitlines()
    assert len(after_lines) > before_lines  # 新消息追加到同一个 JSONL（AC18）
    assert any('"继续"' in line for line in after_lines)


async def test_resume_replays_history_into_log(tmp_path):
    """Enter 恢复：历史消息被回放到对话区，用户能直接看到内容（AC17）。"""
    _seed_session(tmp_path, "20260601-100000-aaaa", "alpha project", "第二个问题")
    app = _new_app_with_writer(tmp_path)
    async with app.run_test() as pilot:
        await _submit(pilot, app, "/resume")
        await pilot.press("enter")
        await pilot.pause()
        text = _log_text(app)

    assert "> alpha project" in text  # 用户块
    assert "回复：alpha project" in text  # 助手块
    assert "> 第二个问题" in text
    assert "回复：第二个问题" in text
    assert "已恢复会话 20260601-100000-aaaa，共 4 条消息" in text


async def test_resume_replays_tool_calls_into_log(tmp_path):
    """含工具调用的历史：工具行与结果摘要一并回放，不留白。"""
    path = Path(sessions_root(str(tmp_path))) / "20260601-100000-aaaa"
    with Writer(str(path), model="old-model") as writer:
        writer.append(Message(role="user", content="读一下 README"))
        writer.append(
            Message(
                role="assistant",
                content="好的，我来看看",
                tool_calls=[ToolCall(id="t1", name="read_file", input='{"path": "README.md"}')],
            )
        )
        writer.append(
            Message(
                role="tool",
                tool_results=[
                    ToolResult(tool_call_id="t1", content="第 1 行\n第 2 行", is_error=False)
                ],
            )
        )
        writer.append(Message(role="assistant", content="读完了"))
    app = _new_app_with_writer(tmp_path)
    async with app.run_test() as pilot:
        await _submit(pilot, app, "/resume")
        await pilot.press("enter")
        await pilot.pause()
        text = _log_text(app)

    assert "好的，我来看看" in text  # preamble
    assert "read_file" in text and "README.md" in text  # 工具行
    assert "第 2 行" in text  # 工具结果摘要
    assert "读完了" in text  # 最终答复


async def test_resume_replay_clears_previous_scrollback(tmp_path):
    """恢复时清掉上一会话的界面残留，新旧历史不混在一起。"""
    _seed_session(tmp_path, "20260601-100000-aaaa", "alpha project")
    app = _new_app_with_writer(tmp_path)
    fake = FakeAgent(events=[Event(text="旧会话的回复"), Event(done=True)])
    async with app.run_test() as pilot:
        app.agent = fake
        await _submit(pilot, app, "旧会话的问题")
        await _drain_turn(app, pilot)
        assert "旧会话的问题" in _log_text(app)

        await _submit(pilot, app, "/resume")
        # 当前会话也在列表里且排在最前，用搜索词选中目标会话（AC13）
        await pilot.press("a", "l", "p", "h", "a")
        await pilot.press("enter")
        await pilot.pause()
        text = _log_text(app)

    assert "alpha project" in text
    assert "旧会话的问题" not in text
    assert "旧会话的回复" not in text


async def test_resume_adds_span_reminder_for_stale_session(tmp_path):
    """最后一条消息距今超过 6 小时 → 恢复时补一条时间跨度提醒（AC17）。"""
    session_dir = Path(sessions_root(str(tmp_path))) / "20260601-100000-aaaa"
    session_dir.mkdir(parents=True)
    stale = int(time.time()) - 7 * 3600
    (session_dir / "conversation.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {"role": "user", "content": "很久以前", "ts": stale}, ensure_ascii=False
                ),
                json.dumps(
                    {"role": "assistant", "content": "好的", "ts": stale}, ensure_ascii=False
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    app = _new_app_with_writer(tmp_path)
    async with app.run_test() as pilot:
        await _submit(pilot, app, "/resume")
        await pilot.press("enter")
        await pilot.pause()

        msgs = app.conv.messages()

    assert msgs[-1].role == "user"
    assert "已暂停" in msgs[-1].content
    assert "7 小时" in msgs[-1].content


async def test_resume_skips_old_format_sessions(tmp_path):
    """旧格式 session id 的目录不出现在列表里（AC20）。"""
    _seed_session(tmp_path, "20260601-100000-aaaa", "alpha project")
    legacy = Path(sessions_root(str(tmp_path))) / "1717000000-abc12345"
    legacy.mkdir(parents=True)
    (legacy / "conversation.jsonl").write_text(
        '{"role":"user","content":"旧格式","ts":1}\n', encoding="utf-8"
    )
    app = _new_app_with_writer(tmp_path)
    async with app.run_test() as pilot:
        await _submit(pilot, app, "/resume")
        select = app.query_one(ResumeSelect)

    assert select.total == 1
    assert select.session_at(0).id == "20260601-100000-aaaa"


async def test_resume_without_history_shows_notice(tmp_path):
    """没有任何历史会话时只提示，不进入列表状态。"""
    app = _new_app_with_writer(tmp_path)
    async with app.run_test() as pilot:
        await _submit(pilot, app, "/resume")
        text = _log_text(app)

    assert app.state is SessionState.IDLE
    assert "没有找到可恢复的历史会话" in text


async def test_resume_blocked_while_streaming(tmp_path):
    """Agent 运行期间输入 /resume → 只提示等待，不进入列表（AC29）。"""
    _seed_session(tmp_path, "20260601-100000-aaaa", "alpha project")
    app = _new_app_with_writer(tmp_path)
    async with app.run_test() as pilot:
        app.state = SessionState.STREAMING
        await _submit(pilot, app, "/resume")
        text = _log_text(app)

    assert app.state is SessionState.STREAMING
    assert "请等待当前任务完成" in text


async def test_build_span_reminder_threshold():
    """6 小时以内不提醒，超过才提醒（阈值边界）。"""
    now = 1_000_000.0
    assert build_span_reminder(0, now) == ""
    assert build_span_reminder(int(now - 3600), now) == ""
    reminder = build_span_reminder(int(now - 6 * 3600 - 1), now)
    assert "已暂停" in reminder
    assert "6 小时" in reminder


async def test_resume_up_down_navigates(tmp_path):
    """上下键切换高亮项（AC：上下键导航）。"""
    _seed_session(tmp_path, "20260601-100000-aaaa", "alpha project")
    _seed_session(tmp_path, "20260602-100000-bbbb", "beta project")
    app = _new_app_with_writer(tmp_path)
    async with app.run_test() as pilot:
        await _submit(pilot, app, "/resume")
        select = app.query_one(ResumeSelect)
        assert select.highlighted == 0

        await pilot.press("down")
        assert select.highlighted == 1
        assert select.session_at(select.highlighted).id == "20260601-100000-aaaa"

        await pilot.press("up")
        assert select.highlighted == 0

        await pilot.press("up")  # 顶部再上移则回卷到最后一项（列表循环行为）
        assert select.highlighted == 1


async def test_resume_compacts_when_history_too_long(tmp_path):
    """加载后估算 token 超阈值 → 恢复流程中先压一次再进场（AC16）。"""
    _seed_session(tmp_path, "20260601-100000-aaaa", *[f"第{i}个很长的问题" * 20 for i in range(60)])
    app = _new_app_with_writer(tmp_path)
    fake = FakeAgent(before=9000, after=800)
    async with app.run_test() as pilot:
        app.agent = fake
        # 选定 provider 后才有确定窗口，此处再压低：阈值 2000-20000-13000 为负 → 必然压缩
        app.runtime.context_window = 2000
        await _submit(pilot, app, "/resume")
        await pilot.press("enter")
        await pilot.pause()
        text = _log_text(app)

        assert fake.compact_calls == 1
        assert "正在压缩" in text
        # FakeAgent 不改历史，此处只验证「压缩被调用且恢复继续」，真实压缩由 agent 测试覆盖
        assert app.state is SessionState.IDLE
        assert "已恢复会话 20260601-100000-aaaa" in text


async def test_resume_failure_keeps_current_session(tmp_path):
    """恢复过程出错 → 提示失败并保持当前会话上下文不变（N5）。"""
    app = _new_app_with_writer(tmp_path)
    before_ctx = app.runtime.session
    info = SessionInfo(
        id="20260601-100000-aaaa",
        title="坏会话",
        modified_at=datetime.now(),
        model="m",
        size=10,
        dir=str(tmp_path / "不存在的目录"),
    )
    async with app.run_test() as pilot:
        await _submit(pilot, app, "/resume")
        app.enter_resume_mode([info])
        await pilot.press("enter")
        await pilot.pause()
        text = _log_text(app)

    assert app.state is SessionState.IDLE  # 不会卡在恢复态
    assert app.runtime.session is before_ctx  # 上下文回滚
    assert "已恢复会话" not in text
