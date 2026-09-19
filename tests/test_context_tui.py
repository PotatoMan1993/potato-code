"""Windows Textual 端到端：实际命令提交、忙碌保护、取消和再次输入。"""

import asyncio

import pytest
from textual.widgets import RichLog

from context_helpers import ContextProvider, substantial_conversation, summary_events
from potatocode.config import ProviderConfig
from potatocode.context.manager import ContextManager
from potatocode.llm import ContextLimits
from potatocode.permission import Mode, new_engine
from potatocode.tool import Registry
from potatocode.tui.app import ChatSubmitted, ChatTextArea, PotatoCodeApp
from potatocode.tui.view import SessionState


def app_for(tmp_path, provider, monkeypatch):
    monkeypatch.setattr("potatocode.tui.app.new_provider", lambda _: provider)
    engine, _ = new_engine(str(tmp_path))
    cfg = ProviderConfig(name="测试", protocol="openai", model="test", api_key="test")
    app = PotatoCodeApp([cfg], "test", Registry(), engine)
    app.conv = substantial_conversation()
    app.context_manager = ContextManager(app.conv, engine)
    return app


async def submit(app, pilot, text):
    app.query_one(ChatTextArea).load_text(text)
    await pilot.press("enter")


async def idle(app):
    async with asyncio.timeout(4):
        while app.state is not SessionState.IDLE or app._stream_task is not None:
            await asyncio.sleep(0.01)


async def test_manual_compact_keeps_mode_and_recent_text_without_empty_assistant(
    tmp_path, monkeypatch
):
    provider = ContextProvider()
    app = app_for(tmp_path, provider, monkeypatch)
    async with app.run_test() as pilot:
        app.mode = Mode.PLAN
        before = app.conv.snapshot()
        # 空助手块只能经此方法写入；手动操作不应调用它。
        monkeypatch.setattr(app, "_finish_turn", lambda _: pytest.fail("手动压缩不应产生助手回复"))
        await submit(app, pilot, "/compact")
        await idle(app)
        assert app.mode is Mode.PLAN
        assert [r.purpose for r in provider.requests] == ["summary"]
        assert app.conv.snapshot().entries[1:] == before.entries[-5:]
        assert all(m.content != "/compact" for m in app.conv.messages())
        assert app.usage_in == 100 and app.usage_out == 40
        assert app.query_one(ChatTextArea).has_focus


async def test_compacting_blocks_submit_and_mode_change_escape_cancels_then_can_chat(
    tmp_path, monkeypatch
):
    provider = ContextProvider(summary_scripts=[[asyncio.Event()]])
    app = app_for(tmp_path, provider, monkeypatch)
    async with app.run_test() as pilot:
        before = app.conv.snapshot()
        await submit(app, pilot, "/compact")
        await asyncio.wait_for(provider.waiting.wait(), 1)
        assert app.state is SessionState.COMPACTING
        await app.on_chat_submitted(ChatSubmitted("不能插入忙碌会话"))
        app.action_cycle_mode()
        assert app.mode is Mode.DEFAULT
        await pilot.press("escape")
        await idle(app)
        assert app.conv.snapshot() == before and provider.closed == 1
        assert not app.context_manager._lock.locked()
        await submit(app, pilot, "继续验证")
        await idle(app)
        assert app.conv.messages()[-1].content == "完成"
        assert app.usage_incomplete


async def test_manual_three_failures_then_fresh_operation_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr("potatocode.context.manager.RETRY_DELAYS", (0.001, 0.001))
    provider = ContextProvider(summary_scripts=[summary_events("bad") for _ in range(3)])
    app = app_for(tmp_path, provider, monkeypatch)
    async with app.run_test() as pilot:
        before = app.conv.snapshot()
        await submit(app, pilot, "/compact")
        await idle(app)
        assert app.conv.snapshot() == before and len(provider.requests) == 3
        await submit(app, pilot, "/compact")
        await idle(app)
        assert app.conv.snapshot().entries[0].origin == "summary"
        assert len(provider.requests) == 4


async def test_manual_skip_does_not_add_history_or_normal_request(tmp_path, monkeypatch):
    provider = ContextProvider()
    app = app_for(tmp_path, provider, monkeypatch)
    app.conv = substantial_conversation(4)
    app.context_manager = ContextManager(app.conv, app.engine)
    async with app.run_test() as pilot:
        before = app.conv.snapshot()
        await submit(app, pilot, "/compact")
        await idle(app)
        assert app.conv.snapshot() == before and not provider.requests
        assert any("近期保留" in line.text for line in app.query_one(RichLog).lines)


async def test_missing_window_reports_in_ui_and_later_configured_request_works(
    tmp_path, monkeypatch
):
    provider = ContextProvider(window=None)
    app = app_for(tmp_path, provider, monkeypatch)
    async with app.run_test() as pilot:
        await submit(app, pilot, "继续")
        await idle(app)
        assert not provider.requests
        assert any("context_window" in line.text for line in app.query_one(RichLog).lines)
        provider.context_limits = ContextLimits(128000)
        await submit(app, pilot, "再次继续")
        await idle(app)
        assert provider.requests[-1].purpose == "normal"
        assert app.conv.messages()[-1].content == "完成"
