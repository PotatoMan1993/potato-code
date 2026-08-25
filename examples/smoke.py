"""smoke：端到端验证系统提示工程化（缓存命中、环境感知、规划提醒，ch05）。

用法：
    python examples/smoke.py [消息1 [消息2 ...]]

从 `.potatocode/config.yaml` 取第一个 provider，同一会话依次发送每条消息；
每条消息打印该轮 token 用量（含缓存写/读）。连发两条消息可观察次轮 `cache_read > 0`
（Anthropic 稳定前缀命中），据此验证缓存策略真实生效（F4/AC4）。
"""

from __future__ import annotations

import asyncio
import sys

from potatocode import config
from potatocode.agent import Agent, Mode, Phase
from potatocode.conversation import Conversation
from potatocode.llm import new_provider
from potatocode.tool import new_default_registry

CONFIG_PATH = ".potatocode/config.yaml"


async def run_turn(agent: Agent, conv: Conversation, msg: str) -> None:
    """发一条消息，消费一轮 Agent Loop 事件流，打印文本、工具与用量。"""
    conv.add_user(msg)
    print(f"\n=== user: {msg} ===")
    async for ev in agent.run(conv, Mode.NORMAL, asyncio.Event()):
        if ev.text:
            print(ev.text, end="", flush=True)
        if ev.tool is not None:
            if ev.tool.phase is Phase.START:
                print(f"\n● {ev.tool.name}({ev.tool.args})", flush=True)
            else:
                print(f"  → {ev.tool.result[:120]}", flush=True)
        if ev.usage is not None:
            u = ev.usage
            print(
                f"\n[usage] input={u.input} output={u.output} "
                f"cache_write={u.cache_write} cache_read={u.cache_read}",
                flush=True,
            )
        if ev.notice:
            print(f"\n[notice] {ev.notice}", flush=True)
        if ev.err is not None:
            print(f"\n[error] {ev.err}", flush=True)
    print()


async def main() -> None:
    cfg = config.load(CONFIG_PATH)
    provider = new_provider(cfg.providers[0])
    registry = new_default_registry()
    agent = Agent(provider, registry, version="dev")
    conv = Conversation()

    messages = sys.argv[1:] or ["你好，请用一句话介绍你自己。"]
    for msg in messages:
        await run_turn(agent, conv, msg)


if __name__ == "__main__":
    asyncio.run(main())
