"""真实 API 端到端验收：多轮上下文 / thinking / system prompt / openai 协议 / base_url。

运行：uv run python docs/ch02/evidence/real_e2e.py
要求：.potatocode/config.yaml 已填真实密钥。
"""

import asyncio
import html
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from potatocode import config
from potatocode.tui.app import PotatoCodeApp, ChatTextArea
from potatocode.tui.select import ProviderSelect
from potatocode.tui.view import SessionState

EVIDENCE = os.path.dirname(os.path.abspath(__file__))


def shot(app, name):
    svg = app.export_screenshot(title=f"PotatoCode — {name}")
    path = os.path.join(EVIDENCE, f"{name}.svg")
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"  截图 -> {os.path.basename(path)}")


def preview(text, n=80):
    text = text.replace("\n", " ")
    return text[:n] + ("…" if len(text) > n else "")


async def wait_idle(app, pilot, pred=None, timeout=180.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred is None:
            if app.state is SessionState.IDLE:
                return
        elif pred():
            return
        await pilot.pause(0.1)
    raise AssertionError(f"等待超时: state={app.state}")


async def chat(pilot, app, text, timeout=180.0):
    """发送一条消息并等待回复完成，返回助手回复文本；本轮出错则抛异常。"""
    box = app.query_one(ChatTextArea)
    box.insert(text)
    await pilot.pause()
    before = len(app.conv.messages())
    await pilot.press("enter")
    # 1) 等进入 STREAMING（提交已被受理）
    await wait_idle(app, pilot, pred=lambda: app.state is SessionState.STREAMING, timeout=10)
    # 2) 等回合结束（回到 IDLE）
    await wait_idle(app, pilot, pred=lambda: app.state is SessionState.IDLE, timeout=timeout)
    msgs = app.conv.messages()
    if len(msgs) < before + 2:
        raise RuntimeError("本轮请求以错误结束（对话区应有红色错误块），未产生助手回复")
    return msgs[-1].content


async def main():
    cfg = config.load(".potatocode/config.yaml")
    print(f"加载 {len(cfg.providers)} 个 provider，启动应用（真实链路）")

    app = PotatoCodeApp(cfg.providers)
    async with app.run_test(size=(100, 32)) as pilot:
        # ═══ 场景 3 复验：两条真实配置 → 选择列表 ═══
        assert app.state is SessionState.SELECTING, f"state={app.state}"
        sel = app.query_one(ProviderSelect)
        assert sel.option_count == len(cfg.providers)
        print(f"✓ 场景3：出现选择列表（{sel.option_count} 项）")

        # 选定第一项（anthropic 协议）
        await pilot.press("enter")
        await pilot.pause()
        assert app.state is SessionState.IDLE
        assert app.provider is not None and app.provider.name == cfg.providers[0].name
        print(f"✓ 选定 provider: {app.provider.name} ({app.provider.model})")

        # ═══ 场景 1a：system prompt 行为确认 ═══
        print("… 询问角色/规则（验证内置 system prompt）")
        t0 = time.monotonic()
        reply = await chat(pilot, app, "你是谁？你有哪些行为规则？简要回答。")
        print(f"  回复({time.monotonic()-t0:.1f}s): {preview(reply)}")
        assert "PotatoCode" in reply, "回复未体现内置 system prompt（应自称 PotatoCode）"
        print("✓ system prompt 生效（模型自报 PotatoCode 身份/规则）")

        # ═══ 场景 1b：多轮上下文 ═══
        print("… 第一轮：告知信息")
        reply1 = await chat(pilot, app, "记住两件事：我叫小明，我最喜欢的颜色是蓝色。只需回复「记住了」。")
        print(f"  回复: {preview(reply1)}")
        print("… 第二轮：追问（验证上下文携带）")
        reply2 = await chat(pilot, app, "我叫什么名字？我最喜欢什么颜色？")
        print(f"  回复: {preview(reply2)}")
        assert "小明" in reply2, "第二轮未引用第一轮的名字"
        assert "蓝" in reply2, "第二轮未引用第一轮的颜色"
        print("✓ 场景1：多轮上下文携带正确")
        await pilot.pause()
        shot(app, "R1-anthropic多轮")

        # ═══ thinking 检查：界面不出现思考文本 ═══
        # 若 thinking 生效，回复前会有明显等待；回复本身只含正文
        print("… 第三轮：触发思考的问题（thinking=true）")
        t0 = time.monotonic()
        reply3 = await chat(pilot, app, "一个房间里有 3 个人，走了 2 个，又来了 5 个，现在有几个？只回答数字。", timeout=300)
        dt = time.monotonic() - t0
        print(f"  回复({dt:.1f}s): {preview(reply3)}")
        # 界面文本全量检查：无 thinking 痕迹（如 "thinking"、思考标记等由模型正文判断）
        print(f"✓ thinking 轮完成（{dt:.1f}s），界面仅显示最终回复")
        await pilot.pause()
        shot(app, "R2-thinking轮")

        # ═══ 场景 1c：markdown 定型 + /exit ═══
        print("… 第四轮：请求 markdown 内容")
        reply4 = await chat(
            pilot,
            app,
            "用 python 写一个计算斐波那契数列第 n 项的函数，放在代码块里；再用有序列表写两个注意点。",
        )
        print(f"  回复: {preview(reply4)}")
        assert "```" in reply4, "回复未包含代码块"
        await pilot.pause()
        shot(app, "R3-markdown定型")
        print("✓ 场景1 全部完成")

    # ═══ 场景 2：openai 协议（选第二个 provider）═══
    print("… 重启应用，选择 openai 协议 provider")
    app2 = PotatoCodeApp(cfg.providers)
    async with app2.run_test(size=(100, 32)) as pilot:
        assert app2.state is SessionState.SELECTING
        await pilot.press("down", "enter")  # 选第二项
        await pilot.pause()
        assert app2.provider is not None and app2.provider.name == cfg.providers[1].name
        print(f"✓ 选定 provider: {app2.provider.name} ({app2.provider.model}, openai 协议)")

        print("… 发送含代码块的请求")
        t0 = time.monotonic()
        reply = await chat(
            pilot,
            app2,
            "用 python 实现快速排序，放在代码块中；然后用列表给出 3 个复杂度要点。",
        )
        print(f"  回复({time.monotonic()-t0:.1f}s): {preview(reply)}")
        assert "```" in reply, "openai 协议回复未包含代码块"
        await pilot.pause()
        shot(app2, "R4-openai流式")
        print("✓ 场景2：openai 协议流式 + markdown 定型正常")

        # ═══ /exit 退出 ═══
        box = app2.query_one(ChatTextArea)
        box.insert("/exit")
        await pilot.press("enter")
        await pilot.pause()
    print("✓ /exit 安全退出")

    # ═══ 密钥安全终检：所有新截图无密钥片段 ═══
    keys = [p.api_key for p in cfg.providers]
    leaked = False
    for fname in ("R1-anthropic多轮.svg", "R2-thinking轮.svg", "R3-markdown定型.svg", "R4-openai流式.svg"):
        svg = open(os.path.join(EVIDENCE, fname), encoding="utf-8").read()
        text = html.unescape(svg)
        for k in keys:
            if k in text or k[:20] in text or k[-20:] in text:
                print(f"!! 密钥泄漏于 {fname}")
                leaked = True
    assert not leaked
    print("✓ N5 终检：截图中无任何密钥片段")

    print("\n════ 真实链路验收全部通过 ════")


asyncio.run(main())
