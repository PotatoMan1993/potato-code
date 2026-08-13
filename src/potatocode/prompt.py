"""提示词与资源：内置 system prompt 与 ASCII PotatoCode banner。"""

from __future__ import annotations

# 内置固定 system prompt（F4：随每轮请求注入，conversation 保持纯 user/assistant）
SYSTEM_PROMPT: str = """\
你是 PotatoCode，一个运行在终端里的 AI 助手，风格简洁、专业、直接。

- 始终使用与用户相同的语言回复。
- 回答优先用 Markdown 组织：代码用代码块（注明语言）、要点用列表、关键词可加粗。
- 代码题给出可直接运行的最小示例，并附简短解释；不堆砌无关内容。
- 不确定时直说，不编造事实。
"""

# ASCII PotatoCode（一颗带表情的小土豆）
BANNER: str = r"""
    ██████╗  ██████╗ ████████╗ █████╗ ████████╗ ██████╗ 
    ██╔══██╗██╔═══██╗╚══██╔══╝██╔══██╗╚══██╔══╝██╔═══██╗
    ██████╔╝██║   ██║   ██║   ███████║   ██║   ██║   ██║
    ██╔═══╝ ██║   ██║   ██║   ██╔══██║   ██║   ██║   ██║
    ██║     ╚██████╔╝   ██║   ██║  ██║   ██║   ╚██████╔╝
    ╚═╝      ╚═════╝    ╚═╝   ╚═╝  ╚═╝   ╚═╝    ╚═════╝ 
"""


def render_banner(version: str, cwd: str) -> str:
    """拼装启动横幅：ASCII 图案 + 应用名与版本号 + 当前工作目录 + 就绪提示行。"""
    return (
        f"{BANNER.rstrip()}\n"
        f"\n"
        f"  PotatoCode v{version}\n"
        f"  cwd: {cwd}\n"
        f"\n"
        f"  Ready. 输入消息开始对话（Enter 发送 · Alt+Enter 换行 · /exit 退出）\n"
    )
