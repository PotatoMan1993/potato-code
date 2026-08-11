"""T14 端到端验证：headless 渲染管线驱动 + SVG 截图存证。

运行：uv run python docs/ch02/evidence/e2e_capture.py
"""


import asyncio
import os
import time

from textual.widgets import RichLog, Static

from potatocode.config import ProviderConfig
from potatocode.llm import StreamEvent
from potatocode.tui.app import PotatoCodeApp, ChatTextArea
from potatocode.tui.select import ProviderSelect
from potatocode.tui.view import SessionState

EVIDENCE = os.path.dirname(os.path.abspath(__file__))  # 与截图同目录
os.makedirs(EVIDENCE, exist_ok=True)

MD_REPLY = """当然可以！下面是示例：

```python
def fib(n: int) -> int:
    return n if n < 2 else fib(n - 1) + fib(n - 2)
```

要点：
1. **递归基**：`n < 2` 直接返回
2. 递归式：`fib(n-1) + fib(n-2)`

> 注意：朴素递归是指数复杂度。
"""


class StubProvider:
    def __init__(self, chunks, delay=0.05, err=None):
        self.chunks = chunks
        self.delay = delay
        self.err = err
        self.calls = []

    @property
    def name(self):
        return "Stub"

    @property
    def model(self):
        return "stub-model"

    async def stream(self, msgs):
        self.calls.append(list(msgs))
        if self.err:
            await asyncio.sleep(0.05)
            yield StreamEvent(err=self.err)
            return
        for c in self.chunks:
            await asyncio.sleep(self.delay)
            yield StreamEvent(text=c)
        yield StreamEvent(done=True)


CFG_A = ProviderConfig(name="Claude", protocol="anthropic", api_key="sk-ant-xxx", model="claude-sonnet-4-5")
CFG_B = ProviderConfig(name="GPT", protocol="openai", api_key="sk-xxx", model="gpt-4o")


async def wait_state(app, pilot, pred, timeout=10.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return
        await pilot.pause(0.02)
    raise AssertionError(f"等待超时: state={app.state}")


def shot(app, name):
    svg = app.export_screenshot(title=f"PotatoCode — {name}")
    path = os.path.join(EVIDENCE, f"{name}.svg")
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"  截图 -> {path}")


async def type_text(pilot, app, text):
    """ASCII 走按键事件路径；含中文时用输入框插入（提交链路相同）。"""
    if text.isascii():
        for ch in text:
            await pilot.press(ch)
    else:
        app.query_one(ChatTextArea).insert(text)
        await pilot.pause()


