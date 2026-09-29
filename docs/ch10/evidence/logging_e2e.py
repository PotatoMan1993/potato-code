"""ch10 端到端证据脚本：在真实启动路径上验证日志落盘。

与单测的区别：这里跑的是**真实的**启动链路——真实的会话创建、真实的日志装配、真实的
配置解析、真实的 MCP 连接尝试、真实的权限引擎与运行时构造、真实的上下文压缩入口。
只有 TUI 的 run_async 被替换为空实现（脚本要在无 TTY 的环境里验完启动全链路），
以及配置非法那一场景走真实子进程以取得真实退出码。

  S1  正常启动 → 会话目录下出现 session.log，含启动记录与信息级记录
  S2  配置非法 → 真实子进程退出码为 1，且该会话日志记录了配置错误
  S3  压缩失败 → 日志中出现含完整堆栈的异常级记录
  S4  MCP 连不上 → 日志中出现该 server 的连接失败告警
  S5  详细度提升 → 默认级别下调试级记录不出现，设环境变量后出现
  S6  两个会话各写各的 → 各自日志不含对方的会话 id

用法：
    uv run python docs/ch10/evidence/logging_e2e.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from potatocode import cli
from potatocode.compact import (
    CompactCircuitBreaker,
    ContentReplacementState,
    ManageInput,
    RecoveryState,
    TriggerKind,
    manage_context,
    new_session_context,
)
from potatocode.conversation import Conversation
from potatocode.llm import Request, StreamEvent
from potatocode.logging_setup import LEVEL_ENV

# 一份可用的 provider 配置（不需要联网，启动链路只解析不请求）
VALID_CONFIG = """\
providers:
  - name: 本地脚本provider
    protocol: anthropic
    api_key: 占位密钥
    model: 占位模型
"""

# 必然连不上的 MCP server：命令不存在，stdio 传输在拉起进程时就失败
BOGUS_MCP = """\
mcp_servers:
  unreachable:
    type: stdio
    command: definitely-not-a-real-command-xyz
"""


class _StubApp:
    """TUI 替身：接收与真实 App 相同的构造参数，run_async 里只落两条自有日志。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        """忽略全部构造参数——启动链路的验证在构造它之前就已完成。"""

    async def run_async(self) -> None:
        """代替 TUI 主循环：记信息级与调试级各一条，并模拟依赖库打日志。"""
        demo = logging.getLogger("potatocode.demo")
        demo.info("本轮会话已就绪")
        demo.debug("这条只在提升详细度后才应出现")
        # 模拟依赖库（LLM SDK / HTTP 客户端）在真实运行中打日志：应被隔离在文件之外
        logging.getLogger("httpx").warning("依赖库告警：不应出现在会话日志里")
        logging.getLogger("anthropic._client").debug("依赖库调试：不应出现在会话日志里")


class _FailingProvider:
    """永远抛异常的 provider：用来触发真实的上下文压缩失败路径。"""

    @property
    def name(self) -> str:
        """provider 名称（协议无关接口要求）。"""
        return "failing"

    @property
    def model(self) -> str:
        """模型名（协议无关接口要求）。"""
        return "failing-model"

    async def stream(self, req: Request) -> AsyncIterator[StreamEvent]:
        """摘要请求一律炸掉，异常沿 force_compact 上抛到 manage_context。"""
        raise RuntimeError("摘要请求被拒：prompt_too_long")
        yield StreamEvent(done=True)  # 不可达：使本函数成为异步生成器


def _check(name: str, ok: bool, detail: object = "") -> bool:
    """打印一条证据检查结果，返回是否通过。"""
    suffix = f"  {detail}" if detail else ""
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{suffix}")
    return ok


def _make_workspace(prefix: str, *, valid_config: bool = True, mcp: bool = False) -> Path:
    """建一个临时工作区，按需放入 provider 配置与 MCP 配置。"""
    ws = Path(tempfile.mkdtemp(prefix=f"potatocode-ch10-{prefix}-"))
    if valid_config:
        cfg = ws / ".potatocode" / "config.yaml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(VALID_CONFIG, encoding="utf-8")
    if mcp:
        (ws / ".potatocode.yaml").write_text(BOGUS_MCP, encoding="utf-8")
    return ws


def _session_log(ws: Path) -> Path | None:
    """取该工作区里最新一个会话的日志文件路径。"""
    logs = sorted((ws / ".potatocode" / "sessions").glob("*/session.log"))
    return logs[-1] if logs else None


async def _run_cli(ws: Path, level: str | None) -> int:
    """在指定工作区跑一次真实启动流程（TUI 用替身），返回退出码。"""
    cwd = os.getcwd()
    original_app = cli.PotatoCodeApp
    os.chdir(ws)
    cli.PotatoCodeApp = _StubApp  # type: ignore[assignment]  # 无 TTY 环境下替换界面
    if level is None:
        os.environ.pop(LEVEL_ENV, None)
    else:
        os.environ[LEVEL_ENV] = level
    try:
        return await cli._amain()
    finally:
        cli.PotatoCodeApp = original_app  # type: ignore[assignment]
        os.chdir(cwd)


