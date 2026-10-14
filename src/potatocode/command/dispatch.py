"""斜杠输入解析：判断输入是否走命令路径，并拆出命令名（ch12 F3/F5/F7）。

纯字符串操作、无副作用；本期所有内置命令均为零参数，携带参数尾巴的输入按
未命中处理（F7），由调用方走未知命令提示分支。
"""

from __future__ import annotations


def parse(input_text: str) -> tuple[str, bool]:
    """解析输入，返回 `(命令名, 是否斜杠输入)`。

    - 空串 / 纯空白 / 非 `/` 开头：`("", False)`，上层送 Agent（F3/F5）
    - 纯 `/`、`/ + 参数尾巴`、`//` 等退化输入：`("", True)`，lookup 必然 miss
    - 正常命令：`("name", True)`，name 已小写化（F4）
    """
    text = input_text.strip()
    if not text.startswith("/"):
        return ("", False)
    rest = text[1:]
    if not rest.strip():
        return ("", True)  # 纯 "/"
    # 用单空格切分（不吞空白）：`/ /help` 这类输入的第二段算参数尾巴（F7）
    pieces = rest.split(" ", 1)
    if len(pieces) > 1 and pieces[1].strip():
        return ("", True)  # 携带参数尾巴：按未命中处理（F7）
    name = pieces[0].strip("/").lower()
    if not name:  # 形如 "//" 的退化输入
        return ("", True)
    return (name, True)


def parse_with_args(input_text: str) -> tuple[str, str, bool]:
    """解析输入，返回 `(命令名, 参数尾巴, 是否斜杠输入)`（ch13 扩展）。

    基础规则与 parse 完全一致；差别在于携带参数尾巴的输入不再一律按未命中丢弃，
    而是把尾巴原样带回，由 dispatcher 查表决定：命令声明 accepts_args 才消费参数，
    否则仍按未命中处理（保持 ch12 F7 行为不回归）。
    """
    text = input_text.strip()
    if not text.startswith("/"):
        return ("", "", False)
    rest = text[1:]
    if not rest.strip():
        return ("", "", True)  # 纯 "/"
    head, _, tail = rest.partition(" ")
    name = head.strip("/").lower()
    if not name:  # 形如 "//" 或 "/ xxx" 的退化输入
        return ("", "", True)
    return (name, tail.strip(), True)
