"""内置命令单测：12 条命令一次性注册、handler 在 NopUI 上可直接运行、关键行为断言（ch12）。"""

from __future__ import annotations

import pytest

from potatocode.command import (
    Command,
    Kind,
    NopUI,
    Registry,
    builtin_local,
    builtin_prompt,
    builtin_ui,
    register_builtins,
)
from potatocode.command.ui import UI
from potatocode.permission import Mode
from potatocode.prompt import EXECUTE_DIRECTIVE

# 12 条内置命令的固定名单（G1），按字典序
BUILTIN_NAMES = [
    "clear",
    "compact",
    "do",
    "exit",
    "help",
    "memory",
    "permission",
    "plan",
    "resume",
    "review",
    "session",
    "status",
]

# 各命令的 Kind（F8）
EXPECTED_KINDS: dict[str, Kind] = {
    "clear": Kind.UI,
    "compact": Kind.UI,
    "do": Kind.PROMPT,
    "exit": Kind.UI,
    "help": Kind.LOCAL,
    "memory": Kind.LOCAL,
    "permission": Kind.LOCAL,
    "plan": Kind.UI,
    "resume": Kind.UI,
    "review": Kind.PROMPT,
    "session": Kind.LOCAL,
    "status": Kind.LOCAL,
}


class RecordingUI(NopUI):
    """可观测桩：记录写入类调用，供行为断言。"""

    def __init__(self) -> None:
        super().__init__()
        self.prints: list[str] = []
        self.errors: list[str] = []
        self.modes: list[Mode] = []
        self.injections: list[tuple[str, str]] = []
        self.compact_calls = 0
        self.resume_calls = 0
        self.clear_calls = 0
        self.quit_calls = 0
        self.idle_value = True

    def println(self, msg: str) -> None:
        self.prints.append(msg)

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def set_mode(self, m: Mode) -> None:
        self.modes.append(m)
        super().set_mode(m)

    def inject_and_send(self, display_label: str, preset_prompt: str) -> None:
        self.injections.append((display_label, preset_prompt))

    def force_compact(self) -> None:
        self.compact_calls += 1

    def open_resume_menu(self) -> None:
        self.resume_calls += 1

    def clear_and_new_session(self) -> None:
        self.clear_calls += 1

    def quit(self) -> None:
        self.quit_calls += 1

    def idle(self) -> bool:
        return self.idle_value


def _registry() -> Registry:
    reg = Registry()
    register_builtins(reg)
    return reg


# ────────── 注册完整性（G1/G4） ──────────


def test_register_builtins_all_registered():
    """注册后恰好 12 条可见命令，名字完整、全小写、互不重复、Kind 正确。"""
    reg = _registry()
    visible = reg.visible()
    assert [c.name for c in visible] == BUILTIN_NAMES  # 字典序排列且恰好 12 条
    for cmd in visible:
        assert cmd.name == cmd.name.lower()
        assert cmd.description
        assert cmd.kind is EXPECTED_KINDS[cmd.name]


def test_register_builtins_no_collision():
    """register_builtins 重复调用（或注册进已有 registry）不应产生冲突副作用之外的问题。"""
    reg = _registry()
    with pytest.raises(RuntimeError):  # 再注册一遍必然撞名（F2 的行为面）
        register_builtins(reg)


# ────────── handler 在 NopUI 上全部可运行 ──────────


@pytest.mark.parametrize("name", BUILTIN_NAMES)
async def test_register_builtins_handlers_run_on_nop_ui(name: str):
    """每条命令的 handler 传 NopUI 直接 await，均不抛异常。"""
    reg = _registry()
    cmd = reg.lookup(name)
    assert cmd is not None
    await cmd.handler(NopUI())


# ────────── 关键行为断言 ──────────


async def test_handle_status_prints_all_keys():
    """/status 一次输出 6 行，key 顺序与 F19 一致。"""
    ui = RecordingUI()
    await builtin_local.handle_status(ui)
    assert len(ui.prints) == 1
    text = ui.prints[0]
    for key in ("Mode:", "Tokens:", "Tools:", "Memories:", "Model:", "Directory:"):
        assert key in text
    # 顺序固定（N8）
    assert text.index("Mode:") < text.index("Tokens:") < text.index("Tools:")
    assert text.index("Tools:") < text.index("Memories:") < text.index("Model:")
    assert text.index("Model:") < text.index("Directory:")


