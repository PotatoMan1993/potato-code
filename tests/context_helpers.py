"""上下文测试用受控 provider；不访问网络或真实配置。"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from dataclasses import asdict

from potatocode.conversation import Conversation
from potatocode.llm import BudgetShape, ContextLimits, StopReason, StreamEvent, Usage
from potatocode.llm.usage import canonical_json


def summary_json(*, quotes=(), draft="不应出现在历史里的草稿", **changes):
    body = {
        "intent": "继续完成用户任务",
        "approach": "检查后逐步修改",
        "technical_points": "无",
        "files_and_code": "无",
        "user_quotes": list(quotes),
        "unfinished": "继续验证",
        "current_work": "整理上下文",
        "next_steps": "执行下一步",
    }
    body.update(changes)
    return json.dumps({"draft": draft, "summary": body}, ensure_ascii=False)


def summary_events(text=None):
    return [
        StreamEvent(text=summary_json() if text is None else text),
        StreamEvent(
            usage=Usage(100, 40, raw_counts={"prompt_tokens": 100, "completion_tokens": 40})
        ),
        StreamEvent(done=True, stop_reason=StopReason.STOP),
    ]


class ContextProvider:
    def __init__(self, *, window=128000, output=4096, summary_scripts=(), normal_scripts=()):
        self.name = "context-test"
        self.model = "model-test"
        self.protocol = "openai"
        self.identity = uuid.uuid4().hex
        self.context_limits = ContextLimits(window, output, 20000)
        self.summary_scripts = list(summary_scripts)
        self.normal_scripts = list(normal_scripts)
        self.requests = []
        self.waiting = asyncio.Event()
        self.closed = 0

    def budget_shape(self, req):
        return BudgetShape(
            fixed=(
                canonical_json(asdict(req.system)),
                canonical_json([asdict(t) for t in req.tools]),
            ),
            messages=tuple((canonical_json(asdict(m)),) for m in req.messages),
            trailing=(canonical_json({"role": "user", "content": req.reminder}),)
            if req.reminder
            else (),
        )

    async def stream(self, req):
        self.requests.append(copy.deepcopy(req))
        scripts = self.summary_scripts if req.purpose == "summary" else self.normal_scripts
        script = (
            scripts.pop(0)
            if scripts
            else (
                summary_events()
                if req.purpose == "summary"
                else [StreamEvent(text="完成"), StreamEvent(done=True, stop_reason=StopReason.STOP)]
            )
        )
        if callable(script):
            script = script(req)
        try:
            for event in script:
                if isinstance(event, asyncio.Event):
                    self.waiting.set()
                    await event.wait()
                else:
                    yield event
        finally:
            self.closed += 1


def substantial_conversation(count=12, size=2200):
    conv = Conversation()
    for i in range(count):
        text = f"第{i}条：" + "文" * size
        if i % 2:
            conv.add_assistant(text)
        else:
            conv.add_user(text)
    return conv


def summary_prefix_length(request):
    return len(json.loads(request.messages[0].content)["transcript"])
