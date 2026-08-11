"""核对真实验收截图的渲染内容（临时脚本）。"""

import glob
import html
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))


def squash(path):
    svg = open(path, encoding="utf-8").read()
    ts = re.findall(r"<text[^>]*>([^<]*)</text>", svg)
    return re.sub(r"\s+", "", " ".join(html.unescape(t) for t in ts).replace(" ", " "))


checks = {
    "R1-anthropic多轮.svg": ["PotatoCode", "记住了", "小明", "蓝色", "⏱", "Claude", "qwen3.8-max"],
    "R2-thinking轮.svg": ["6", "⏱", "小明"],
    "R3-markdown定型.svg": ["fib", "ValueError", "⏱"],
    "R4-openai流式.svg": ["quick_sort", "⏱", "GPT"],
}
ok = True
for fname, keys in checks.items():
    path = os.path.join(HERE, fname)
    if not os.path.exists(path):
        print(f"{fname}: 文件不存在")
        ok = False
        continue
    content = squash(path)
    missing = [k for k in keys if re.sub(r"\s+", "", k) not in content]
    if missing:
        ok = False
    print(f"{fname}: {'通过' if not missing else '缺失 ' + str(missing)}")
print("总体:", "全部通过" if ok else "有问题")
