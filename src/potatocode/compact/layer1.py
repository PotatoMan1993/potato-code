"""第 1 层压缩：超阈值工具结果落盘 + 对话内替换为预览体（决策冻结）。

纯字符串处理，不调用 LLM；落盘的决策由 `ContentReplacementState` 冻结，
保证同一 tool_use_id 在后续每一轮请求里都用同一份预览字符串（prompt cache 稳定）。
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path

from ..llm import ROLE_TOOL, Message, ToolResult
from .const import (
    MESSAGE_AGGREGATE_LIMIT,
    PREVIEW_HEAD_BYTES,
    PREVIEW_HEAD_LINES,
    SINGLE_RESULT_LIMIT,
)
from .state import (
    DECISION_KEPT,
    DECISION_REPLACED,
    DECISION_SKIP,
    ContentReplacementState,
    SessionContext,
)

logger = logging.getLogger(__name__)

# 预览体末尾的重读提示（固定文案，模型据此知道去哪里取回原文）
_REREAD_HINT = (
    "完整内容已保存到上述路径，如需查看请用文件读取工具读取该路径，不要凭头部预览猜测全文"
)


def spill_single(session: SessionContext, tool_use_id: str, content: str) -> None:
    """把单条工具结果原文写入 `spill_dir/<tool_use_id>`。

    幂等：文件已存在则直接返回，不重写、不报错；写盘失败抛 OSError 由上层捕获。
    """
    path = Path(session.spill_dir) / tool_use_id
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)  # 目录被外部清掉时也能落盘
    path.write_bytes(content.encode("utf-8"))


def _head_preview(content: str) -> str:
    """取头部预览：先截到 PREVIEW_HEAD_LINES 行，再截到 PREVIEW_HEAD_BYTES 字节。"""
    head = "".join(content.splitlines(keepends=True)[:PREVIEW_HEAD_LINES]).rstrip("\n")
    encoded = head.encode("utf-8")
    if len(encoded) <= PREVIEW_HEAD_BYTES:
        return head
    # 字节级截断可能切开多字节字符：用 ignore 丢弃不完整的尾部字节
    return encoded[:PREVIEW_HEAD_BYTES].decode("utf-8", errors="ignore")


def build_preview(original_bytes: int, head: str, spill_path: str) -> str:
    """构造替换体：原始字节数 + 头部预览 + 落盘路径 + 重读提示。

    只在首次决策为「替换」的瞬间调用一次；之后每一轮都复用账本里存好的字符串。
    """
    return "\n".join(
        [
            f"[content offloaded] original size: {original_bytes} bytes",
            f"[saved to] {spill_path}",
            "[head preview]",
            head,
            _REREAD_HINT,
        ]
    )


def _spill_decision(
    state: ContentReplacementState,
    session: SessionContext,
    result: ToolResult,
) -> tuple[str, bool]:
    """对一条工具结果做「落盘 → 构造预览 → 写账本」，返回 (content, 是否替换)。

    三个动作由 `decide_once` 串在同一临界区内：落盘失败返回 skip（不写账本，
    下次组装请求时重新评估），成功返回 replaced。
    """
    tool_use_id = result.tool_call_id
    content = result.content

    def _decide() -> tuple[str, str]:
        try:
            spill_single(session, tool_use_id, content)
        except OSError as e:  # 磁盘问题降级为不替换，不打断对话
            logger.warning("工具结果落盘失败，本轮保持原文: %s: %s", tool_use_id, e)
            return (DECISION_SKIP, "")
        spill_path = str(Path(session.spill_dir) / tool_use_id)
        head = _head_preview(content)
        return (DECISION_REPLACED, build_preview(len(content.encode("utf-8")), head, spill_path))

    new_content = state.decide_once(tool_use_id, content, _decide)
    return new_content, new_content is not content


def _keep_decision() -> tuple[str, str]:
    """「保留原文」决策回调：复用同一函数对象，避免在循环里反复建闭包。"""
    return (DECISION_KEPT, "")


def _process_tool_message(
    message: Message,
    state: ContentReplacementState,
    session: SessionContext,
) -> None:
    """就地处理一条 RoleTool 消息：先结清旧决策，再按单条 / 聚合阈值落盘新候选。"""
    results = message.tool_results

    candidates: list[int] = []
    for i, r in enumerate(results):
        if state.seen(r.tool_call_id):
            # 已决策项：复用账本存量结果（替换返回同一份预览字符串，不重新构造）
            results[i].content = state.decide_once(r.tool_call_id, r.content, _keep_decision)
        else:
            candidates.append(i)

    # 未决策项按字节倒序处理：先单条超限，再按聚合预算继续
    candidates.sort(key=lambda i: len(results[i].content.encode("utf-8")), reverse=True)
    remaining = sum(len(results[i].content.encode("utf-8")) for i in candidates)
    for pos, i in enumerate(candidates):
        size = len(results[i].content.encode("utf-8"))
        if size <= SINGLE_RESULT_LIMIT and remaining <= MESSAGE_AGGREGATE_LIMIT:
            # 本项及其后都不落盘：冻结「保留原文」，避免下一轮重复评估
            for k in candidates[pos:]:
                r = results[k]
                r.content = state.decide_once(r.tool_call_id, r.content, _keep_decision)
            break
        new_content, replaced = _spill_decision(state, session, results[i])
        if replaced:
            remaining -= size  # 被替换的项不再计入聚合预算
        results[i].content = new_content
        # 落盘失败的项由 skip 决策负责：既不写账本也不冻结，留给下一轮重新评估


def offload_and_snip(
    msgs: list[Message],
    state: ContentReplacementState,
    session: SessionContext,
) -> list[Message]:
    """第 1 层压缩入口：返回替换后的新消息列表（不修改入参）。

    只作用于 RoleTool 消息上的工具结果；未超阈值的项冻结为「保留原文」，
    超阈值的项落盘并替换为预览体。
    """
    out = copy.deepcopy(msgs)
    for message in out:
        if message.role == ROLE_TOOL and message.tool_results:
            _process_tool_message(message, state, session)
    return out