async def test_handle_help_lists_all_builtins():
    """/help 输出包含全部 12 个命令名（经注册中心查询，F18/N7）。"""
    ui = RecordingUI()
    reg = _registry()
    handler = builtin_local.make_help_handler(reg)
    await handler(ui)
    assert len(ui.prints) == 1
    for name in BUILTIN_NAMES:
        assert f"/{name}" in ui.prints[0]


async def test_handle_memory_lists_files():
    """/memory 逐行列文件名；为空时提示（F20）。"""
    ui = RecordingUI()

    class FilesUI(RecordingUI):
        def memory_files(self) -> list[str]:
            return ["MEMORY.md", "user_pref_x.md"]

    await builtin_local.handle_memory(ui)
    assert ui.prints == ["无已加载的记忆文件"]
    ui2 = FilesUI()
    await builtin_local.handle_memory(ui2)
    assert ui2.prints == ["MEMORY.md\nuser_pref_x.md"]


async def test_handle_permission_prints_mode_value():
    """/permission 输出当前模式的字符串名（F21）。"""
    ui = RecordingUI()
    ui.mode_value = Mode.PLAN
    await builtin_local.handle_permission(ui)
    assert ui.prints == ["plan"]


async def test_handle_session_prints_id_and_path():
    """/session 输出 Session 与 Path 两行（F22）。"""

    class SessionUI(RecordingUI):
        def session_id(self) -> str:
            return "20260601-100000-aaaa"

        def session_path(self) -> str:
            return "/tmp/sessions/20260601-100000-aaaa/conversation.jsonl"

    ui = SessionUI()
    await builtin_local.handle_session(ui)
    assert ui.prints == [
        "Session: 20260601-100000-aaaa\nPath: /tmp/sessions/20260601-100000-aaaa/conversation.jsonl"
    ]


async def test_handle_compact_blocks_when_busy():
    """/compact 在非 idle 时只报错、不触发压缩（N3a）。"""
    ui = RecordingUI()
    ui.idle_value = False
    await builtin_ui.handle_compact(ui)
    assert ui.errors == ["请等待当前任务完成"]
    assert ui.compact_calls == 0

    ui.idle_value = True
    await builtin_ui.handle_compact(ui)
    assert ui.compact_calls == 1


async def test_handle_resume_blocks_when_busy():
    """/resume 在非 idle 时只报错、不打开列表（沿用 ch09 约束）。"""
    ui = RecordingUI()
    ui.idle_value = False
    await builtin_ui.handle_resume(ui)
    assert ui.errors == ["请等待当前任务完成"]
    assert ui.resume_calls == 0


async def test_handle_do_sets_mode_and_injects():
    """/do 先切回 default 再注入执行指令（F14）。"""
    ui = RecordingUI()
    ui.mode_value = Mode.PLAN
    await builtin_prompt.handle_do(ui)
    assert ui.modes == [Mode.DEFAULT]
    assert ui.injections == [("/do", EXECUTE_DIRECTIVE)]


async def test_handle_review_injects_directive():
    """/review 注入含"审查"关键字的固定文案（F23）。"""
    ui = RecordingUI()
    await builtin_prompt.handle_review(ui)
    assert len(ui.injections) == 1
    label, preset = ui.injections[0]
    assert label == "/review"
    assert "审查" in preset


async def test_handle_clear_calls_ui_and_prints():
    """/clear 调用 clear_and_new_session 并输出提示（F17）。"""
    ui = RecordingUI()
    await builtin_ui.handle_clear(ui)
    assert ui.clear_calls == 1
    assert ui.prints == ["已清空当前会话，开启新会话"]


async def test_handle_exit_calls_quit():
    """/exit 调用 quit（F12/N12）。"""
    ui = RecordingUI()
    await builtin_ui.handle_exit(ui)
    assert ui.quit_calls == 1


async def test_handle_plan_sets_plan_mode():
    """/plan 切到 PLAN 模式（F13）。"""
    ui = RecordingUI()
    await builtin_ui.handle_plan(ui)
    assert ui.modes == [Mode.PLAN]
    assert ui.prints == ["已切换到 PLAN 模式"]


async def test_hidden_command_dispatchable_but_invisible():
    """hidden 机制：不进 /help 与补全候选，但 lookup 可命中（F28，机制预留）。"""
    reg = _registry()

    async def _handler(ui: UI) -> None:
        ui.println("secret!")

    reg.register(
        Command(
            name="zz_hidden",
            description="隐藏命令",
            kind=Kind.LOCAL,
            handler=_handler,
            hidden=True,
        )
    )
    assert reg.lookup("zz_hidden") is not None
    assert all(c.name != "zz_hidden" for c in reg.visible())
    assert all(c.name != "zz_hidden" for c in reg.prefix_match("/zz"))
