"""记忆更新的提示词模板：让模型把「值得记住的东西」输出成结构化操作列表。"""

from __future__ import annotations

from ..llm import Message

# 记忆更新的系统提示（F38：不传工具定义，模型只负责判断与产出 JSON）
MEMORY_UPDATE_SYSTEM_PROMPT = """\
你是 PotatoCode 的记忆管理员。用户与编码助手刚结束一轮对话，请判断其中有哪些信息值得
跨会话记住，并以 JSON 数组给出需要执行的更新操作。

值得记住的：
- 用户偏好（回复语言、风格、工具选择习惯）
- 用户对助手行为的纠正（"不要这样做"）
- 项目知识（技术栈、目录约定、构建/测试方式、架构决策）
- 参考资料（文档链接、外部系统地址、常用命令）

不值得记住的：一次性的任务细节、临时代码片段、可以从代码本身读出来的事实、寒暄。

规则：
- 分级：项目相关知识（项目知识、参考资料）放 "project"；跨项目通用的（用户偏好、纠正反馈）
  放 "user"。
- 类型 type 只能取：user_preference、correction_feedback、project_knowledge、reference_material。
- 去重：先看给定的现有索引，已有相同或相近的笔记时用 update 合并，而不是 create 新文件；
  过时且无法合并的笔记用 delete 删除。
- 文件名：create 时给出英文小写下划线 slug，文件名为 `<type>_<slug>.md`。
- update / delete 必须给出已存在的 filename（见索引行末尾括号里的文件名）。
- 没有值得记住的内容时返回空数组 []。
- 只输出 JSON 数组本身，不要任何解释文字，不要 Markdown 代码块围栏。

输出格式：
[
  {"action":"create","level":"project","type":"project_knowledge","title":"标题","slug":"api_conventions","content":"正文，一到三句话"},
  {"action":"update","level":"user","filename":"user_preference_terse_replies.md","title":"标题","content":"合并后的正文"},
  {"action":"delete","level":"project","filename":"project_knowledge_old_api.md"}
]"""


def _render_messages(msgs: list[Message]) -> str:
    """把本轮对话渲染成纯文本（工具调用与结果只保留摘要行）。"""
    lines: list[str] = []
    for msg in msgs:
        if msg.role == "user":
            lines.append(f"[用户] {msg.content}")
        elif msg.role == "assistant":
            text = msg.content or "（调用工具）"
            lines.append(f"[助手] {text}")
            for call in msg.tool_calls:
                lines.append(f"[助手/工具调用] {call.name}({call.input})")
        else:
            for result in msg.tool_results:
                body = result.content.strip().replace("\n", " ")
                lines.append(f"[工具结果] {body[:200]}")
    return "\n".join(lines)


def render_update_request(index: str, recent_msgs: list[Message]) -> str:
    """拼装发给模型的记忆更新请求正文（现有索引 + 本轮对话）。"""
    return (
        "## 现有记忆索引\n"
        f"{index.strip() or '（空）'}\n\n"
        "## 本轮对话\n"
        f"{_render_messages(recent_msgs)}\n\n"
        "请按要求输出需要执行的更新操作 JSON 数组。"
    )


__all__ = ["MEMORY_UPDATE_SYSTEM_PROMPT", "render_update_request"]
