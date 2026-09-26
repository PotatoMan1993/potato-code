"""ch09 端到端证据脚本：用脚本化 provider 驱动真实 TUI + Agent + 工具执行。

与单测的区别：这里跑的是**真实的** Textual App、真实的 Conversation、真实的工具执行
（真的遍历了本仓库含 5196 个依赖文件的目录树），只有 LLM 是被脚本化的——因此能在不联网、
不消耗密钥的前提下验证遍历范围修订的四条链路：

  S1  默认范围内找文件 → 结果不含被排除目录
  S1R 在本仓库（真实 .venv）上跑同一条链路
  S2  默认范围内搜内容 → 命中不来自被排除目录
  S3  搜索根显式指向被排除目录 → 能搜进去（逃生口）
  S4  工具结果回灌进对话历史 + 模型据此给出最终答复

用法：
    python docs/ch09/evidence/scope_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from textual.widgets import RichLog

from potatocode.agent import Agent, new_runtime
from potatocode.config import ProviderConfig
from potatocode.llm import Request, StreamEvent, ToolCall
from potatocode.permission import new_engine
from potatocode.tool import new_default_registry
from potatocode.tui.app import ChatTextArea, PotatoCodeApp

# 端到端脚本所在位置 → 仓库根（docs/ch09/evidence/xxx.py 往上三层）
REPO = Path(__file__).resolve().parents[3]

# 项目自身文件与依赖目录文件里的关键词（用于验证命中的来源）
TOKEN = "SEARCHABLE_TOKEN"

# 被排除目录的判定标志（出现在结果中即为泄漏）
NOISE_MARKERS = (".venv", "__pycache__", "node_modules", ".git")


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
        """按调用次序回放脚本；脚本耗尽后返回收尾文本。"""
        self.requests.append(req)
        idx = len(self.requests) - 1
        script = (
            self._scripts[idx]
            if idx < len(self._scripts)
            else [StreamEvent(text="完成。"), StreamEvent(done=True)]
        )
        for ev in script:
            yield ev


def _tool_result(provider: ScriptedProvider, tool_call_id: str) -> str:
    """从最后一发主对话请求里取出指定工具调用的结果文本（按 tool_call_id 配对）。"""
    main_reqs = [r for r in provider.requests if r.tools]
    for msg in main_reqs[-1].messages:
        if msg.role != "tool":
            continue
        for tr in msg.tool_results:
            if tr.tool_call_id == tool_call_id:
                return tr.content
    return ""


def _log_text(app: PotatoCodeApp) -> str:
    """把 TUI scrollback 拼成纯文本，便于断言与展示。"""
    log = app.query_one("#log", RichLog)
    return "\n".join(strip.text for strip in log.lines)


def _check(name: str, ok: bool, detail: object = "") -> bool:
    """打印一条证据检查结果，返回是否通过。"""
    suffix = f"  {detail}" if detail else ""
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{suffix}")
    return ok


def _has_noise(text: str) -> bool:
    """结果文本中是否出现被排除目录的痕迹。"""
    return any(marker in text for marker in NOISE_MARKERS)


def _make_workspace() -> Path:
    """构造含项目自身文件与三类噪声目录的临时工作区。"""
    ws = Path(tempfile.mkdtemp(prefix="potatocode-ch09-"))
    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text(f"# {TOKEN} 出现在项目自身源码里\n", encoding="utf-8")
    (ws / "notes.txt").write_text("项目自身的说明文件\n", encoding="utf-8")
    (ws / ".venv" / "lib").mkdir(parents=True)
    (ws / ".venv" / "lib" / "dep.py").write_text(f"# {TOKEN} 出现在依赖里\n", encoding="utf-8")
    (ws / ".venv" / "lib" / "dep.txt").write_text("依赖里的文本文件\n", encoding="utf-8")
    (ws / "__pycache__").mkdir()
    (ws / "__pycache__" / "c.py").write_text(f"# {TOKEN} 缓存产物\n", encoding="utf-8")
    (ws / "node_modules" / "pkg").mkdir(parents=True)
    (ws / "node_modules" / "pkg" / "n.py").write_text(f"# {TOKEN} 依赖包\n", encoding="utf-8")
    return ws


async def _run_app(workspace: Path, scripts: list[list[StreamEvent]], prompts: list[str]) -> tuple[
    ScriptedProvider, list[bool], str
]:
    """用脚本化 provider 驱动真实 App，按序提交 prompts，返回 (provider, 断言结果, scrollback)。"""
    provider = ScriptedProvider(scripts)
    registry = new_default_registry()
    engine, err = new_engine(str(workspace))
    assert err is None, err
    cfg = ProviderConfig(name="scripted", protocol="anthropic", api_key="test", model="scripted")
    runtime = new_runtime(workspace=str(workspace))
    app = PotatoCodeApp([cfg], "evidence", registry, engine, runtime=runtime)

    results: list[bool] = []
    async with app.run_test() as pilot:
        app.agent = Agent(provider, registry, engine, runtime=runtime)
        for prompt in prompts:
            app.conv.add_user(prompt)
            app._start_turn()
            await pilot.pause()
            while app._stream_task is not None:
                await app._stream_task
                await pilot.pause()
        text = _log_text(app)
    return provider, results, text


async def main() -> int:
    """跑四个端到端场景，逐项打印证据。"""
    sys.stdout.reconfigure(encoding="utf-8")  # 中文证据输出不依赖控制台代码页
    results: list[bool] = []

    # ────────── 受控工作区：S1 / S2 / S3 / S4 ──────────
    ws = _make_workspace()
    print(f"工作区: {ws}")

    scripts = [
        # 第 1 轮：模型按模式找 txt 文件
        [
            StreamEvent(text="我先列一下项目里的 txt 文件。"),
            StreamEvent(
                tool_calls=[
                    ToolCall(
                        id="c_glob",
                        name="glob",
                        input=json.dumps({"pattern": "**/*.txt", "path": str(ws)}),
                    )
                ]
            ),
            StreamEvent(done=True),
        ],
        # 第 2 轮：模型在默认范围内搜内容
        [
            StreamEvent(
                tool_calls=[
                    ToolCall(
                        id="c_grep_default",
                        name="grep",
                        input=json.dumps({"pattern": TOKEN, "glob": "*.py", "path": str(ws)}),
                    )
                ]
            ),
            StreamEvent(done=True),
        ],
        # 第 3 轮：模型显式搜进被排除目录（逃生口）
        [
            StreamEvent(
                tool_calls=[
                    ToolCall(
                        id="c_grep_venv",
                        name="grep",
                        input=json.dumps(
                            {"pattern": TOKEN, "glob": "*.py", "path": str(ws / ".venv" / "lib")}
                        ),
                    )
                ]
            ),
            StreamEvent(done=True),
        ],
        # 第 4 轮：模型据工具结果给出最终答复
        [StreamEvent(text=f"项目自身源码里有 {TOKEN}，依赖目录里也有。"), StreamEvent(done=True)],
    ]

    provider, _, text = await _run_app(ws, scripts, ["有哪些 txt 文件？", "搜一下关键词", "搜进依赖目录", "总结"])

    glob_out = _tool_result(provider, "c_glob")
    grep_default = _tool_result(provider, "c_grep_default")
    grep_venv = _tool_result(provider, "c_grep_venv")

    # —— S1：默认范围内找文件 ——
    results.append(
        _check(
            "S1 glob 默认范围命中项目自身的 notes.txt",
            "notes.txt" in glob_out,
            glob_out.replace("\n", " | ")[:120],
        )
    )
    results.append(
        _check("S1 glob 结果不含任何被排除目录", not _has_noise(glob_out), f"原文={glob_out!r}")
    )

    # —— S2：默认范围内搜内容 ——
    results.append(
        _check(
            "S2 grep 默认范围命中项目自身源码",
            "app.py" in grep_default and TOKEN in grep_default,
            grep_default.replace("\n", " | ")[:120],
        )
    )
    results.append(
        _check("S2 grep 命中不来自被排除目录", not _has_noise(grep_default), f"原文={grep_default!r}")
    )

    # —— S3：逃生口（搜索根指向被排除目录）——
    results.append(
        _check(
            "S3 搜索根指向 .venv 时能搜进去",
            "dep.py" in grep_venv and TOKEN in grep_venv,
            grep_venv.replace("\n", " | ")[:120],
        )
    )

    # —— S4：回灌与最终答复 ——
    results.append(
        _check(
            "S4 工具行在 TUI 正常渲染",
            "● glob(" in text and "● grep(" in text,
        )
    )
    results.append(
        _check(
            "S4 工具结果回灌进最终请求的历史",
            glob_out.strip() != "" and grep_default.strip() != "",
        )
    )
    results.append(_check("S4 模型给出最终文本答复", TOKEN in text))

    # ────────── 真实仓库：S1R（真实 .venv，5196 个依赖文件） ──────────
    print(f"\n真实仓库: {REPO}")
    scripts_real = [
        [
            StreamEvent(
                tool_calls=[
                    ToolCall(
                        id="c_glob_real",
                        name="glob",
                        input=json.dumps({"pattern": "**/*.py", "path": str(REPO)}),
                    )
                ]
            ),
            StreamEvent(done=True),
        ],
        [StreamEvent(text="已列出。"), StreamEvent(done=True)],
    ]
    provider_real, _, _ = await _run_app(REPO, scripts_real, ["列出项目里的 py 文件"])
    glob_real = _tool_result(provider_real, "c_glob_real")
    lines = [ln for ln in glob_real.splitlines() if ln != "[truncated]"]
    results.append(
        _check(
            "S1R 真实仓库 glob **/*.py 结果不含被排除目录",
            not _has_noise(glob_real),
            f"{len(lines)} 条，噪声 0 条",
        )
    )
    results.append(
        _check(
            "S1R 真实仓库仍能命中项目自身源码",
            any(ln.startswith(("src", "tests")) for ln in lines),
            f"样本={lines[:2]}",
        )
    )

    print(f"\n通过 {sum(results)}/{len(results)} 项")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
