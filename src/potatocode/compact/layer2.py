"""第 2 层压缩：LLM 全量摘要 + 三段恢复 + 近期原文，失败按熔断规则记账。

摘要请求复用主对话的 provider，但不传任何工具定义；摘要自身撞上下文超限时
按「用户提交 → 一组往返」分组丢弃最旧消息并重试（自动 / 手动 / 紧急共用）。
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from ..llm import ROLE_ASSISTANT, ROLE_TOOL, ROLE_USER, Message, PromptTooLongError, Request
from .const import (
    ESTIMATE_CHARS_PER_TOKEN,
    MANUAL_SAFETY_MARGIN,
    PTL_DROP_PERCENTAGE,
    PTL_RETRY_LIMIT,
    RECENT_KEEP_MESSAGES,
    RECENT_KEEP_TOKENS,
    SUMMARY_RESERVE,
)
from .recovery import build_recovery_attachment
from .summary_prompt import build_summary_prompt, extract_summary
from .token import estimate_tokens, message_chars

if TYPE_CHECKING:
    from .compact import ManageInput

logger = logging.getLogger(__name__)

# 摘要与恢复段合并成一条 user 消息时的标题
_SUMMARY_TITLE = "## 历史会话摘要"
# 摘要消息（user）紧接 user 原文时插入的 assistant 衔接占位
_BRIDGE_TEXT = "（已加载上下文摘要与恢复信息。请继续。）"


def _one_token(msg: Message) -> int:
    """估算单条消息的 token（字符数 / 常量比例，向上取整）。"""
    return math.ceil(message_chars([msg]) / ESTIMATE_CHARS_PER_TOKEN)


async def summarize_once(in_: ManageInput, msgs: list[Message]) -> str:
    """发一次摘要请求：不传工具，累加文本，流出错立即抛出。

    摘要请求结束后不更新主对话的用量锚点（锚点只由主对话路径维护）。
    """
    req = Request(messages=build_summary_prompt(msgs), tools=None)
    parts: list[str] = []
    async for ev in in_.provider.stream(req):
        if ev.err is not None:
            raise ev.err
        if ev.text:
            parts.append(ev.text)
    return extract_summary("".join(parts))


async def ptl_retry(in_: ManageInput, msgs: list[Message], first_err: Exception) -> str:
    """摘要请求自身撞 PTL 时的丢消息组重试（F27）。

    前 PTL_RETRY_LIMIT 次每次丢最旧 1 组；之后每次按剩余组数的比例丢（至少 1 组）；
    组全部丢光仍失败则抛最近一次异常，不会发送 messages 为空的摘要请求。
    """
    groups = group_by_user_turn(msgs)
    last_err = first_err
    retries = 0
    while True:
        if retries < PTL_RETRY_LIMIT:
            drop = 1
        else:
            drop = max(1, math.ceil(len(groups) * PTL_DROP_PERCENTAGE))
        groups = groups[drop:]
        if not groups:
            raise last_err
        retries += 1
        logger.info("摘要请求撞 PTL，丢弃最旧 %d 组后重试（第 %d 次）", drop, retries)
        flat = [m for g in groups for m in g]
        try:
            return await summarize_once(in_, flat)
        except PromptTooLongError as e:  # 只有 PTL 才继续丢；其他异常向上抛
            last_err = e


def group_by_user_turn(msgs: list[Message]) -> list[list[Message]]:
    """按「用户提交 → 一组 assistant/tool 往返」切分对话，供丢消息组使用。"""
    groups: list[list[Message]] = []
    for m in msgs:
        if m.role == ROLE_USER or not groups:
            groups.append([m])  # 首条不是 user 时也单独成组，避免被丢掉
        else:
            groups[-1].append(m)
    return groups


def pick_recent_tail(msgs: list[Message]) -> list[Message]:
    """从尾部倒序累加，取「累计 token ≥ 下界 且 条数 ≥ 下界」的近期原文。

    两个下界都满足才停手（择宽）；随后做配对修正：截断点落在落单 tool_result 上时
    向前推到发起该工具调用的 assistant 之前，保证不切开 tool_use / tool_result。
    """
    if not msgs:
        return []
    total = 0
    count = 0
    start = 0
    for i in range(len(msgs) - 1, -1, -1):
        total += _one_token(msgs[i])
        count += 1
        start = i
        if total >= RECENT_KEEP_TOKENS and count >= RECENT_KEEP_MESSAGES:
            break
    while start > 0 and msgs[start].role == ROLE_TOOL:
        start -= 1
    return list(msgs[start:])


def _join_after_summary(summary_msg: Message, recent: list[Message]) -> list[Message]:
    """拼接摘要消息与近期原文，必要时插 assistant 占位，避免 user/user 连续。"""
    if not recent:
        return [summary_msg]
    first = recent[0]
    if first.role == ROLE_TOOL:
        # 防御性修正：丢掉落单的 tool_result，避免构造出非法配对
        recent = list(recent)
        while recent and recent[0].role == ROLE_TOOL:
            recent.pop(0)
        if not recent:
            return [summary_msg]
        first = recent[0]
    if first.role == ROLE_USER:
        bridge = Message(role=ROLE_ASSISTANT, content=_BRIDGE_TEXT)
        return [summary_msg, bridge, *recent]
    return [summary_msg, *recent]


def _summary_prompt_over_limit(in_: ManageInput, msgs: list[Message]) -> bool:
    """预检摘要请求自身的 messages 是否已超「窗口 − 摘要预留 − 手动余量」。

    超了就说明这一发必然被拒，直接走丢消息组重试，不让请求白白撞墙。
    """
    prompt = build_summary_prompt(msgs)
    limit = in_.context_window - SUMMARY_RESERVE - MANUAL_SAFETY_MARGIN
    return estimate_tokens(0, prompt, 0) > limit


async def run_summary(in_: ManageInput, *, precheck: bool = False) -> list[Message]:
    """摘要 + 恢复 + 近期原文的完整构造：返回替换旧历史的新消息列表。

    `precheck=True`（手动 / 紧急路径）时先判断摘要请求自身是否已超
    「窗口 − 摘要预留 − 手动余量」，超了就跳过首发直接丢消息组。
    """
    old_msgs = in_.conv.messages()
    # 入口一次性拍快照：整轮渲染只用这一份，避免渲染期间 record_file 造成漂移
    snapshot = in_.recovery.snapshot()
    if precheck and _summary_prompt_over_limit(in_, old_msgs):
        logger.info("摘要请求预检超限，直接进入丢消息组重试")
        summary_text = await ptl_retry(in_, old_msgs, PromptTooLongError("摘要请求预检超限"))
    else:
        try:
            summary_text = await summarize_once(in_, old_msgs)
        except PromptTooLongError as err:
            summary_text = await ptl_retry(in_, old_msgs, err)
    recovery_text = build_recovery_attachment(snapshot, in_.tool_defs)
    # 摘要与恢复段合并成同一条 user 消息：避免 user/user 连续违反协议
    summary_msg = Message(
        role=ROLE_USER, content=f"{_SUMMARY_TITLE}\n{summary_text}\n\n{recovery_text}"
    )
    recent = pick_recent_tail(old_msgs)
    logger.info("摘要完成：保留近期原文 %d 条", len(recent))
    return _join_after_summary(summary_msg, recent)


async def auto_compact(in_: ManageInput) -> tuple[list[Message], int, int]:
    """自动路径摘要：整轮（含 PTL 自重试）失败累加连续失败计数，成功清零。"""
    before = in_.estimated_token
    try:
        new_msgs = await run_summary(in_)
    except Exception:
        in_.auto_tracking.record_failure()
        raise
    in_.auto_tracking.record_success()
    return new_msgs, before, estimate_tokens(0, new_msgs, 0)


async def force_compact(in_: ManageInput) -> tuple[list[Message], int, int]:
    """手动 / 紧急路径摘要：与自动路径同一条核心路径，但不读也不写熔断计数。

    手动 / 紧急触发时不做阈值判断，因此先用摘要请求自身的预检兜住必然撞墙的请求。
    """
    before = in_.estimated_token
    new_msgs = await run_summary(in_, precheck=True)
    return new_msgs, before, estimate_tokens(0, new_msgs, 0)
