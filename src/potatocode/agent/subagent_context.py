"""子 Agent 调用深度的协程上下文标记（ch15 F24 QuerySource 等价物）。

Agent 工具在启动子 Agent 前把深度 +1（前台 await / 后台 create_task 都会继承
该上下文），执行结束复位；Agent 工具入口发现深度 > 0 即判定调用方来自某个
子 Agent，直接结构化拒绝——从调用链上阻断无限嵌套。独立成模块以避免
agent_tool ↔ task.manager 互相导入。
"""

from __future__ import annotations

import contextvars

# 当前协程所处的子 Agent 嵌套深度：0 = 主对话，>0 = 已在某个子 Agent 内
SUBAGENT_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "potatocode_subagent_depth", default=0
)

__all__ = ["SUBAGENT_DEPTH"]
