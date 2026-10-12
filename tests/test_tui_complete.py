"""自动补全菜单单测：状态机行为与 App 键位集成（ch12 G5/F24-F32c）。"""

from __future__ import annotations

from textual.widgets import Static

from potatocode.agent.runtime import new_runtime
from potatocode.command import Registry, register_builtins
from potatocode.config import ProviderConfig
from potatocode.permission import new_engine
from potatocode.tool import new_default_registry
from potatocode.tui.app import ChatTextArea, PotatoCodeApp
from potatocode.tui.complete import CompletionMenu
from potatocode.tui.view import SessionState


def _reg() -> Registry:
    reg = Registry()
    register_builtins(reg)
    return reg


def _cfg() -> ProviderConfig:
    return ProviderConfig(
        name="fake-provider", protocol="anthropic", api_key="sk-test", model="fake-model"
    )


def _new_app(tmp_path) -> PotatoCodeApp:
    engine, _ = new_engine(str(tmp_path))
    runtime = new_runtime(workspace=str(tmp_path))
    # 单 provider：on_mount 直进对话态，输入框拿到焦点
    return PotatoCodeApp([_cfg()], "dev", new_default_registry(), engine, runtime=runtime)


# ────────── 状态机 ──────────


def test_update_activates_on_slash():
    """仅输入 "/" 时激活并返回全部 12 条候选（F24/AC11）。"""
    menu = CompletionMenu()
    menu.update("/", _reg())
    assert menu.active
    assert len(menu.items) == 12
    assert menu.selected() is not None


def test_update_filters_by_prefix():
    """/s 只剩以 s 开头的候选（F25/AC12），字典序保持。"""
    menu = CompletionMenu()
    menu.update("/s", _reg())
    assert [c.name for c in menu.items] == ["session", "status"]


def test_update_closes_on_non_slash_and_empty():
    """输入不以 / 开头（含删空）立即关闭（F26/AC15）。"""
    menu = CompletionMenu()
    menu.update("/", _reg())
    menu.update("hello", _reg())
    assert not menu.active
    menu.update("/", _reg())
    menu.update("", _reg())
    assert not menu.active
    assert menu.items == []


def test_update_closes_on_multiline():
    """含换行的输入不激活；激活后变多行立即关闭（F32a）。"""
    menu = CompletionMenu()
    menu.update("/do\nsecond", _reg())
    assert not menu.active
    menu.update("/", _reg())
    assert menu.active
    menu.update("/do\nsecond", _reg())
    assert not menu.active


def test_zero_match_stays_active_without_items():
    """零匹配时保持激活、items 为空、selected 返回 None（F32b）。"""
    menu = CompletionMenu()
    menu.update("/zz", _reg())
    assert menu.active
    assert menu.items == []
    assert menu.selected() is None


def test_move_clamps_and_scrolls():
    """↑↓ 夹在候选范围内；越过可见窗口时滚动 offset（F29/N5/C7）。"""
    menu = CompletionMenu()
    menu.update("/", _reg())
    for _ in range(8):
        menu.move_down()
    assert menu.cursor == 8
    assert menu.offset == 1  # 第 9 项滚入窗口
    for _ in range(10):
        menu.move_up()
    assert menu.cursor == 0
    assert menu.offset == 0
    menu.move_up()  # 顶部再上移不越界
    assert menu.cursor == 0


def test_selected_returns_highlighted_command():
    menu = CompletionMenu()
    menu.update("/sta", _reg())
    sel = menu.selected()
    assert sel is not None and sel.name == "status"


# ────────── 渲染 ──────────


def test_render_lists_candidates_with_hint():
    """渲染含候选行与滚动提示；12 条候选只显示 8 行 + ↓ 提示（F27/N5）。"""
    menu = CompletionMenu()
    menu.update("/", _reg())
    text = menu.render(100)
    plain = text.plain
    for name in ("clear", "compact", "do", "exit"):
        assert f"/{name}" in plain
    assert "↓ 4 more" in plain  # 12 - 8
    assert plain.count("\n") <= 10  # 8 行候选 + 提示行，高度受限


def test_render_highlights_cursor_row():
    """高亮行带 reverse 样式（F29）。"""
    menu = CompletionMenu()
    menu.update("/s", _reg())
    text = menu.render(100)
    assert any(str(span.style) == "reverse" for span in text.spans)
    menu.move_down()
    text2 = menu.render(100)
    assert any(str(span.style) == "reverse" for span in text2.spans)


def test_render_zero_match_hint():
    """零匹配时渲染"无匹配"提示行（F32b）。"""
    menu = CompletionMenu()
    menu.update("/zz", _reg())
    assert "无匹配" in menu.render(100).plain


def test_render_inactive_returns_empty():
    menu = CompletionMenu()
    assert menu.render(100).plain == ""


# ────────── App 集成（键位） ──────────


async def test_app_menu_activates_filters_and_escapes(tmp_path):
    """按 / 激活 → 输 s 过滤 → ESC 关闭且输入保留（F31/AC14）。"""
    app = _new_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("/")
        await pilot.pause()
        assert app.completion.active
        assert len(app.completion.items) == 12
        assert app.query_one("#completion", Static).display

        await pilot.press("s")
        await pilot.pause()
        assert [c.name for c in app.completion.items] == ["session", "status"]

        await pilot.press("escape")
        await pilot.pause()
        assert not app.completion.active
        assert not app.query_one("#completion", Static).display
        assert app.query_one("#input", ChatTextArea).text == "/s"  # 输入保留


async def test_app_menu_backspace_clears_closes(tmp_path):
    """全部退格删空后菜单立即消失（F26/C6）。"""
    app = _new_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("/", "s")
        await pilot.pause()
        assert app.completion.active
        await pilot.press("backspace", "backspace")
        await pilot.pause()
        assert not app.completion.active


async def test_app_menu_enter_executes_highlighted(tmp_path):
    """↓ 切高亮后回车执行次条候选（/status），输入框清空（F30/AC13/C3）。"""
    app = _new_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("/", "s")
        await pilot.pause()
        await pilot.press("down")  # 高亮 /status
        await pilot.press("enter")
        await pilot.pause()
        text = "\n".join(s.text for s in app.query_one("#log").lines)

        assert "Mode:" in text  # /status 的特征输出
        assert app.query_one("#input", ChatTextArea).text == ""
        assert not app.completion.active
        assert app.conv.messages() == []  # 本地命令不进历史


async def test_app_menu_tab_executes_highlighted(tmp_path):
    """Tab 直接执行当前高亮候选（F30）。"""
    app = _new_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("/", "s")  # 高亮 /session
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        text = "\n".join(s.text for s in app.query_one("#log").lines)

        assert "Session:" in text  # /session 的特征输出
        assert not app.completion.active


async def test_app_menu_zero_match_enter_goes_unknown(tmp_path):
    """零匹配时回车透传 → 未命中提示分支（F32b）。"""
    app = _new_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("/", "z", "z")
        await pilot.pause()
        assert app.completion.active and app.completion.items == []
        await pilot.press("enter")
        await pilot.pause()
        text = "\n".join(s.text for s in app.query_one("#log").lines)

        assert "未知命令" in text
        assert not app.completion.active


async def test_app_menu_hidden_stays_idle_after_run(tmp_path):
    """本地命令执行后回到 idle、菜单关闭（N1）。"""
    app = _new_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("/")
        await pilot.pause()
        await pilot.press("enter")  # 高亮第一项 /clear → 会触发 clear_and_new_session
        await pilot.pause()

        assert app.state is SessionState.IDLE
        assert not app.completion.active
