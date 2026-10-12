"""parse 输入解析单测：斜杠判定、命令名拆解、大小写与退化输入（ch12 F3/F5/F7）。"""

from __future__ import annotations

import pytest

from potatocode.command import parse


@pytest.mark.parametrize(
    ("input_text", "expected"),
    [
        ("", ("", False)),  # 空输入不参与分发（F5）
        ("   ", ("", False)),  # 纯空白不参与分发（F5）
        ("hello", ("", False)),  # 非 / 开头送给 Agent（F3）
        ("/", ("", True)),  # 纯斜杠：退化输入走未命中分支
        ("/help", ("help", True)),
        ("/HELP", ("help", True)),  # 大小写不敏感（F4）
        ("  /help  ", ("help", True)),  # 首尾空白剥离
        ("/help xx", ("", True)),  # 携带参数尾巴按未命中处理（F7）
        ("//double", ("double", True)),  # 多重斜杠剥成一个名字，lookup 必然 miss
        ("/ /help", ("", True)),  # 第二段非空 → 未命中分支
    ],
)
def test_parse_table(input_text: str, expected: tuple[str, bool]):
    assert parse(input_text) == expected
