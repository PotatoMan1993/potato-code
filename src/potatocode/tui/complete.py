"""自动补全菜单：状态机 + 渲染 + App 侧键位拦截与输入同步（ch12 G5/F24-F32c）。

菜单是 TUI 层独有的 UX 元素，候选列表只从命令注册中心的 `prefix_match` 取；
渲染为 inline 的 `Static` widget（不用 OptionList 弹窗，避免抢焦点）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text
from textual.widgets import Static

from ..command import Command, Registry

# 菜单候选行的可见行数上限（N5），超出部分菜单内部滚动
MAX_ROWS = 8

# 零匹配时的占位提示行（F32b）
EMPTY_HINT = "（无匹配命令）"


@dataclass(slots=True)
class CompletionMenu:
    """补全菜单状态机：候选集、高亮与滚动窗口。"""

    items: list[Command] = field(default_factory=list)  # 当前候选，已按 name 字典序
    cursor: int = 0  # 高亮索引（相对 items）
    offset: int = 0  # 滚动窗口起点
    active: bool = False

    def update(self, input_text: str, reg: Registry) -> None:
        """按当前输入刷新候选（F24/F25/F26/F32a）。

        输入非 `/` 开头（含删空）或含换行时立即关闭；候选集变化后高亮回到顶部。
        零匹配时保持激活，仅显示"无匹配"提示行。
        """
        text = input_text.strip()
        if not text.startswith("/") or "\n" in input_text:
            self.hide()
            return
        self.items = reg.prefix_match(text)
        self.active = True
        self.cursor = 0
        self.offset = 0

    def move_up(self) -> None:
        """高亮上移（F29），夹在候选范围内并保持可见。"""
        self._move(-1)

    def move_down(self) -> None:
        """高亮下移（F29），越过可见窗口时滚动（C7）。"""
        self._move(1)

    def _move(self, delta: int) -> None:
        if not self.items:
            return
        self.cursor = max(0, min(len(self.items) - 1, self.cursor + delta))
        if self.cursor < self.offset:
            self.offset = self.cursor
        elif self.cursor >= self.offset + MAX_ROWS:
            self.offset = self.cursor - MAX_ROWS + 1

    def selected(self) -> Command | None:
        """当前高亮的命令；零匹配返回 None（F32b）。"""
        if not self.items:
            return None
        return self.items[self.cursor]

    def hide(self) -> None:
        """关闭菜单并清空状态（F31/F32：所有键位回到输入区默认行为）。"""
        self.active = False
        self.items = []
        self.cursor = 0
        self.offset = 0

    def render(self, width: int) -> Text:
        """渲染整个菜单块（含滚动提示行）；未激活返回空 Text（F27/N5/N6）。"""
        out = Text()
        if not self.active:
            return out
        if not self.items:
            out.append(EMPTY_HINT, style="dim italic")
            return out
        name_w = max(len(c.name) for c in self.items)
        if self.offset > 0:
            out.append(f"  ↑ {self.offset} more\n", style="dim")
        for i, cmd in enumerate(self.items[self.offset : self.offset + MAX_ROWS]):
            style = "reverse" if self.offset + i == self.cursor else ""
            line = Text(f"  /{cmd.name.ljust(name_w)}  {cmd.description}", style=style)
            line.truncate(width)
            out.append_text(line)
            out.append("\n")
        below = len(self.items) - (self.offset + MAX_ROWS)
        if below > 0:
            out.append(f"  ↓ {below} more", style="dim")
        return out


class CompletionMixin:
    """PotatoCodeApp 的补全 mixin：键位分派、输入同步与菜单渲染。"""

    # —— 以下属性由 PotatoCodeApp 提供（仅类型声明，无运行时赋值）——
    completion: CompletionMenu

    def handle_completion_key(self, key: str) -> bool:
        """菜单激活时的键位分派；返回 True 表示该键已被菜单消费（F29-F32）。

        零匹配时回车不消费（透传后走未命中提示分支），Tab/ESC 仅关闭菜单（F32b）。
        """
        menu = self.completion
        if not menu.active:
            return False
        if key == "up":
            menu.move_up()
        elif key == "down":
            menu.move_down()
        elif key == "escape":
            menu.hide()  # 输入框内容保持不变（F31）
        elif key in ("enter", "tab"):
            sel = menu.selected()
            if sel is None:
                menu.hide()
                self._refresh_completion()
                return key == "tab"
            self.execute_completion(sel.name)
            return True
        else:
            return False  # 其余键透传 TextArea
        self._refresh_completion()
        return True

    def execute_completion(self, name: str) -> None:
        """执行补全选中的命令：菜单关闭、输入框清空、走命令提交路径（F30）。"""
        from .app import ChatSubmitted, ChatTextArea  # 延迟导入避免循环

        self.completion.hide()
        self._refresh_completion()
        self.query_one(ChatTextArea).clear()  # type: ignore[attr-defined]
        self.post_message(ChatSubmitted("/" + name))

    def _sync_completion_from_input(self) -> None:
        """输入框内容变化后刷新菜单（激活 / 过滤 / 关闭，F24/F26）。"""
        from .app import ChatTextArea  # 延迟导入避免循环

        self.completion.update(self.query_one(ChatTextArea).text, self.cmd_registry)  # type: ignore[attr-defined]
        self._refresh_completion()

    def _refresh_completion(self) -> None:
        """把菜单当前状态渲染进 #completion Static（N6：紧贴输入框下方、状态栏上方）。"""
        widget = self.query_one("#completion", Static)
        if self.completion.active:
            widget.update(self.completion.render(self.size.width))
            widget.display = True
        else:
            widget.update("")
            widget.display = False
