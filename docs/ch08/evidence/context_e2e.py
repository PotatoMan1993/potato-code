"""ch08 端到端证据脚本：用脚本化 provider 驱动真实 TUI + Agent + compact 全链路。

与单测的区别：这里跑的是**真实的** Textual App、真实的 Conversation、真实的工具执行
（真的读了一个 60KB 文件）、真实的落盘目录与 TUI 视图层，只有 LLM 是被脚本化的——
因此能在不联网、不消耗密钥的前提下验证：

  E2  单条大工具结果 → 落盘 + 对话内替换为预览体
  E5  手动 /compact → 系统消息 + 会话被替换
  E12 会话目录创建、/unknown 友好提示、命令不入对话历史
  E13 自动压缩的 TUI 状态提示（"正在压缩上下文..." → "已压缩，token 从 X 降至 Y"）

用法：
    python docs/ch08/evidence/context_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from textual.widgets import RichLog

from potatocode.agent import Agent, CompactEvent, CompactPhase, Event, new_runtime
from potatocode.config import ProviderConfig
from potatocode.conversation import Conversation
from potatocode.llm import Request, StreamEvent, ToolCall, Usage
from potatocode.permission import Mode, new_engine
from potatocode.tool import new_default_registry
from potatocode.tui.app import ChatTextArea, PotatoCodeApp

# 摘要返回：两阶段标签 + 9 部分结构
SUMMARY_TEXT = (
    "<analysis>草稿</analysis><summary>"
    "## 1 主要请求和意图\n读取 big.txt 并汇报。\n"
    "## 2 关键技术概念\n上下文压缩。\n## 3 文件和代码段\nbig.txt\n"
    "## 4 错误和修复\n无\n## 5 问题解决过程\n先落盘再摘要。\n"
    "## 6 所有用户消息原文\n读一下 big.txt\n## 7 待办任务\n无\n"
    "## 8 当前工作\n正在验证上下文管理。\n## 9 可能的下一步\n收尾。"
    "</summary>"
)


class ScriptedProvider:
    """按脚本返回事件的 provider，并记录每次请求供断言。"""

    def __init__(self, scripts: list[list[StreamEvent]]) -> None:
        self._scripts = scripts
        self.requests: list[Request] = []

    @property
    def name(self) -> str:
        """provider 名称。"""
        return "scripted"

    @property
    def model(self) -> str:
        """模型名。"""
        return "scripted-model"

    async def stream(self, req: Request) -> AsyncIterator[StreamEvent]:
        """按调用次序回放脚本；摘要请求（无工具）恒返回摘要文本。"""
        self.requests.append(req)
        idx = len(self.requests) - 1
        script = (
            self._scripts[idx]
            if idx < len(self._scripts)
            else [StreamEvent(text=SUMMARY_TEXT), StreamEvent(done=True)]
        )
        if not req.tools:  # 摘要请求：直接给结构化摘要
            script = [StreamEvent(text=SUMMARY_TEXT), StreamEvent(done=True)]
        for ev in script:
            yield ev


def _log_text(app: PotatoCodeApp) -> str:
    """把 TUI scrollback 拼成纯文本，便于断言与展示。"""
    log = app.query_one("#log", RichLog)
    return "\n".join(strip.text for strip in log.lines)


def _check(name: str, ok: bool, detail: object = "") -> bool:
    """打印一条证据检查结果，返回是否通过。"""
    suffix = f"  {detail}" if detail else ""
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{suffix}")
    return ok


async def main() -> int:
    """在临时工作目录里跑完整链路，逐项打印证据。"""
    sys.stdout.reconfigure(encoding="utf-8")  # 中文证据输出不依赖控制台代码页
    workspace = Path(tempfile.mkdtemp(prefix="potatocode-ch08-"))
    big = workspace / "big.txt"
    big.write_text("".join(f"{i} 这是一行用于撑大上下文的内容\n" for i in range(2200)), encoding="utf-8")
    print(f"工作目录: {workspace}")
    print(f"big.txt 字节数: {big.stat().st_size}")

    provider = ScriptedProvider(
        [
            # 第 1 轮：请求读取 big.txt
            [
                StreamEvent(text="我先读一下 big.txt。"),
                StreamEvent(
                    tool_calls=[
                        ToolCall(
                            id="call_big",
                            name="read_file",
                            input=json.dumps({"path": str(big)}),
                        )
                    ]
                ),
                StreamEvent(usage=Usage(input_tokens=900, output_tokens=100)),
                StreamEvent(done=True),
            ],
            # 第 2 轮：读完后收尾
            [StreamEvent(text="读完了。"), StreamEvent(usage=Usage(input_tokens=1200, output_tokens=80)), StreamEvent(done=True)],
        ]
    )

    registry = new_default_registry()
    engine, err = new_engine(str(workspace))
    assert err is None, err
    cfg = ProviderConfig(name="scripted", protocol="anthropic", api_key="test", model="scripted")
    runtime = new_runtime(workspace=str(workspace))
    runtime.context_window = 40000  # 阈值 7000：便于在十几轮内观察自动压缩
    app = PotatoCodeApp([cfg], "evidence", registry, engine, runtime=runtime)

    results: list[bool] = []
    async with app.run_test() as pilot:
        agent = Agent(provider, registry, engine, runtime=runtime)
        app.agent = agent
        conv = app.conv

        # —— E12：会话目录在启动期创建 ——
        results.append(
            _check("E12 会话目录存在", Path(runtime.session.spill_dir).is_dir(), runtime.session.spill_dir)
        )

        # —— E2：读大文件 → 落盘 + 预览体替换 ——
        conv.add_user("读一下 big.txt")
        app._start_turn()
        await pilot.pause()
        while app._stream_task is not None:
            await app._stream_task
            await pilot.pause()

        spilled = sorted(p.name for p in Path(runtime.session.spill_dir).iterdir())
        results.append(_check("E2 大工具结果已落盘", spilled == ["call_big"], f"文件={spilled}"))
        spill_file = Path(runtime.session.spill_dir) / "call_big"
        results.append(
            _check(
                "E2 落盘内容是工具结果原文（带行号，大于裸文件）",
                spill_file.exists()
                and spill_file.stat().st_size >= big.stat().st_size
                and spill_file.read_bytes().startswith(b"     1\t"),  # read_file 的行号前缀
                f"{spill_file.stat().st_size} 字节 vs 裸文件 {big.stat().st_size} 字节",
            )
        )
        # 最后一发主对话请求里该工具结果已被替换成预览体
        main_reqs = [r for r in provider.requests if r.tools]
        tool_msg = [m for m in main_reqs[-1].messages if m.role == "tool"][0]
        preview = tool_msg.tool_results[0].content
        results.append(
            _check(
                "E2 对话内已替换为预览体",
                "original size:" in preview and "[saved to]" in preview and "文件读取工具" in preview,
            )
        )
        results.append(_check("E2 原文未被塞进请求", len(preview) < 4000, f"预览体 {len(preview)} 字节"))
        declared = re.search(r"original size: (\d+) bytes", preview)
        results.append(
            _check(
                "E2 预览体声明的原始字节数与落盘文件一致",
                declared is not None
                and int(declared.group(1)) == spill_file.stat().st_size,
                declared.group(1) if declared else "未找到声明",
            )
        )
        results.append(
            _check(
                "E4 read_file 已记入文件追踪",
                any(r.path == str(big.resolve()) for r in runtime.recovery.snapshot()),
            )
        )

        # —— E13：手动 /compact 的 TUI 提示（手动路径文案与自动共用同一格式）——
        app.query_one("#input", ChatTextArea).insert("/compact")
        await pilot.press("enter")
        while app.compact_task is not None and not app.compact_task.done():
            await app.compact_task
            await pilot.pause()
        text_after_compact = _log_text(app)
        results.append(
            _check(
                "E5 手动压缩提示 token 变化",
                "已压缩，token 从" in text_after_compact,
                [ln for ln in text_after_compact.splitlines() if "已压缩" in ln][:1],
            )
        )
        results.append(
            _check(
                "E5 会话被替换为摘要开头",
                "## 历史会话摘要" in conv.messages()[0].content,
                f"会话现有 {conv.length()} 条",
            )
        )

        # —— E12：未知命令友好提示，不入对话 ——
        before_len = conv.length()
        app.query_one("#input", ChatTextArea).insert("/unknown")
        await pilot.press("enter")
        await pilot.pause()
        results.append(
            _check("E12 未知命令提示", "未知命令: /unknown" in _log_text(app))
        )
        results.append(_check("E12 命令未写入对话", conv.length() == before_len))

        # —— E13：自动压缩的 TUI 状态提示（用脚本事件直接驱动视图层）——
        auto_events = [
            Event(compact=CompactEvent(phase=CompactPhase.BEFORE_AUTO)),
            Event(compact=CompactEvent(phase=CompactPhase.AFTER_AUTO, before=167000, after=12000)),
            Event(text="压缩后继续。"),
            Event(done=True),
        ]

        class _EventOnlyAgent:
            """只回放事件流的假 Agent，用于验证 TUI 对压缩事件的分派。"""

            async def run(self, conv, mode, cancel):
                for ev in auto_events:
                    yield ev

        app.agent = _EventOnlyAgent()  # type: ignore[assignment]
        app._start_turn()
        while app._stream_task is not None:
            await app._stream_task
            await pilot.pause()
        text_auto = _log_text(app)
        results.append(_check("E13 自动压缩中提示", "正在压缩上下文..." in text_auto))
        results.append(
            _check(
                "E13 自动压缩完成提示",
                "已压缩，token 从 167000 降至 12000" in text_auto,
            )
        )

        # —— E12：命令路径不触发普通对话请求 ——
        main_before = len([r for r in provider.requests if r.tools])
        app.query_one("#input", ChatTextArea).insert("/exit")
        await pilot.press("enter")
        await pilot.pause()
        results.append(
            _check(
                "E12 /exit 未发普通对话请求",
                len([r for r in provider.requests if r.tools]) == main_before,
            )
        )

    print(f"\n通过 {sum(results)}/{len(results)} 项")
    print("会话目录保留（进程退出不清理）:", runtime.session.spill_dir)
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
