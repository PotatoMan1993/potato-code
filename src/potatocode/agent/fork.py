"""Fork 路径辅助：消息克隆、悬空 tool_use 修补与 Fork 上下文识别（ch15 F22-F25）。

Fork 式子 Agent 克隆父对话全部历史并注入 Fork Boilerplate，借 prompt cache
降低首次请求成本（N2）；`build_forked_messages` 保证克隆后的消息列表保持
tool_call/tool_result 配对与角色交替的历史合法性不变量（F22）。
"""

from __future__ import annotations

import copy

from ..llm import ROLE_TOOL, ROLE_USER, Message, ToolResult

# Fork Boilerplate 的识别标记：出现在消息历史中即认定该对话来自 Fork（F24-③ 兜底）
FORK_BOILERPLATE_TAG = "<fork_boilerplate>"

# Fork 子 Agent 首条 user 消息的前缀，约束其行为（F23）
FORK_BOILERPLATE = f"""{FORK_BOILERPLATE_TAG}
你是一个 Fork 出来的工作进程。你不是主 Agent。
规则（不可协商）：
1. 不能再 Fork（调用 Agent 工具会被拦截）。
2. 不要对话、不要提问、不要请求确认。
3. 直接使用工具：读文件、搜索代码、做修改。
4. 严格限制在你被分配的任务范围内。
5. 最终报告以 "Scope:" 开头，500 字以内。
</fork_boilerplate>

"""

# 悬空 tool_use 的占位结果文本（修补历史合法性用，F22-②）
_FORK_SKIPPED_PLACEHOLDER = "[forked, skipped]"


def build_forked_messages(parent_msgs: list[Message], task: str) -> list[Message]:
    """把父对话克隆为 Fork 子对话的消息列表（F22）。

    1. 深拷贝父消息列表（互不影响父对话状态）；
    2. 找最后一条带 tool_calls 的 assistant 消息，其调用若被后续 tool 回合
       消费则跳过；未消费（悬空）的每个调用补一条占位 tool_result，保证
       tool_call/tool_result 配对合法；
    3. 末尾追加 user 消息 = FORK_BOILERPLATE + 任务文本（F23）。

    返回的新列表用 `Conversation.from_messages` 装载即可。
    """
    cloned = copy.deepcopy(list(parent_msgs))

    # 定位最后一条携带工具调用的 assistant 消息，收集其未被消费的调用 id
    last_call_index = -1
    for index in range(len(cloned) - 1, -1, -1):
        if cloned[index].tool_calls:
            last_call_index = index
            break
    if last_call_index >= 0:
        consumed_ids = {
            result.tool_call_id
            for message in cloned[last_call_index + 1 :]
            if message.role == ROLE_TOOL
            for result in message.tool_results
        }
        dangling = [
            call for call in cloned[last_call_index].tool_calls if call.id not in consumed_ids
        ]
        if dangling:
            cloned.append(
                Message(
                    role=ROLE_TOOL,
                    tool_results=[
                        ToolResult(
                            tool_call_id=call.id,
                            content=_FORK_SKIPPED_PLACEHOLDER,
                            is_error=True,
                        )
                        for call in dangling
                    ],
                )
            )

    cloned.append(Message(role=ROLE_USER, content=FORK_BOILERPLATE + task))
    return cloned


def is_fork_context(msgs: list[Message]) -> bool:
    """判定消息历史是否来自 Fork：任一消息文本含 `<fork_boilerplate>` 标记（F24-③）。

    作为调用深度标记（QuerySource）失效时的兜底识别——历史上该标记只经
    `build_forked_messages` 写入 user 消息开头，普通对话不会出现。
    """
    return any(FORK_BOILERPLATE_TAG in message.content for message in msgs)


__all__ = [
    "FORK_BOILERPLATE",
    "FORK_BOILERPLATE_TAG",
    "build_forked_messages",
    "is_fork_context",
]