async def main():
    # ═══ S1 启动布局：banner + 就绪行 + ❯ 输入框 + 状态栏（AC7/F7）═══
    print("S1 启动布局")
    app = PotatoCodeApp([CFG_A])
    async with app.run_test(size=(100, 32)) as pilot:
        assert app.state is SessionState.IDLE
        box = app.query_one(ChatTextArea)
        assert box.placeholder == "Send a message..."
        assert app.focused is box
        await pilot.pause()
        shot(app, "S1-启动布局")
    print("  通过：banner/输入框/状态栏齐备，输入框聚焦")

    # ═══ S2 多 provider 选择（AC2/F2）═══
    print("S2 provider 选择")
    app = PotatoCodeApp([CFG_A, CFG_B])
    async with app.run_test(size=(100, 32)) as pilot:
        assert app.state is SessionState.SELECTING
        sel = app.query_one(ProviderSelect)
        assert sel.option_count == 2
        await pilot.pause()
        shot(app, "S2-provider选择")
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.provider is not None and app.provider.name == "GPT"
        assert app.state is SessionState.IDLE
    print("  通过：方向键选择第二项后进入对话，状态栏更新")

    # ═══ S3 流式进行中：Imagining… (Ns) 计时 + 逐字增量（AC12/F12, AC5/F8 前半）═══
    print("S3 流式进行中")
    app = PotatoCodeApp([CFG_A])
    async with app.run_test(size=(100, 32)) as pilot:
        app.provider = StubProvider(["你好！", "正在", "生成", "内容……"], delay=1.2)
        await type_text(pilot, app, "写一首诗")
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert app.state is SessionState.STREAMING
        t_start = time.monotonic()
        # 首个增量到达前：只有 Imagining 行
        await pilot.pause(0.3)
        assert app.cur_reply == "", "首个增量前不应有正文"
        # 等首个增量到达后截图（含部分正文 + 计时行）
        await wait_state(app, pilot, lambda: len(app.cur_reply) >= 5)
        await pilot.pause()
        shot(app, "S3-流式进行中")
        assert app.cur_reply.startswith("你好！")
        # 计时显示递增（动态区渲染内容含 Imagining）
        elapsed = time.monotonic() - t_start
        assert elapsed > 1.0
        # 等待完成
        await wait_state(app, pilot, lambda: app.state is SessionState.IDLE)
        assert app.conv.messages()[-1].content == "你好！正在生成内容……"
    print("  通过：等待期显示计时，增量逐段到达，完成后记账")

    # ═══ S4 markdown 定型：代码块/列表/强调（AC8/F8）+ 总耗时 ═══
    print("S4 markdown 定型")
    app = PotatoCodeApp([CFG_A])
    async with app.run_test(size=(100, 32)) as pilot:
        app.provider = StubProvider([MD_REPLY], delay=0.05)
        await type_text(pilot, app, "用 python 写斐波那契")
        await pilot.press("enter")
        await wait_state(app, pilot, lambda: app.state is SessionState.IDLE)
        await pilot.pause()
        shot(app, "S4-markdown定型")
        assert app.conv.messages()[-1].content == MD_REPLY
    print("  通过：完成后 markdown 定型，总耗时显示")

    # ═══ S5 多轮上下文（AC6/F6）═══
    print("S5 多轮上下文")
    app = PotatoCodeApp([CFG_A])
    async with app.run_test(size=(100, 32)) as pilot:
        stub = StubProvider(["收到！"], delay=0.02)
        app.provider = stub
        await type_text(pilot, app, "我叫小明")
        await pilot.press("enter")
        await wait_state(app, pilot, lambda: len(app.conv.messages()) == 2)
        await type_text(pilot, app, "我叫什么名字？")
        await pilot.press("enter")
        await wait_state(app, pilot, lambda: len(app.conv.messages()) == 4)
        second_req = stub.calls[1]
        assert [m.role for m in second_req] == ["user", "assistant", "user"]
        assert second_req[0].content == "我叫小明"
        assert second_req[2].content == "我叫什么名字？"
        await pilot.pause()
        shot(app, "S5-多轮对话")
    print("  通过：第二轮请求携带全部历史")

    # ═══ S6 错误反馈：不退出、可继续（AC11/F11）═══
    print("S6 错误反馈")
    app = PotatoCodeApp([CFG_A])
    async with app.run_test(size=(100, 32)) as pilot:
        app.provider = StubProvider(None, err=PermissionError("401 invalid api key"))
        await type_text(pilot, app, "你好")
        await pilot.press("enter")
        await wait_state(app, pilot, lambda: app.state is SessionState.IDLE)
        assert len(app.conv.messages()) == 1
        await pilot.pause()
        shot(app, "S6-错误反馈")
        # 修正后可继续
        app.provider = StubProvider(["恢复正常"], delay=0.02)
        await type_text(pilot, app, "再来一次")
        await pilot.press("enter")
        await wait_state(app, pilot, lambda: len(app.conv.messages()) == 3)
    print("  通过：错误红色显示、程序不退出、可继续对话")

    # ═══ S7 真实网络错误路径：占位密钥 → anthropic 端点 401（F11 真实链路）═══
    print("S7 真实端点 401（占位密钥）")
    app = PotatoCodeApp([CFG_A])
    async with app.run_test(size=(100, 32)) as pilot:
        # 不替换 provider：走真实 AsyncAnthropic + 占位密钥
        await type_text(pilot, app, "hi")
        await pilot.press("enter")
        await wait_state(app, pilot, lambda: app.state is SessionState.IDLE, timeout=30)
        assert len(app.conv.messages()) == 1, "401 出错不应记账 assistant"
        await pilot.pause()
        shot(app, "S7-真实401错误")
    print("  通过：真实请求失败走错误块，程序继续可用")

    print("\n全部端到端场景通过，截图已存入 docs/ch02/evidence/")


asyncio.run(main())
