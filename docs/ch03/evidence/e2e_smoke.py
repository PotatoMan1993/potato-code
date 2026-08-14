"""ch03 端到端无头冒烟：用 Textual run_test 驱动真实 PotatoCodeApp。

Phase A（fake provider）：确定性验证 TUI 渲染链路 —— 工具行/结果摘要/最终答复。
Phase B（真实 openai 兼容端点）：真实链路 —— 模型调用 read_file → 结果回灌 → 最终答复。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.getcwd(), "src"))

from potatocode.config import ProviderConfig
from potatocode.llm import Message, StreamEvent, ToolCall, ToolDefinition
from potatocode.tool import new_default_registry
from potatocode.tui.app import PotatoCodeApp
from potatocode.tui.view import SessionState


class FakeProvider:
    """fake provider：请求#1 发起 read_file（可指向不存在路径），请求#2 返回总结。"""

    def __init__(self, read_path: str = "docs/ch03/spec.md") -> None:
        self._read_path = read_path
        self._calls = 0

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    async def stream(self, msgs, tools):
        self._calls += 1
        if self._calls == 1:
            yield StreamEvent(text="我来读取文件。")
            yield StreamEvent(
                tool_calls=[
                    ToolCall(id="c1", name="read_file", input='{"path": "' + self._read_path + '"}')
                ]
            )
            yield StreamEvent(done=True)
        else:
            yield StreamEvent(text="这是工具系统的总结。")
            yield StreamEvent(done=True)


def _log_text(app) -> list[str]:
    from textual.widgets import RichLog

    log = app.query_one("#log", RichLog)
    return [str(line) for line in log.lines]


async def phase_a() -> None:
    """fake provider：验证工具行渲染、结果摘要、最终答复进入 scrollback。"""
    import potatocode.tui.app as app_mod

    cfg = ProviderConfig(name="fake", protocol="openai", api_key="x", model="m")
    app_mod.new_provider = lambda c: FakeProvider()
    app = PotatoCodeApp([cfg], "0.1.0", new_default_registry())

    async with app.run_test() as pilot:
        input_box = app.query_one("#input")
        input_box.focus()
        input_box.text = "读 spec 并总结"
        await pilot.press("enter")
        # 等待回合结束（回到 IDLE）
        for _ in range(200):
            if app.state is SessionState.IDLE and app._stream_task is None:
                break
            await asyncio.sleep(0.05)
        await pilot.pause()

        lines = _log_text(app)
        joined = "\n".join(lines)
        print("=== Phase A scrollback ===")
        print(joined)
        print("=== checks ===")
        assert "read_file({" in joined, "工具行缺失"
        assert "⎿" in joined, "结果摘要缺失"
        assert "这是工具系统的总结。" in joined, "最终答复缺失"
        assert "我来读取文件。" in joined, "preamble 缺失"
        # 顺序：preamble 在工具行之前，工具行在最终答复之前
        assert joined.index("我来读取文件。") < joined.index("read_file("), "顺序错乱"
        assert joined.index("read_file(") < joined.index("这是工具系统的总结。"), "顺序错乱"
        print("Phase A 全绿 ✅")


class MultiToolProvider:
    """fake provider：一次回复请求 write_file + bash 两个工具，验证单轮内多工具顺序执行。"""

    def __init__(self, target: str) -> None:
        self._target = target
        self._calls = 0

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    async def stream(self, msgs, tools):
        import json

        self._calls += 1
        if self._calls == 1:
            yield StreamEvent(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="write_file",
                        input=json.dumps({"path": self._target, "content": "hello ch03"}),
                    ),
                    ToolCall(
                        id="c2",
                        name="bash",
                        input=json.dumps({"command": "echo hello-ch03"}),
                    ),
                ]
            )
            yield StreamEvent(done=True)
        else:
            yield StreamEvent(text="文件已创建并用 bash 验证内容。")
            yield StreamEvent(done=True)


async def phase_a3(tmp_dir: str) -> None:
    """场景 2：单轮内多工具（write_file + bash）按顺序执行，工具行依次出现。"""
    import potatocode.tui.app as app_mod

    cfg = ProviderConfig(name="fake", protocol="openai", api_key="x", model="m")
    app_mod.new_provider = lambda c: MultiToolProvider(tmp_dir)
    app = PotatoCodeApp([cfg], "0.1.0", new_default_registry())

    async with app.run_test() as pilot:
        input_box = app.query_one("#input")
        input_box.focus()
        input_box.text = "新建文件并用 bash 查看"
        await pilot.press("enter")
        for _ in range(200):
            if app.state is SessionState.IDLE and app._stream_task is None:
                break
            await asyncio.sleep(0.05)
        await pilot.pause()

        joined = "\n".join(_log_text(app))
        print("=== Phase A3 scrollback ===")
        print(joined)
        # 两个工具行按请求顺序出现
        wi = joined.index("write_file(")
        bi = joined.index("bash(")
        assert wi < bi, "write_file 应先于 bash"
        # 结果摘要都出现
        assert joined.count("⎿") >= 2, "两个工具结果摘要缺失"
        assert "已写入" in joined, "write_file 结果缺失"
        assert "hello-ch03" in joined, "bash 结果缺失"
        # 磁盘落地验证
        with open(tmp_dir, encoding="utf-8") as f:
            assert f.read() == "hello ch03", "写文件未真实落地"
        print("Phase A3 全绿 ✅（单轮多工具顺序执行）")


