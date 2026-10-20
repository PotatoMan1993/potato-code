"""Hook 系统端到端验证（ch14 T25/checklist 场景 1/2/4/6/7/8）。

Windows 无 tmux：用 Textual `run_test()` headless pilot 驱动完整 TUI——
真实 PotatoCodeApp + hook.load 解析的 hooks.yaml + fake provider 脚本，覆盖
PreToolUse 拦截、SessionStart 注入、UserPromptSubmit 拦截、only_once 与
/clear 重置、坏配置不阻断、/hooks 命令。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

from textual.widgets import RichLog

from potatocode.agent import new_runtime
from potatocode.config import ProviderConfig
from potatocode.hook import load as load_hooks
from potatocode.llm import Request, StreamEvent, ToolCall
from potatocode.permission import new_engine
from potatocode.tool import new_default_registry
from potatocode.tui.app import PotatoCodeApp
from potatocode.tui.view import SessionState

# 覆盖场景 1/2/4/6 的 hooks.yaml（场景 6 用 marker 文件验证 only_once 的执行次数）
HOOKS_YAML = """
hooks:
  - name: zh-cn-default
    event: SessionStart
    action:
      type: prompt
      text: "e2e: 默认用 zh-CN 回复"
  - name: block-write
    event: PreToolUse
    if:
      all_of:
        - field: tool_name
          match: { type: exact, value: write_file }
    action:
      type: shell
      command: "echo blocked by hook >&2; exit 2"
  - name: warn-delete
    event: UserPromptSubmit
    if:
      all_of:
        - field: prompt
          match: { type: regex, value: "(?i)delete" }
    action:
      type: shell
      command: "echo 用户消息含 delete 关键字 >&2; exit 2"
  - name: first-turn
    event: PreUserMessage
    only_once: true
    action:
      type: shell
      command: "touch {marker}"
