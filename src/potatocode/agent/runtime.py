"""会话级运行时状态：跨多轮 run 复用，由 TUI 持有并注入 Agent。

TUI 历史上每轮都重新构造一次 Agent，若把压缩相关状态放在 Agent 字段上会被逐轮重置
（决策冻结、熔断器立刻失效），因此统一收拢到这个长生命周期容器里。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..compact import (
    CompactCircuitBreaker,
    ContentReplacementState,
    RecoveryState,
    SessionContext,
    new_session_context,
)


@dataclass
class SessionRuntime:
    """一个进程内会话的全部长生命周期状态。"""

    replacement: ContentReplacementState  # 工具结果替换决策账本
    recovery: RecoveryState  # 最近读过文件的追踪状态
    auto_tracking: CompactCircuitBreaker  # 自动摘要熔断计数
    session: SessionContext  # 会话 id 与落盘目录
    context_window: int = 200000  # 当前 provider 的上下文窗口（token）
    usage_anchor: int = 0  # 上一次主对话 stream 的真实用量之和；摘要请求不更新
    anchor_msg_len: int = 0  # 记录锚点时的会话条数，供增量估算划界
    turn_count: int = 0  # 已自然完成的对话轮数（ch11：每 5 轮触发一次记忆更新）
    # 字段读写都在 asyncio 单线程事件循环内完成，无需额外加锁

    def reset_for_new_session(self, ses_ctx: SessionContext) -> None:
        """/clear 开新会话时原子重置（ch12 T0c）。

        compact 三个子状态全部换新实例，用量锚点与轮数清零，会话指向新的
        `ses_ctx`；`context_window` 跟随 provider 保留，writer 与 conv 的重建由
        调用方负责，不进本接口。
        """
        self.replacement = ContentReplacementState()
        self.recovery = RecoveryState()
        self.auto_tracking = CompactCircuitBreaker()
        self.session = ses_ctx
        self.usage_anchor = 0
        self.anchor_msg_len = 0
        self.turn_count = 0


def new_runtime(
    workspace: str = ".",
    context_window: int = 200000,
    session: SessionContext | None = None,
) -> SessionRuntime:
    """按工作目录新建一份默认运行时（含落盘目录）。

    传入 session 时复用它而不新建：启动最前端为了先建日志出口已经建过一份会话，
    复用可保证日志文件与运行时指向同一个会话（见 docs/ch10/spec.md F2）。
    """
    return SessionRuntime(
        replacement=ContentReplacementState(),
        recovery=RecoveryState(),
        auto_tracking=CompactCircuitBreaker(),
        session=session if session is not None else new_session_context(workspace),
        context_window=context_window,
    )
