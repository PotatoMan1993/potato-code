"""摘要 Prompt 模板与解析：把对话塞进固定模板，再从模型输出里抠出正式摘要。

模板本身是产品规范的一部分，直接内嵌在代码里（不从外部文件加载），
9 个小节标题用固定字面字符串，便于解析与测试匹配。
"""

from __future__ import annotations

import json
import logging
import re

from ..llm import ROLE_ASSISTANT, Message

logger = logging.getLogger(__name__)

# 摘要指令：两阶段输出（先分析草稿、再正式摘要），正式摘要固定 9 个小节
SUMMARY_INSTRUCTION = """你是一名正在压缩编码助手会话历史的助理。请分两个阶段输出。

第一阶段：在 <analysis> 标签内写出分析草稿，逐条梳理会话脉络。草稿会被丢弃，只用于理清思路。
第二阶段：在 <summary> 标签内写出正式摘要，严格按以下 9 个小节顺序输出，小节标题逐字保留：

<analysis>
（在此写分析草稿，会被丢弃）
</analysis>

<summary>
## 1 主要请求和意图
## 2 关键技术概念
## 3 文件和代码段
## 4 错误和修复
## 5 问题解决过程
## 6 所有用户消息原文
## 7 待办任务
## 8 当前工作(最详细)
## 9 可能的下一步
</summary>

要求：
- 第 6 节必须按时间顺序逐条保留本会话中每一条用户消息的原文，不允许改写或概括。
- 第 8 节要最详细：正在做什么、已经改到哪一步、停在哪里。
- 不要调用任何工具，输出纯文本。"""

# 会话序列化时的前缀标记（纯文本，便于模型阅读）
_CALL_PREFIX = "[call"
_RESULT_PREFIX = "[result"


def serialize_conversation(msgs: list[Message]) -> str:
    """把对话扁平化成可读文本（工具调用只暴露名称与参数 JSON，不暴露内部对象）。"""
    lines: list[str] = []
    for m in msgs:
        if m.content:
            lines.append(f"{m.role}: {m.content}")
        elif m.role == ROLE_ASSISTANT and m.tool_calls:
            lines.append("assistant: (无文本)")
        for c in m.tool_calls:
            args = c.input if isinstance(c.input, str) else json.dumps(c.input, ensure_ascii=False)
            lines.append(f"{_CALL_PREFIX} {c.name} id={c.id} args={args}]")
        for r in m.tool_results:
            lines.append(f"{_RESULT_PREFIX} id={r.tool_call_id} is_error={r.is_error}] {r.content}")
    return "\n".join(lines)


def build_summary_prompt(msgs: list[Message]) -> list[Message]:
    """把对话嵌入固定模板，返回只含一条 user 消息的请求体 messages。"""
    body = f"{SUMMARY_INSTRUCTION}\n\n[conversation]\n{serialize_conversation(msgs)}"
    return [Message(role="user", content=body)]


def extract_summary(raw: str) -> str:
    """从模型返回的整段文本里抠出最后一对 `<summary>` 之间的正文。

    `<analysis>` 草稿直接丢弃；找不到标签时返回原文并记一条 warning，避免硬失败。
    """
    matches = re.findall(r"<summary>(.*?)</summary>", raw, re.DOTALL)
    if not matches:
        logger.warning("摘要输出未找到 <summary> 标签，降级使用原文")
        return raw
    return matches[-1].strip()