def _run_cli_subprocess(ws: Path) -> subprocess.CompletedProcess[str]:
    """用真实子进程跑一次 CLI（配置非法时它在进入 TUI 之前就退出，无需 TTY）。"""
    return subprocess.run(
        [sys.executable, "-m", "potatocode"],
        cwd=ws,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


async def _scenario_s3(ws: Path) -> str:
    """走真实的压缩入口触发一次失败，返回该会话日志的文本。"""
    session = new_session_context(str(ws))
    from potatocode.logging_setup import close_logging, setup_logging

    handler = setup_logging(session.log_path)
    try:
        conv = Conversation()
        conv.add_user("请读一下 a.txt")
        in_ = ManageInput(
            conv=conv,
            provider=_FailingProvider(),  # type: ignore[arg-type]
            context_window=200000,
            tool_defs=[],
            replacement=ContentReplacementState(),
            recovery=RecoveryState(),
            auto_tracking=CompactCircuitBreaker(),
            session=session,
            usage_anchor=0,
            anchor_msg_len=0,
            estimated_token=0,
            trigger=TriggerKind.MANUAL,  # 手动路径直通摘要，跳过阈值检查
        )
        try:
            await manage_context(in_)
        except Exception:
            pass  # 压缩失败会上抛，这里要验的正是它留下的日志
    finally:
        close_logging(handler)
    return Path(session.log_path).read_text(encoding="utf-8")


async def main() -> int:
    """依序跑完全部场景并打印结果。"""
    results: list[bool] = []

    # ---- S1 + S5：正常启动落盘，以及详细度过滤 ----
    ws_default = _make_workspace("s1")
    code = await _run_cli(ws_default, None)
    log_default = _session_log(ws_default)
    text_default = log_default.read_text(encoding="utf-8") if log_default else ""

    results.append(
        _check(
            "S1 正常启动后会话目录下出现 session.log",
            log_default is not None and log_default.is_file(),
            log_default,
        )
    )
    results.append(
        _check(
            "S1 日志含启动记录（会话 id + 工作目录）",
            "启动：" in text_default and "session=" in text_default,
        )
    )
    results.append(
        _check(
            "S1 退出码为 0，且信息级记录已落盘",
            code == 0 and "本轮会话已就绪" in text_default,
            f"exit={code}",
        )
    )
    results.append(
        _check(
            "S1 文件尾部完整成行（退出前已刷盘）",
            text_default.endswith("\n") and text_default.strip() != "",
        )
    )

    ws_debug = _make_workspace("s5")
    await _run_cli(ws_debug, "DEBUG")
    log_debug = _session_log(ws_debug)
    text_debug = log_debug.read_text(encoding="utf-8") if log_debug else ""

    results.append(
        _check(
            "S5 默认详细度下调试级记录不出现",
            "这条只在提升详细度后才应出现" not in text_default,
        )
    )
    results.append(
        _check(
            "S5 设环境变量后同一记录出现",
            "这条只在提升详细度后才应出现" in text_debug,
        )
    )

    # ---- AC5：真实运行中依赖库的日志被隔离在文件之外 ----
    # 行格式：时间 级别 来源模块:行号 — 消息，取第 4 列的模块名
    sources = {
        line.split()[3].rsplit(":", 1)[0]
        for line in text_debug.splitlines()
        if len(line.split()) >= 4
    }
    results.append(
        _check(
            "AC5 依赖库日志未进入文件（连调试级下也没有）",
            "依赖库告警" not in text_debug and "依赖库调试" not in text_debug,
        )
    )
    results.append(
        _check(
            "AC5 文件内全部记录都来自本项目包",
            bool(sources) and all(s.startswith("potatocode.") for s in sources),
            f"来源={sorted(sources)}",
        )
    )

    # ---- S2：配置非法，走真实子进程取真实退出码 ----
    ws_s2 = _make_workspace("s2", valid_config=False)
    proc = _run_cli_subprocess(ws_s2)
    log_s2 = _session_log(ws_s2)
    text_s2 = log_s2.read_text(encoding="utf-8") if log_s2 else ""

    results.append(
        _check("S2 配置非法时退出码为 1", proc.returncode == 1, f"exit={proc.returncode}")
    )
    results.append(
        _check("S2 终端仍给出可读提示", "配置错误" in proc.stderr, repr(proc.stderr[:60]))
    )
    results.append(
        _check(
            "S2 该会话日志同时记录了配置错误",
            log_s2 is not None and "配置错误" in text_s2,
        )
    )

    # ---- S3：压缩失败留堆栈 ----
    ws_s3 = _make_workspace("s3")
    text_s3 = await _scenario_s3(ws_s3)
    results.append(
        _check(
            "S3 压缩失败的完整堆栈已落盘",
            "上下文压缩失败" in text_s3 and "Traceback" in text_s3 and "prompt_too_long" in text_s3,
        )
    )

    # ---- S4：MCP 连不上留告警 ----
    ws_s4 = _make_workspace("s4", mcp=True)
    await _run_cli(ws_s4, None)
    log_s4 = _session_log(ws_s4)
    text_s4 = log_s4.read_text(encoding="utf-8") if log_s4 else ""
    results.append(
        _check(
            "S4 MCP 连接失败告警进入会话日志",
            "connect server unreachable failed" in text_s4,
        )
    )

    # ---- S6：两个会话各写各的 ----
    ws_a = _make_workspace("s6a")
    ws_b = _make_workspace("s6b")
    await _run_cli(ws_a, None)
    await _run_cli(ws_b, None)
    log_a, log_b = _session_log(ws_a), _session_log(ws_b)
    id_a = log_a.parent.name if log_a else ""
    id_b = log_b.parent.name if log_b else ""
    text_b = log_b.read_text(encoding="utf-8") if log_b else ""
    text_a = log_a.read_text(encoding="utf-8") if log_a else ""
    results.append(
        _check(
            "S6 两个会话的日志互相隔离",
            bool(id_a) and bool(id_b) and id_a != id_b and id_a not in text_b and id_b not in text_a,
            f"{id_a} / {id_b}",
        )
    )

    print(f"\n通过 {sum(results)}/{len(results)} 项")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