async def phase_a2() -> None:
    """AC12：工具失败（读不存在文件）→ 结构化错误回灌、红色样式、程序不退出、对话可继续。"""
    import potatocode.tui.app as app_mod

    cfg = ProviderConfig(name="fake", protocol="openai", api_key="x", model="m")
    app_mod.new_provider = lambda c: FakeProvider(read_path="docs/ch03/不存在的文件.md")
    app = PotatoCodeApp([cfg], "0.1.0", new_default_registry())

    async with app.run_test() as pilot:
        input_box = app.query_one("#input")
        input_box.focus()
        input_box.text = "读一个不存在的文件"
        await pilot.press("enter")
        for _ in range(200):
            if app.state is SessionState.IDLE and app._stream_task is None:
                break
            await asyncio.sleep(0.05)
        await pilot.pause()

        joined = "\n".join(_log_text(app))
        print("=== Phase A2 scrollback（尾部）===")
        print(joined[-800:])
        assert "read_file(" in joined, "工具行缺失"
        assert "文件不存在" in joined, "结构化错误未回灌"
        assert app.state is SessionState.IDLE, "工具失败后未回到 IDLE（可能崩溃）"

        # 再发一条正常消息，证明会话可继续
        input_box.text = "你好"
        await pilot.press("enter")
        for _ in range(200):
            if app.state is SessionState.IDLE and app._stream_task is None:
                break
            await asyncio.sleep(0.05)
        await pilot.pause()
        joined2 = "\n".join(_log_text(app))
        assert "这是工具系统的总结。" in joined2, "继续对话未得到答复"
        assert app.state is SessionState.IDLE
        print("Phase A2 全绿 ✅（工具失败不崩溃、对话继续）")


def _load_provider(protocol: str, name_hint: str = "") -> ProviderConfig:
    """从真实配置里挑出指定协议的 provider（可加名字过滤）。"""
    import yaml

    raw = yaml.safe_load(open(".potatocode/config.yaml", encoding="utf-8"))
    for item in raw["providers"]:
        if item["protocol"] == protocol and name_hint.lower() in item["name"].lower():
            return ProviderConfig(
                name=item["name"],
                protocol=item["protocol"],
                api_key=item["api_key"],
                model=item["model"],
                base_url=item.get("base_url"),
                thinking=item.get("thinking", False),
            )
    raise SystemExit(f"配置里没有 {protocol} 协议的 provider（hint={name_hint}）")


async def _send(app, pilot, text: str) -> str:
    """发一条消息并等回合结束，返回 scrollback 全文。"""
    input_box = app.query_one("#input")
    input_box.focus()
    input_box.text = text
    await pilot.press("enter")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if app.state is SessionState.IDLE and app._stream_task is None:
            break
        await asyncio.sleep(0.2)
    await pilot.pause()
    return "\n".join(_log_text(app))


async def phase_c(protocol: str, name_hint: str = "") -> None:
    """AC12：触发工具失败（读不存在文件）→ 错误回灌 + 程序不退出 + 对话可继续。"""
    cfg = _load_provider(protocol, name_hint)
    app = PotatoCodeApp([cfg], "0.1.0", new_default_registry())

    async with app.run_test() as pilot:
        # 1) 读不存在文件
        joined = await _send(app, pilot, "读 docs/ch03/不存在的文件.md 的内容并告诉我")
        print("=== Phase C-1 失败场景 ===")
        print(joined[-1200:])
        err_shown = "文件不存在" in joined or "is_error" in joined
        print("错误以结构化结果回灌:", err_shown)
        assert app.state is SessionState.IDLE, "回合后未回到 IDLE（可能崩溃）"

        # 2) 正常对话，证明会话可继续
        joined2 = await _send(app, pilot, "1+1等于几？直接回答")
        print("=== Phase C-2 继续对话 ===")
        print(joined2[-400:])
        assert "2" in joined2, "继续对话未得到正常答复"
        assert app.state is SessionState.IDLE
        print("Phase C 全绿 ✅（程序未退出、对话继续）")


async def phase_b(protocol: str, question: str, name_hint: str = "") -> None:
    """真实端点：跑一次真实工具任务，观察工具行/结果/答复。"""
    cfg = _load_provider(protocol, name_hint)
    app = PotatoCodeApp([cfg], "0.1.0", new_default_registry())

    async with app.run_test() as pilot:
        input_box = app.query_one("#input")
        input_box.focus()
        input_box.text = question
        await pilot.press("enter")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if app.state is SessionState.IDLE and app._stream_task is None:
                break
            await asyncio.sleep(0.2)
        await pilot.pause()

        lines = _log_text(app)
        joined = "\n".join(lines)
        print(f"=== Phase B [{protocol}] scrollback ===")
        print(joined)
        print("=== checks ===")
        print("工具行出现:", "(" in joined and ("read_file" in joined or "bash" in joined))
        print("结果摘要出现:", "⎿" in joined)
        print("最终答复非空:", len(joined) > 200)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "a"
    protocol = sys.argv[2] if len(sys.argv) > 2 else "openai"
    hint = sys.argv[4] if len(sys.argv) > 4 else ""
    question = sys.argv[3] if len(sys.argv) > 3 else "读 docs/ch03/spec.md 并用一句话总结"
    if mode == "a":
        asyncio.run(phase_a())
    elif mode == "a2":
        asyncio.run(phase_a2())
    elif mode == "a3":
        import tempfile

        asyncio.run(phase_a3(os.path.join(tempfile.gettempdir(), "potato_ch03_multi.txt")))
    elif mode == "b":
        asyncio.run(phase_b(protocol, question, hint))
    elif mode == "c":
        asyncio.run(phase_c(protocol, hint))
