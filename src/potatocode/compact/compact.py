"""上下文管理主入口 `manage_context`：编排两层压缩与自动 / 手动 / 紧急三条路径。

compact 包对状态零持有、可重入；调用方（Agent 主循环 / TUI）把会话级状态对象
与本次估算 token 一并传进来，本模块决定跑第 1 层、第 2 层还是两者都跑。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from ..llm import Provider, ToolDefinition
from .const import AUTO_SAFETY_MARGIN, SUMMARY_RESERVE
from .layer1 import offload_and_snip
from .layer2 import auto_compact, force_compact
from .state import CompactCircuitBreaker, ContentReplacementState, RecoveryState, SessionContext
from .token import estimate_tokens

if TYPE_CHECKING:
    from ..conversation import Conversation

logger = logging.getLogger(__name__)


class TriggerKind(Enum):
    """本次压缩的触发原因：自动阈值 / 用户手动 / 撞墙紧急。"""

    AUTO = "auto"
    MANUAL = "manual"
    EMERGENCY = "emergency"


@dataclass
class ManageInput:
    """一次上下文管理的全部入参：会话、状态对象与本轮估算 token。"""

    conv: Conversation
    provider: Provider
    context_window: int  # 当前 provider 的上下文窗口（token）
    tool_defs: list[ToolDefinition]  # 本轮迭代选好的工具定义，与 stream 请求共用同一列表
    replacement: ContentReplacementState
    recovery: RecoveryState
    auto_tracking: CompactCircuitBreaker
    session: SessionContext
    usage_anchor: int  # 上一次主对话 stream 的真实用量之和
    anchor_msg_len: int  # 记录锚点时的 conv.length()
    estimated_token: int  # 调用方算好的本轮估算 token
    trigger: TriggerKind


@dataclass
class ManageOutput:
    """一次上下文管理的结果：压缩前 / 后的估算 token。"""

    before_tokens: int
    after_tokens: int
    executed_error: str | None = None     # 执行失败或者未执行时


def auto_threshold(context_window: int) -> int:
    """自动摘要的触发阈值：上下文窗口减去摘要预留与安全余量。"""
    return context_window - SUMMARY_RESERVE - AUTO_SAFETY_MARGIN


async def manage_context(in_: ManageInput) -> ManageOutput:
    """Agent 每轮请求前必调的唯一入口，返回压缩前后的估算 token。

    手动跳过第 1 层与所有阈值；紧急先强制跑一次第 1 层再摘要；
    自动路径先跑第 1 层、用压缩后的消息重算估算，再决定是否跑第 2 层。
    """
    try:
        return await _manage(in_)
    except Exception:
        # 压缩失败不能吞：记一条日志后交给上层按各自规则处理（自动计熔断、手动上抛）
        logger.exception("上下文压缩失败: trigger=%s", in_.trigger.value)
        raise


async def _manage(in_: ManageInput) -> ManageOutput:
    """按 trigger 分派到自动 / 手动 / 紧急三条路径（见 manage_context 说明）。"""
    if in_.trigger is TriggerKind.MANUAL:
        # 手动 /compact：跳过第 1 层、跳过阈值检查、跳过熔断器
        new_msgs, before, after = await force_compact(in_)
        in_.conv.replace_history(new_msgs)
        logger.info("手动压缩完成：token %d -> %d", before, after)
        return ManageOutput(before_tokens=before, after_tokens=after)

    if in_.trigger is TriggerKind.EMERGENCY:
        # 紧急压缩：先强制跑一次第 1 层，避免摘要请求自身撞 PTL
        layer1_out = offload_and_snip(in_.conv.messages(), in_.replacement, in_.session)
        in_.conv.replace_history(layer1_out)
        new_msgs, before, after = await force_compact(in_)
        in_.conv.replace_history(new_msgs)
        logger.info("紧急压缩完成：token %d -> %d", before, after)
        return ManageOutput(before_tokens=before, after_tokens=after)

    # 自动路径：无论是否触发第 2 层，第 1 层的替换都必须写回会话
    layer1_out = offload_and_snip(in_.conv.messages(), in_.replacement, in_.session)
    in_.conv.replace_history(layer1_out)
    est_tokens = estimate_tokens(in_.usage_anchor, layer1_out, in_.anchor_msg_len)

    if in_.context_window <= SUMMARY_RESERVE + AUTO_SAFETY_MARGIN:
        # 窗口过小：阈值非正会让每轮都触发摘要，直接跳过自动第 2 层
        logger.warning("context_window=%d 过小，跳过自动压缩", in_.context_window)
        executed_error= f"context_window={in_.context_window} 过小，跳过自动压缩"
        return ManageOutput(before_tokens=in_.estimated_token, after_tokens=est_tokens, executed_error=executed_error)

    if est_tokens < auto_threshold(in_.context_window) or in_.auto_tracking.tripped():
        executed_error = f"current_context={est_tokens} 小于自动压缩触发阈值，跳过自动压缩"
        return ManageOutput(before_tokens=in_.estimated_token, after_tokens=est_tokens, executed_error=executed_error)

    new_msgs, before, after = await auto_compact(in_)
    in_.conv.replace_history(new_msgs)
    logger.info(
        "自动压缩完成：token %d -> %d（阈值 %d）",
        before,
        after,
        auto_threshold(in_.context_window),
    )
    return ManageOutput(before_tokens=before, after_tokens=after)
