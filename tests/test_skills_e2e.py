"""ch13 Skill 系统端到端验证（T10/checklist §4）：无头 TUI 驱动全链路。

Windows 下无法用 tmux 驱动交互 TUI，这里用 Textual 的 `run_test()` 无头跑真实
PotatoCodeApp：fake provider 脚本化响应，捕获每轮请求的 environment 段，验证
/help、/skill list、inline 显式调用、热重载、LoadSkill 意图触发与 /clear 清理。
不发起任何真实网络请求。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from potatocode.command.skill_register import build_skill_catalog_text
from potatocode.config import ProviderConfig
from potatocode.llm import Request, StreamEvent, ToolCall
from potatocode.skills import SkillExecutor, SkillLoader
from potatocode.tool.load_skill import LoadSkill
from potatocode.tui.app import PotatoCodeApp
from potatocode.tui.view import SessionState

# 测试 skill 的 SOP 正文（改行前后各一版，验证热重载；两版互不为子串）
SKILL_BODY_V1 = "Echo hello"
SKILL_BODY_V2 = "Shout goodbye"
SKILL_NAME = "test-skill"


class ScriptedProvider:
    """按请求次数逐轮返回脚本事件的 provider 替身，并记录每轮请求。"""

    def __init__(self, scripts: list[list[StreamEvent]]) -> None:
        self._scripts = scripts
        self._repeat = scripts[-1] if scripts else []
        self.calls = 0
        self.requests: list[Request] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    async def stream(self, req: Request) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        self.requests.append(req)
        script = self._scripts[self.calls - 1] if self.calls <= len(self._scripts) else self._repeat
        for _index, event in enumerate(script):
            if event.tool_calls:
                yield StreamEvent(
                    tool_calls=[
                        ToolCall(id=f"c{self.calls}_{i}", name=call.name, input=call.input)
                        for i, call in enumerate(event.tool_calls)
                    ]
                )
            else:
                yield event


def write_test_skill(workspace: Path, body: str) -> Path:
    """在工作区写一个最小 inline skill（checklist §4 第 1 条）。"""
    skill_md = workspace / ".potatocode" / "skills" / SKILL_NAME / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(
        "---\n"
        f"name: {SKILL_NAME}\n"
        'description: "A test skill"\n'
        "mode: inline\n"
        "---\n"
        f"\n{body}\n",
        encoding="utf-8",
    )
    return skill_md


def make_app(workspace: Path, provider: ScriptedProvider) -> PotatoCodeApp:
    """按 cli.py 的装配顺序构造无头测试 App（fake provider 注入）。"""
    import potatocode.tui.app as app_module
    from potatocode.permission import new_engine
    from potatocode.tool import new_default_registry

    app_module.new_provider = lambda cfg: provider  # 替换 provider 构造（不触网）

    registry = new_default_registry()
    skill_loader = SkillLoader(str(workspace))
    skill_loader.load_all()
    load_skill_tool = LoadSkill()
    load_skill_tool.set_loader(skill_loader)
    registry.register(load_skill_tool)
    skill_executor = SkillExecutor(skill_loader)
    engine, _ = new_engine(str(workspace))
    app = PotatoCodeApp(
        [ProviderConfig(name="fake", protocol="anthropic", api_key="dummy", model="fake-model")],
        "test-0.0",
        registry,
        engine,
        runtime=None,
        workspace=str(workspace),
        skill_loader=skill_loader,
        skill_executor=skill_executor,
        load_skill_tool=load_skill_tool,
        skill_catalog_text=build_skill_catalog_text(skill_loader),
    )
    return app


async def wait_until_idle(app: PotatoCodeApp, timeout_seconds: float = 5.0) -> None:
    """等待当前回合结束（回到 IDLE 态），超时失败。"""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while app.state is not SessionState.IDLE:
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"回合未在 {timeout_seconds}s 内结束，当前态: {app.state}")
        await asyncio.sleep(0.05)


def latest_environment(provider: ScriptedProvider) -> str:
    """最近一轮请求的 environment 段文本。"""
    assert provider.requests, "fake provider 没有收到任何请求"
    return provider.requests[-1].system.environment


async def send_user_message(app: PotatoCodeApp, text: str) -> None:
    """模拟一次用户提交（与 on_chat_submitted 的消息路径一致）。"""
    app.conv.add_user(text)
    app._start_turn()


async def capture_println(app: PotatoCodeApp) -> list[str]:
    """把 println 输出接到列表（实例属性遮蔽 mixin 方法）。"""
    captured: list[str] = []
    original = app.println

    def _capture(msg: str) -> None:
        captured.append(msg)
        original(msg)

    app.println = _capture  # type: ignore[method-assign]
    return captured


@pytest.mark.asyncio
async def test_e2e_help_lists_skill_commands(tmp_path: Path) -> None:
    """/help 同时列出 /test-skill [skill] 与 /skill（checklist §4 第 2 条）。"""
    write_test_skill(tmp_path, SKILL_BODY_V1)
    provider = ScriptedProvider([])
    app = make_app(tmp_path, provider)
    async with app.run_test() as pilot:
        captured = await capture_println(app)
        await app.dispatch_slash("/help")
        await pilot.pause()
        joined = "\n".join(captured)
        assert "/test-skill" in joined
        assert "[skill]" in joined
        assert "/skill" in joined


@pytest.mark.asyncio
async def test_e2e_skill_list_and_info(tmp_path: Path) -> None:
    """/skill list 与 /skill info <name>（checklist /skill 子命令）。"""
    write_test_skill(tmp_path, SKILL_BODY_V1)
    provider = ScriptedProvider([])
    app = make_app(tmp_path, provider)
    async with app.run_test() as pilot:
        captured = await capture_println(app)
        await app.dispatch_slash("/skill list")
        await pilot.pause()
        assert "test-skill" in "\n".join(captured)
        assert "[project]" in "\n".join(captured)
        captured.clear()
        await app.dispatch_slash("/skill info test-skill")
        await pilot.pause()
        info_text = "\n".join(captured)
        assert "mode: inline" in info_text
        assert "SKILL.md" in info_text


@pytest.mark.asyncio
async def test_e2e_inline_skill_hot_reload_and_clear(tmp_path: Path) -> None:
    """inline 显式调用 → SOP 进 env；热重载即时生效；/clear 后 env 不再出现（§4）。"""
    skill_md = write_test_skill(tmp_path, SKILL_BODY_V1)
    provider = ScriptedProvider([[StreamEvent(text="ok"), StreamEvent(done=True)]])
    app = make_app(tmp_path, provider)
    async with app.run_test() as pilot:
        # 1) /test-skill：SOP 钉到 env，触发的一轮请求 environment 里能看到
        await app.dispatch_slash(f"/{SKILL_NAME}")
        await wait_until_idle(app)
        environment = latest_environment(provider)
        assert "## Active Skills" in environment
        assert f"### Skill: {SKILL_NAME}" in environment
        assert SKILL_BODY_V1 in environment

        # 2) 热重载：改一行不重启，再调一次，新行进 prompt（§4 第 4 条）
        skill_md.write_text(
            "---\n"
            f"name: {SKILL_NAME}\n"
            'description: "A test skill"\n'
            "mode: inline\n"
            "---\n"
            f"\n{SKILL_BODY_V2}\n",
            encoding="utf-8",
        )
        await app.dispatch_slash(f"/{SKILL_NAME}")
        await wait_until_idle(app)
        assert SKILL_BODY_V2 in latest_environment(provider)
        assert SKILL_BODY_V1 not in latest_environment(provider)

        # 3) /clear：立即再发任意消息，env 里不再出现 Active Skills（§4 第 6 条）
        await app.dispatch_slash("/clear")
        await pilot.pause()
        await send_user_message(app, "随便说点什么")
        await wait_until_idle(app)
        assert "## Active Skills" not in latest_environment(provider)


@pytest.mark.asyncio
async def test_e2e_load_skill_intent_trigger(tmp_path: Path) -> None:
    """自然语言意图触发：Agent 调 LoadSkill → 下一轮 env 出现 SOP（§4 第 5 条）。"""
    write_test_skill(tmp_path, SKILL_BODY_V1)
    provider = ScriptedProvider([
        [StreamEvent(tool_calls=[ToolCall(id="x", name="LoadSkill",
                                          input=json.dumps({"name": SKILL_NAME}))])],
        [StreamEvent(text="done"), StreamEvent(done=True)],
    ])
    app = make_app(tmp_path, provider)
    async with app.run_test():
        await send_user_message(app, "请用 test-skill 这个 skill 帮我做事")
        await wait_until_idle(app)
        # 第 2 轮请求的 env 段里出现 SOP（LoadSkill 激活后的下一轮，spec 5.5）
        assert "## Active Skills" in latest_environment(provider)
        assert SKILL_BODY_V1 in latest_environment(provider)
