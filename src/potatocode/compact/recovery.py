"""压缩后的三段恢复：最近读过的文件、当前可用工具、边界提示。

全部为纯函数：调用方先拍一次文件追踪快照再传进来，避免渲染期间状态漂移；
工具列表直接用主循环本轮迭代选好的同一份定义列表，保证与请求 tools 完全一致。
"""

from __future__ import annotations

import json

from ..llm import ToolDefinition
from .const import ESTIMATE_CHARS_PER_TOKEN, RECOVERY_FILE_LIMIT, RECOVERY_TOKENS_PER_FILE
from .state import FileReadRecord

# 固定边界提示：告诉模型需要原文时去重读，不要凭摘要猜测
BOUNDARY_NOTICE = """\
以上内容是历史的压缩摘要与恢复信息，不是完整原文。
当任务需要文件原文、错误原文或用户的原话时，请使用文件读取工具重新读取上述路径，
不要依据摘要内容做猜测。"""

# 单个文件快照被截断时追加在尾部的标注
_TRUNCATED_MARK = "(content truncated)"


def render_file_block(rec: FileReadRecord) -> str:
    """渲染单个文件快照：路径、读取时间与内容片段（超限时保留头部并标注截断）。"""
    char_limit = int(RECOVERY_TOKENS_PER_FILE * ESTIMATE_CHARS_PER_TOKEN)
    content = rec.content
    truncated = len(content) > char_limit
    if truncated:
        content = content[:char_limit]
    block = f"### {rec.path}\n[read at] {rec.timestamp.isoformat()}\n{content}"
    if truncated:
        block += f"\n{_TRUNCATED_MARK}"
    return block


def render_tools_block(defs: list[ToolDefinition]) -> str:
    """渲染当前可用工具列表：每个工具一行名称与用途，下一行附参数 schema。"""
    if not defs:
        return "(无)"
    lines: list[str] = []
    for d in defs:
        schema = json.dumps(d.input_schema, separators=(",", ":"), ensure_ascii=False)
        lines.append(f"- {d.name}: {d.description}")
        lines.append(f"  schema: {schema}")
    return "\n".join(lines)


def build_recovery_attachment(
    snapshot: list[FileReadRecord],
    tool_defs: list[ToolDefinition],
) -> str:
    """拼装恢复三段文本：最近读过的文件、当前可用工具、边界提示。

    `snapshot` 由调用方一次性拍好（已按时间戳倒序），本函数只取前 RECOVERY_FILE_LIMIT 个；
    返回值是纯文本片段，由上层与摘要正文合并到同一条 user 消息里。
    """
    files = snapshot[:RECOVERY_FILE_LIMIT]
    if files:
        files_text = "\n\n".join(render_file_block(r) for r in files)
    else:
        files_text = "(无)"
    return "\n\n".join(
        [
            f"## 最近读过的文件\n{files_text}",
            f"## 当前可用工具\n{render_tools_block(tool_defs)}",
            f"## 边界提示\n{BOUNDARY_NOTICE}",
        ]
    )
