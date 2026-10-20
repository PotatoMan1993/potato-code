"""UI 抽象层：命令处理函数操作 TUI 的唯一通道（ch12 G3/F33/F34）。

handler 只依赖该协议，不持有任何 Textual 类型；`PotatoCodeApp` 通过
`CommandMixin` 实现全部方法。所有方法同步返回，UI 实现内部自行调度异步动作
（Textual 本身跑在 asyncio 事件循环上）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ..permission import Mode

if TYPE_CHECKING:
    from ..agent import Agent  # 仅类型引用：current_agent 的返回值（ch13）


class UI(Protocol):
    """命令可见的 UI 能力面——只暴露命令实际需要的方法（F34）。"""

    # ── 输出（写入 scrollback，不进对话历史） ──
    def println(self, msg: str) -> None:
        """按系统提示样式输出一行/多行消息。"""
        ...

    def error(self, msg: str) -> None:
        """按错误样式输出一条消息。"""
        ...

    # ── 权限模式 ──
    def current_mode(self) -> Mode: ...

    def set_mode(self, m: Mode) -> None: ...

    # ── 提示词注入（KindPROMPT 命令使用） ──
    def inject_and_send(self, display_label: str, preset_prompt: str) -> None:
        """向对话历史追加 preset_prompt 并立即触发一轮 LLM 回合。

        display_label 在 scrollback 中展示，preset_prompt 是实际入历史/存档的文本
        （F11/N3：与真实用户消息走同一条持久化路径）。
        """
        ...

    # ── 只读查询（/status /memory /session 等） ──
    def tokens_in(self) -> int: ...

    def tokens_out(self) -> int: ...

    def model_name(self) -> str: ...

    def cwd(self) -> str: ...

    def tool_count(self) -> int: ...

    def memory_files(self) -> list[str]: ...

    def session_path(self) -> str: ...

    def session_id(self) -> str: ...

    # ── 影响界面的动作 ──
    def quit(self) -> None: ...

    def force_compact(self) -> None: ...

    def open_resume_menu(self) -> None: ...

    async def clear_and_new_session(self) -> None:
        """结束当前会话并开启新会话（ch14：改为 async，串接 SessionEnd/SessionStart 派发）。"""
        ...

    # ── Hook 查询（ch14 /hooks 命令） ──
    def hook_rules(self) -> list:
        """已加载的 hook 规则列表（未注入 hook 引擎时为空列表）。"""
        ...

    def hook_sources(self) -> list[str]:
        """hook 配置的加载来源文件路径列表。"""
        ...

    # ── Skill 支持（ch13） ──
    def current_agent(self) -> Agent | None:
        """当前主 Agent（未选定 provider 时为 None）；/clear 钩子与 skill 命令用。"""
        ...

    def append_assistant_message(self, text: str) -> None:
        """向主对话追加一条 assistant 消息并展示（fork skill 结果回流用）。"""
        ...

    def skill_deliver_result(self, text: str) -> None:
        """SkillHost 协议复用：fork skill 结果回流（实现通常委托上一方法）。"""
        ...

    # ── 状态机查询 ──
    def idle(self) -> bool: ...


class NopUI:
    """测试桩：所有写入 no-op、所有查询返回零值，供 registry/handler 单测使用。"""

    def __init__(self) -> None:
        self.mode_value: Mode = Mode.DEFAULT

    def println(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass

    def current_mode(self) -> Mode:
        return self.mode_value

    def set_mode(self, m: Mode) -> None:
        self.mode_value = m

    def inject_and_send(self, display_label: str, preset_prompt: str) -> None:
        pass

    def tokens_in(self) -> int:
        return 0

    def tokens_out(self) -> int:
        return 0

    def model_name(self) -> str:
        return ""

    def cwd(self) -> str:
        return ""

    def tool_count(self) -> int:
        return 0

    def memory_files(self) -> list[str]:
        return []

    def session_path(self) -> str:
        return ""

    def session_id(self) -> str:
        return ""

    def quit(self) -> None:
        pass

    def force_compact(self) -> None:
        pass

    def open_resume_menu(self) -> None:
        pass

    async def clear_and_new_session(self) -> None:
        pass

    def hook_rules(self) -> list:
        return []

    def hook_sources(self) -> list[str]:
        return []

    def current_agent(self) -> Agent | None:
        return None

    def append_assistant_message(self, text: str) -> None:
        pass

    def skill_deliver_result(self, text: str) -> None:
        pass

    def idle(self) -> bool:
        return True