"""


class ScriptedProvider:
    """端到端 fake provider：按脚本逐轮返回事件并记录请求。"""

    def __init__(self, scripts: list[list[StreamEvent]], repeat: list[StreamEvent]) -> None:
        self._scripts = scripts
        self._repeat = repeat
        self.calls = 0
        self.reminders_seen: list[str] = []

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def model(self) -> str:
        return "scripted-model"

    async def stream(self, req: Request) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        self.reminders_seen.append(req.reminder)
        script = self._scripts[self.calls - 1] if self.calls <= len(self._scripts) else self._repeat
        for ev in script:
            if ev.tool_calls:
                yield StreamEvent(
                    tool_calls=[
                        ToolCall(id=f"c{self.calls}_{i}", name=c.name, input=c.input)
                        for i, c in enumerate(ev.tool_calls)
                    ]
                )
            else:
                yield ev


def _log_text(app: PotatoCodeApp) -> str:
    """把 scrollback 内容拍平为纯文本（断言错误块 / 工具结果用）。"""
    log = app.query_one("#log", RichLog)
    return "\n".join(strip.text for strip in log.lines)


async def test_hook_e2e_scenarios(tmp_path: Path, monkeypatch):
    """一条流水线覆盖 checklist 场景 1/2/4/6/7/8 的可自动化部分。"""
    marker = tmp_path / "first-turn-marker"
    (tmp_path / ".potatocode").mkdir()
    (tmp_path / ".potatocode" / "hooks.yaml").write_text(
        HOOKS_YAML.replace("{marker}", marker.as_posix()), encoding="utf-8"
    )

    hook_engine = load_hooks(str(tmp_path))
    assert len(hook_engine.rules) == 4  # 场景 7 的坏配置单测见 test_hook_loader.py

    permission_engine, _ = new_engine(str(tmp_path))
    runtime = new_runtime(workspace=str(tmp_path))
    cfg = ProviderConfig(
        name="fake", protocol="anthropic", api_key="sk-test", model="fake-model"
    )
    app = PotatoCodeApp(
        [cfg],
        "e2e",
        new_default_registry(),
        permission_engine,
        runtime=runtime,
        sessions_dir=str(tmp_path / "sessions"),
        workspace=str(tmp_path),
        hook_engine=hook_engine,
    )

    # fake provider：第一轮调 write_file（触发场景 1 拦截），后续轮纯文本自然完成
    provider = ScriptedProvider(
        scripts=[
            [StreamEvent(tool_calls=[ToolCall(id="x", name="write_file", input=json.dumps(
                {"path": str(tmp_path / "hooked.txt"), "content": "hi"}
            ))])]
        ],
        repeat=[StreamEvent(text="done")],
    )

    async with app.run_test() as pilot:
        # 直接注入 fake provider（绕过 _activate_provider 的真实适配器构造）
        app.provider = provider
        app.state = SessionState.IDLE
        await pilot.pause()

        # —— 场景 2：SessionStart 注入已进 reminder 队列 ——
        assert runtime.pending_reminders == ["e2e: 默认用 zh-CN 回复"]

        # —— 场景 1 + 6：首轮对话触发 write_file 拦截 + first-turn only_once ——
        for ch in "create hooked.txt":
            await pilot.press(ch)
        await pilot.press("enter")
        for _ in range(80):
            await pilot.pause()
            if app.state is SessionState.IDLE and provider.calls >= 2:
                break  # 拦截结果回灌后 agent 还会再跑一轮纯文本收尾
        assert app.state is SessionState.IDLE
        assert provider.calls == 2
        assert not (tmp_path / "hooked.txt").exists()  # 场景 1：文件未被写入
        assert "[hook block-write] blocked by hook" in _log_text(app)  # 场景 1：回灌原因
        assert marker.exists()  # 场景 6：only_once 首轮触发
        marker.unlink()
        # 场景 2：SessionStart 注入本轮已消费（进请求 reminder）
        assert "e2e: 默认用 zh-CN 回复" in provider.reminders_seen[0]

        # —— 场景 4：含 delete 关键字的输入被拦截、消息不进历史、输入框保留 ——
        for ch in "please delete that file":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert app.state is SessionState.IDLE  # 未发起 LLM 回合
        assert "[hook warn-delete] 用户消息含 delete 关键字" in _log_text(app)
        assert not any("please delete" in m.content for m in app.conv.messages())
        from potatocode.tui.app import ChatTextArea

        # F32：焦点返回输入框、内容保留等用户重新编辑
        assert app.query_one(ChatTextArea).text == "please delete that file"
        assert not marker.exists()  # 场景 6：only_once 未再次执行（UserPromptSubmit 不触发它）
        app.query_one(ChatTextArea).clear()  # 模拟用户清空重编

        # —— 场景 6：正常输入再次走 PreUserMessage，only_once 仍跳过 ——
        for ch in "second turn":
            await pilot.press(ch)
        await pilot.press("enter")
        for _ in range(80):
            await pilot.pause()
            if app.state is SessionState.IDLE and provider.calls >= 3:
                break
        assert provider.calls == 3
        assert not marker.exists()  # only_once：第二轮未再触发

        # —— 场景 8：/hooks 命令输出 ——
        for ch in "/hooks":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        hooks_text = _log_text(app)
        assert "zh-cn-default" in hooks_text
        assert "block-write" in hooks_text
        assert "warn-delete" in hooks_text
        assert "first-turn" in hooks_text
        assert "Loaded from:" in hooks_text
        # 路径过长会被 RichLog 折行：只断言路径关键片段可见
        loaded_line = hooks_text.split("Loaded from:")[-1]
        assert ".potatocode" in loaded_line
        assert "hooks.yaml" in loaded_line

        # —— /clear：SessionEnd/SessionStart + only_once 重置（场景 6 收尾）——
        for ch in "/clear":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert runtime.pending_reminders == ["e2e: 默认用 zh-CN 回复"]  # 新会话重新 SessionStart

        for ch in "third turn":
            await pilot.press(ch)
        await pilot.press("enter")
        for _ in range(80):
            await pilot.pause()
            if app.state is SessionState.IDLE and provider.calls >= 4:
                break
        assert provider.calls == 4
        assert marker.exists()  # only_once 在新会话重新触发（AC9）


async def test_e2e_bad_config_does_not_block_startup(tmp_path: Path, capfd):
    """场景 7：坏 hook（async + 拦截事件）报 stderr 跳过、好 hook 正常加载（N1）。"""
    (tmp_path / ".potatocode").mkdir()
    (tmp_path / ".potatocode" / "hooks.yaml").write_text(
        """
hooks:
  - name: bad-async
    event: PreToolUse
    async: true
    action: { type: shell, command: "echo x" }
  - name: good-hook
    event: SessionStart
    action: { type: shell, command: "echo ok" }
""",
        encoding="utf-8",
    )
    hook_engine = load_hooks(str(tmp_path))
    captured = capfd.readouterr()
    assert 'hook "bad-async": async not allowed for blocking events' in captured.err
    assert [r.name for r in hook_engine.rules] == ["good-hook"]


async def test_e2e_stop_http_notifies_local_server(tmp_path: Path):
    """场景 5（AC13）：Stop hook 对本地真实 HTTP server 发 POST，body 含 "event":"Stop"。"""
    import asyncio
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from potatocode.hook import Engine as HookEngine
    from potatocode.hook import Event as HookEvent
    from potatocode.hook.rule import Action, ActionType, HttpAction, Rule

    received: list[dict] = []

    class _Recorder(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server 命名约定
            length = int(self.headers.get("Content-Length", 0))
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args) -> None:  # 静默访问日志
            pass

    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        stop_rule = Rule(
            name="notify-stop",
            event=HookEvent.STOP,
            action=Action(
                type=ActionType.HTTP,
                http=HttpAction(url=f"http://127.0.0.1:{port}/done", method="POST"),
            ),
        )
        engine = HookEngine([stop_rule], [])
        result = await engine.dispatch(
            HookEvent.STOP,
            {"event": "Stop", "session_id": "s1", "cwd": str(tmp_path), "mode": "default"},
        )
        await asyncio.sleep(0)  # 让响应处理完成
        assert result.blocked is False
        assert received == [
            {"event": "Stop", "session_id": "s1", "cwd": str(tmp_path), "mode": "default"}
        ]
    finally:
        server.shutdown()
        server.server_close()
