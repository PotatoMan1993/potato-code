"""TUI 单测：Shift+Tab 模式切换、状态栏模式显示、人在回路菜单按键、取消（AC9/F7/F8/N4）。"""

from __future__ import annotations

import asyncio

from rich.console import Console

from potatocode.agent import ApprovalRequest
from potatocode.config import ProviderConfig
from potatocode.permission import Mode, Outcome, new_engine
from potatocode.tool import new_default_registry
from potatocode.tui.app import PotatoCodeApp
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
    return PotatoCodeApp([_cfg()], "dev", new_default_registry(), engine)


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
