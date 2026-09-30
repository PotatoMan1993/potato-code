"""会话恢复界面：`/resume` 的历史会话列表与恢复流程（ch11 第 3 层）。

列表交互与 provider 选择一致（上下键 + Enter），另加「输入即过滤」：
可打印字符进搜索词、退格逐字删除，Esc 取消、Enter 恢复。

恢复流程的每一步都有降级策略（N5）：坏行跳过、孤立工具调用截断、token 超限先压缩、
单点失败退回当前会话而不是把 TUI 卡在中间态。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from textual import events
from textual.widgets import OptionList, RichLog
from textual.widgets.option_list import Option

from ..compact import AUTO_SAFETY_MARGIN, SUMMARY_RESERVE, estimate_tokens, open_session_context
from ..conversation import Conversation
from ..llm import ROLE_USER, Message
from ..session import SessionInfo, Writer, list_sessions, load_session_full
from .view import history_blocks, write_notice

if TYPE_CHECKING:
    from .app import PotatoCodeApp

logger = logging.getLogger(__name__)

# 超过这个时长未活动的会话，恢复时追加一条「已暂停」提醒（F21-5）
RESUME_GAP_SECONDS = 6 * 3600

# 时间跨度提醒模板（<duration> 由 format_duration 填充）
SPAN_REMINDER = (
    "[系统提示] 本会话已暂停 {duration}。部分上下文可能已过时，如需最新信息请重新读取相关文件。"
)


def format_duration(seconds: float) -> str:
    """把秒数渲染成「2 天 5 小时」这类中文时长。"""
    total = int(max(0, seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分钟"
    if minutes:
        return f"{minutes} 分钟"
    return f"{total} 秒"


def build_span_reminder(last_ts: int, now: float | None = None) -> str:
    """按最后一条消息的时间戳决定是否要补一条时间跨度提醒；不需要则返回空串。"""
    if not last_ts:
        return ""
    gap = (now if now is not None else time.time()) - last_ts
    if gap <= RESUME_GAP_SECONDS:
        return ""
    return SPAN_REMINDER.format(duration=format_duration(gap))


class ResumeSelect(OptionList):
    """历史会话列表：上下键导航 + 输入过滤 + Enter 恢复 + Esc 取消。"""

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._all: list[SessionInfo] = []
        self._shown: list[SessionInfo] = []
        self._filter = ""

    @property
    def filter_text(self) -> str:
        """当前搜索词。"""
        return self._filter

    @property
    def total(self) -> int:
        """不过滤时的会话总数。"""
        return len(self._all)

    @property
    def shown_count(self) -> int:
        """过滤后展示的会话数。"""
        return len(self._shown)

    def set_sessions(self, items: list[SessionInfo]) -> None:
        """装载会话列表并重置搜索词。"""
        self._all = list(items)
        self._filter = ""
        self._rebuild()

    def session_at(self, index: int | None) -> SessionInfo | None:
        """按下标取回会话信息（越界或未高亮返回 None）。"""
        if index is None or index < 0 or index >= len(self._shown):
            return None
        return self._shown[index]

    async def _on_key(self, event: events.Key) -> None:
        """自行处理全部按键：列表内不允许任何按键冒泡给 App 绑定。"""
        app = self.app
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            await app.cancel_resume()  # type: ignore[attr-defined]
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            await app.commit_resume(self.highlighted)  # type: ignore[attr-defined]
            return
        if event.key in ("up", "down"):
            event.stop()
            event.prevent_default()
            if event.key == "up":
                self.action_cursor_up()
            else:
                self.action_cursor_down()
            return
        if event.key == "backspace":
            event.stop()
            event.prevent_default()
            if self._filter:
                self._filter = self._filter[:-1]
                self._rebuild()
            return
        if event.is_printable and event.character and event.character.isprintable():
            event.stop()
            event.prevent_default()
            self._filter += event.character
            self._rebuild()
            return
        # 其余按键（方向键以外的控制键）一律吞掉，避免误触 App 级绑定
        event.stop()
        event.prevent_default()

    def _rebuild(self) -> None:
        """按搜索词重建选项并刷新提示行。"""
        needle = self._filter.lower()
        self._shown = [
            item
            for item in self._all
            if not needle or needle in item.title.lower() or needle in item.id.lower()
        ]
        self.clear_options()
        if self._shown:
            self.add_options([Option(item.display_text(), id=item.id) for item in self._shown])
            self.highlighted = 0
        else:
            self.add_option(Option("（没有匹配的历史会话）", id="__empty__", disabled=True))
            self.highlighted = None
        app = self.app
        notify = getattr(app, "update_resume_hint", None)
        if notify is not None:
            notify()


async def begin_resume(app: PotatoCodeApp) -> None:
    """扫描历史会话并进入选择界面（无历史会话时只提示，不切状态）。"""
    items = list_sessions(app.sessions_dir)
    if not items:
        write_notice(app, "没有找到可恢复的历史会话")
        return
    app.enter_resume_mode(items)


async def do_resume_session(app: PotatoCodeApp, info: SessionInfo) -> None:
    """恢复指定会话：换上下文 → 必要时压缩 → 时间跨度提醒 → 换存档与历史 → 重绘对话区。"""
    loaded = load_session_full(info.dir)
    msgs: list[Message] = list(loaded.messages)
    if not msgs:
        # 存档被删/全坏行：不切过去，免得把用户丢进一个空会话（N5）
        write_notice(app, f"会话 {info.id} 没有可恢复的消息，已保持当前会话")
        return

    # 确认有内容可恢复才开始：清掉上一会话的 scrollback，后续进度提示与回放按序累积
    _clear_log(app)
    write_notice(app, f"正在恢复会话 {info.id} ...")

    old_ctx = app.runtime.session
    try:
        # 会话上下文先切过去：超限压缩的落盘应写进被恢复会话自己的目录
        app.runtime.session = open_session_context(app.workspace, info.id)
        msgs = await _compact_if_needed(app, msgs)
    except Exception as e:  # 恢复失败：回滚会话上下文，保持当前会话可用（N5）
        app.runtime.session = old_ctx
        logger.exception("恢复会话失败: %s", info.id)
        write_notice(app, f"恢复会话失败：{type(e).__name__}: {e}")
        return

    # 提醒块是系统注入的，视图层用 notice 样式展示，不混进用户消息的回放
    replay = list(msgs)
    reminder = build_span_reminder(loaded.last_ts)
    if reminder:
        msgs = [*msgs, Message(role=ROLE_USER, content=reminder)]

    new_writer = Writer.open_existing(info.dir, model=app.current_model())
    old_writer = app.writer
    app.writer = new_writer
    # 恢复的历史不回写（on_append 从下一条新消息起生效），换回调只影响后续追加
    app.conv = Conversation.from_messages(
        msgs, on_append=new_writer.on_append, on_replace=new_writer.on_replace
    )
    if old_writer is not None:
        old_writer.close()
    # 历史整体换了，用量锚点与条数锚点随之失效
    app.runtime.usage_anchor = 0
    app.runtime.anchor_msg_len = 0

    # 对话区重绘：把恢复出的历史按原样回放，用户一眼能看到消息内容（F21）
    _replay_history(app, replay)
    if reminder:
        write_notice(app, reminder)
    write_notice(app, f"已恢复会话 {info.id}，共 {len(msgs)} 条消息")


def _clear_log(app: PotatoCodeApp) -> None:
    """清空对话区 scrollback（换会话前丢弃上一会话的界面残留）。"""
    try:
        app.query_one("#log", RichLog).clear()
    except Exception:  # 纯视图操作：失败不阻断恢复流程（N5）
        logger.exception("清空对话区失败")


def _replay_history(app: PotatoCodeApp, msgs: list[Message]) -> None:
    """把历史消息按序回放到对话区（失败只记日志，不影响已完成的恢复，N5）。"""
    try:
        log = app.query_one("#log", RichLog)
        for block in history_blocks(msgs):
            log.write(block)
    except Exception:  # 纯视图操作：失败不回滚会话，只损失一次回放
        logger.exception("恢复会话的历史回放失败")


async def _compact_if_needed(app: PotatoCodeApp, msgs: list[Message]) -> list[Message]:
    """估算 token 超限时先做一次压缩（F21-4）；压缩失败保留原历史。"""
    limit = app.runtime.context_window - SUMMARY_RESERVE - AUTO_SAFETY_MARGIN
    if estimate_tokens(0, msgs, 0) < limit:
        return msgs
    agent = app.ensure_agent()
    if agent is None:  # 还没选定 provider：无法压缩，直接恢复
        return msgs
    write_notice(app, "恢复的历史超出上下文窗口，正在压缩...")
    temp = Conversation.from_messages(msgs)
    try:
        await agent.run_force_compact(temp, app.current_defs())
    except Exception:
        logger.exception("恢复前压缩失败，按原历史恢复")
        return msgs
    return temp.messages()


__all__ = [
    "RESUME_GAP_SECONDS",
    "SPAN_REMINDER",
    "ResumeSelect",
    "begin_resume",
    "build_span_reminder",
    "do_resume_session",
    "format_duration",
]
