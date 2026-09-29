"""ch11 端到端证据脚本：在真实链路上验证项目指令 / 会话存档 / 会话恢复 / 自动记忆。

与单测的区别：这里跑的是**真实**链路——真实的 provider 请求、真实的 TUI 事件循环
（Textual 无头驱动，与 `python -m potatocode` 同一条装配路径）、真实的 JSONL 落盘与
恢复、真实的 LLM 记忆提炼。单测用 fake provider 覆盖分支，本脚本覆盖"装起来真能用"。

  场景 1  冷启动 + 指令注入：MEWCODE.md 的约束在模型回复里生效；JSONL 落盘且首行带 model
  场景 2  自动记忆：显式"记住"请求触发异步提炼，笔记与索引行落地
  场景 3  会话恢复：新进程 `/resume` → 列表 → 恢复历史 → 新消息追加进同一存档
  场景 4  工具与压缩：工具调用与结果进 JSONL；压缩标记落盘，恢复从最后一个标记之后开始

用法：
    uv run python docs/ch11/evidence/session_e2e.py [--provider N]

需要 `.potatocode/config.yaml` 中至少有一个可用 provider：脚本会逐个探测，默认取第一个
能正常响应的。所有落盘（会话、记忆、用户级指令）都在临时目录里，不碰工作区与用户主目录。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from potatocode import config as pconfig
from potatocode.agent import new_runtime
from potatocode.compact import sessions_root
from potatocode.instructions import Loader
from potatocode.llm import Message, Request, System, new_provider
from potatocode.memory import Manager, project_memory_dir
from potatocode.permission import Outcome, new_engine
from potatocode.session import Writer, load_session
from potatocode.tool import new_default_registry
from potatocode.tui.app import ChatTextArea, PotatoCodeApp
from potatocode.tui.resume import ResumeSelect
from potatocode.tui.view import SessionState

# 仓库根：本文件在 docs/ch11/evidence/ 下
REPO = Path(__file__).resolve().parents[3]

# 项目级指令：用一条容易观察的约束，验证它确实进了系统提示并被模型遵循
MEWCODE = "所有回复必须以「喵~」开头。\n"

_results: list[tuple[str, bool, str]] = []


def _check(name: str, ok: bool, detail: str = "") -> bool:
    """记录并打印一项检查结果。"""
    _results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _section(title: str) -> None:
    """打印场景分隔标题。"""
    print(f"\n=== {title} ===")


async def _discover_provider(forced: int | None):
    """按配置顺序探测可用 provider，返回 (ProviderConfig, Provider)；都不行返回 (None, None)。"""
    try:
        cfg = pconfig.load(str(REPO / ".potatocode" / "config.yaml"))
    except Exception as e:
        print(f"读取 .potatocode/config.yaml 失败：{type(e).__name__}: {e}")
        return None, None
    candidates = [cfg.providers[forced]] if forced is not None else list(cfg.providers)
    for c in candidates:
        provider = new_provider(c)
        try:
            probe = Request(
                messages=[Message(role="user", content="回复 ok")],
                tools=None,
                system=System(stable="只回复 ok"),
            )
            got = ""
            async for ev in provider.stream(probe):
                if ev.err is not None:
                    raise ev.err
                got += ev.text
            if got:
                print(f"使用 provider: {c.name} ({c.protocol} · {c.model})")
                return c, provider
        except Exception as e:
            print(f"跳过 provider {c.name}：{type(e).__name__}: {str(e)[:80]}")
    return None, None


class Harness:
    """把 cli.py 的装配流程搬到临时工作区，逐次构造一次"进程启动"。"""

    def __init__(self, workspace: Path, cfg, user_home: Path) -> None:
        self.workspace = workspace
        self.cfg = cfg
        self.user_home = user_home
        self.instructions = ""
        self.mem_index = ""

    def load_startup_inputs(self) -> None:
        """模拟 cli.py 启动早期的指令与记忆加载。"""
        self.instructions = Loader(str(self.workspace), str(self.user_home)).load()
        mgr = Manager(project_memory_dir(str(self.workspace)), str(self.user_home / "mem"))
        self.mem_index = mgr.load_index()

    def new_app(self) -> PotatoCodeApp:
        """构造一个带存档写入器的 App（与 cli.py 的入参一致）。"""
        engine, err = new_engine(str(self.workspace))
        assert err is None, err
        runtime = new_runtime(workspace=str(self.workspace))
        writer = Writer(runtime.session.session_dir, model="")
        mem_mgr = Manager(project_memory_dir(str(self.workspace)), str(self.user_home / "mem"))
        app = PotatoCodeApp(
            [self.cfg],
            "e2e",
            new_default_registry(),
            engine,
            None,
            runtime=runtime,
            writer=writer,
            mem_mgr=mem_mgr,
            instruction_text=self.instructions,
            memory_text=self.mem_index,
            sessions_dir=sessions_root(str(self.workspace)),
            workspace=str(self.workspace),
        )
        return app


async def _submit(pilot, app: PotatoCodeApp, text: str) -> None:
    """在输入框键入并回车提交。"""
    app.query_one("#input", ChatTextArea).insert(text)
    await pilot.press("enter")
    await pilot.pause()


async def _wait_idle(pilot, app: PotatoCodeApp, timeout: float = 300.0) -> None:
    """等本轮跑完；人在回路请求按"允许本次"放行（脚本不测权限，测的是存档与记忆）。"""
    start = time.monotonic()
    while app.state is not SessionState.IDLE:
        if time.monotonic() - start > timeout:
            raise TimeoutError(f"回合未结束，state={app.state}")
        if app.state is SessionState.APPROVING and app.pending is not None:
            app.pending.respond.set_result(Outcome.ALLOW_ONCE)
        await pilot.pause(0.3)


def _journal(session_dir: str | Path) -> list[dict]:
    """读回某个会话的存档行。"""
    text = (Path(session_dir) / "conversation.jsonl").read_text(encoding="utf-8")
    return [json.loads(x) for x in text.splitlines() if x.strip()]


def _index_of(select: ResumeSelect, session_id: str) -> int | None:
    """在列表里按下标找出目标会话（只用公开接口）。"""
    for i in range(select.shown_count):
        item = select.session_at(i)
        if item is not None and item.id == session_id:
            return i
    return None


async def _open_resume(pilot, app: PotatoCodeApp) -> ResumeSelect | None:
    """输入 `/resume` 并等列表就绪；未进入列表态返回 None。"""
    await _submit(pilot, app, "/resume")
    if app.state is not SessionState.RESUMING:
        return None
    return app.query_one(ResumeSelect)


async def _pick(pilot, app: PotatoCodeApp, select: ResumeSelect, session_id: str) -> bool:
    """用上下键把高亮移到目标会话后回车；返回是否真的切进了该会话。"""
    index = _index_of(select, session_id)
    if index is None:
        return False
    # 列表默认停在最新一项，这里按真实交互逐格移动
    step = index - (select.highlighted or 0)
    key = "down" if step > 0 else "up"
    for _ in range(abs(step)):
        await pilot.press(key)
        await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    return app.runtime.session.session_id == session_id


def _assistant_text(conv) -> str:
    """把对话里的全部 assistant 文本拼起来。"""
    return "".join(m.content for m in conv.messages() if m.role == "assistant")


async def scenario_1(harness: Harness) -> tuple[str, int]:
    """场景 1：冷启动 + 指令注入 + JSONL 落盘。"""
    _section("场景 1（冷启动 + 项目指令 + 会话存档）")
    app = harness.new_app()
    session_id = app.runtime.session.session_id
    print(f"  会话 id: {session_id}")
    async with app.run_test(size=(110, 40)) as pilot:
        app.query_one(ChatTextArea).focus()
        await _submit(pilot, app, "你好，请用一句话自我介绍")
        await _wait_idle(pilot, app)
        reply = _assistant_text(app.conv)
        _check("MEWCODE.md 约束生效（回复以「喵~」开头）", "喵" in reply[:20], repr(reply[:24]))

        lines = _journal(app.runtime.session.session_dir)
        _check("JSONL 至少 user + assistant 两行", len(lines) >= 2, f"{len(lines)} 行")
        _check("首行带 model 字段", bool(lines[0].get("model")), str(lines[0].get("model")))
        _check(
            "每行可解析且带 role / ts",
            all(row.get("role") and row.get("ts") for row in lines),
        )
    return session_id, len(lines)


async def scenario_2(harness: Harness) -> None:
    """场景 2：显式记忆请求触发异步提炼。"""
    _section("场景 2（自动记忆：显式请求触发提炼）")
    app = harness.new_app()
    session_dir = app.runtime.session.session_dir
    async with app.run_test(size=(110, 40)) as pilot:
        app.query_one(ChatTextArea).focus()
        await _submit(pilot, app, "你好")
        await _wait_idle(pilot, app)
        before = len(_journal(session_dir))

        await _submit(pilot, app, "记住：以后回复都要简洁，不要结尾复述")
        await _wait_idle(pilot, app)

        proj_index = Path(project_memory_dir(str(harness.workspace))) / "MEMORY.md"
        user_index = harness.user_home / "mem" / "MEMORY.md"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not proj_index.exists() and not user_index.exists():
            await pilot.pause(0.5)

        _check("记忆笔记落盘（项目级或用户级）", proj_index.exists() or user_index.exists())
        index = proj_index if proj_index.exists() else user_index
        if index.exists():
            line = index.read_text(encoding="utf-8").strip().splitlines()[0]
            print(f"  索引行: {line}")
            _check("索引行含类型与文件名", line.startswith("- [") and ".md" in line)

        after = len(_journal(session_dir))
        _check("同一轮仍在追加写入同一存档", after > before, f"{before} → {after} 行")


async def scenario_3(harness: Harness, session_id: str) -> None:
    """场景 3：新进程 /resume 恢复历史并继续追加。"""
    _section("场景 3（会话恢复 /resume）")
    before = len(_journal(Path(sessions_root(str(harness.workspace))) / session_id))

    app = harness.new_app()
    new_id = app.runtime.session.session_id
    _check("新进程开的是新会话", new_id != session_id, f"{session_id} → {new_id}")

    async with app.run_test(size=(110, 40)) as pilot:
        select = await _open_resume(pilot, app)
        _check("/resume 进入列表态", select is not None)
        if select is None:
            return
        listed = [select.session_at(i).id for i in range(select.shown_count)]
        print(f"  列表（新→旧）: {listed}")
        _check("列表包含历史会话", session_id in listed, f"{select.shown_count} 项")

        ok = await _pick(pilot, app, select, session_id)
        _check("选中的是目标会话（进入正确会话目录）", ok, app.runtime.session.session_id)
        _check("恢复后回到空闲态", app.state is SessionState.IDLE)
        restored = app.conv.messages()
        _check("对话历史被恢复", len(restored) >= 2, f"{len(restored)} 条")

        await _submit(pilot, app, "我们刚才聊了什么？")
        await _wait_idle(pilot, app)

    after = len(_journal(Path(sessions_root(str(harness.workspace))) / session_id))
    _check("恢复后新消息追加进旧存档", after > before, f"{before} → {after} 行")


async def scenario_4(harness: Harness, session_id: str) -> None:
    """场景 4：工具调用进存档；压缩标记落盘且恢复从标记之后开始。"""
    _section("场景 4（工具调用存档 + 压缩标记）")
    (harness.workspace / "note.txt").write_text("ch11-e2e-marker\n", encoding="utf-8")

    app = harness.new_app()
    session_dir = Path(sessions_root(str(harness.workspace))) / session_id
    async with app.run_test(size=(110, 40)) as pilot:
        select = await _open_resume(pilot, app)
        if select is not None:
            _check("再次恢复同一会话", await _pick(pilot, app, select, session_id))

        await _submit(pilot, app, "请读取 note.txt 并告诉我里面写了什么")
        await _wait_idle(pilot, app)
        rows = _journal(session_dir)
        _check("JSONL 记录 tool_calls", any(r.get("tool_calls") for r in rows))
        _check("JSONL 记录 tool_results", any(r.get("tool_results") for r in rows))

        before_tokens, after_tokens = await app.agent.run_force_compact(
            app.conv, app.current_defs()
        )
        rows = _journal(session_dir)
        _check(
            "压缩标记落盘",
            any(r.get("type") == "compact" for r in rows),
            f"token {before_tokens} → {after_tokens}",
        )

    reloaded = load_session(str(session_dir))
    _check(
        "恢复时从最后一个 compact 标记之后加载",
        len(reloaded) < len(rows),
        f"标记后 {len(reloaded)} 条 / 全文件 {len(rows)} 行",
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="ch11 端到端证据脚本")
    parser.add_argument(
        "--provider", type=int, default=None, help="强制使用配置里的第 N 个 provider"
    )
    parser.add_argument("--keep", action="store_true", help="保留临时工作区，便于事后查看落盘")
    args = parser.parse_args()

    cfg, _ = await _discover_provider(args.provider)
    if cfg is None:
        print(
            "\n没有可用的 provider，端到端场景未执行。"
            "请在 .potatocode/config.yaml 配置可用密钥后重跑。"
        )
        return 2

    workspace = Path(tempfile.mkdtemp(prefix="potatocode-ch11-"))
    user_home = workspace / "home"
    user_home.mkdir(parents=True, exist_ok=True)
    (workspace / "MEWCODE.md").write_text(MEWCODE, encoding="utf-8")
    print(f"临时工作区: {workspace}")

    try:
        harness = Harness(workspace, cfg, user_home)
        harness.load_startup_inputs()
        print(f"启动时加载的指令: {harness.instructions.strip()!r}")

        session_id, _ = await scenario_1(harness)
        await scenario_2(harness)
        await scenario_3(harness, session_id)
        await scenario_4(harness, session_id)
    finally:
        if args.keep:
            print(f"（保留工作区）{workspace}")
        else:
            shutil.rmtree(workspace, ignore_errors=True)

    failed = [name for name, ok, _ in _results if not ok]
    print(f"\n通过 {len(_results) - len(failed)}/{len(_results)} 项")
    if failed:
        print("失败项: " + "；".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
