"""compact 包单测的公共工具：脚本化 fake provider 与入参 / 会话构造。

这里的 fake provider 只服务于上下文管理单测（按调用次数返回摘要文本或异常），
与 agent 包自己的 fake provider 作用域不重合，互不依赖。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import pytest

from potatocode.compact import (
    CompactCircuitBreaker,
    ContentReplacementState,
    ManageInput,
    RecoveryState,
    SessionContext,
    TriggerKind,
    new_session_context,
)
from potatocode.conversation import Conversation
from potatocode.llm import (
    Request,
    StreamEvent,
    ToolCall,
    ToolDefinition,
    ToolResult,
)

# 一份结构完整的 9 部分摘要（含两阶段标签），作为默认的模型返回
SUMMARY_BODY = """## 1 主要请求和意图
用户要求把大工具结果落盘。
## 2 关键技术概念
上下文窗口、prompt cache。
## 3 文件和代码段
src/potatocode/compact/layer1.py
## 4 错误和修复
无
## 5 问题解决过程
先做第 1 层预防，再做第 2 层摘要。
## 6 所有用户消息原文
读文件 / 继续
## 7 待办任务
补齐测试。
## 8 当前工作
正在写上下文管理的单测。
## 9 可能的下一步
跑通全部单测。"""
DEFAULT_SUMMARY = f"<analysis>草稿内容会被丢弃</analysis>\n<summary>{SUMMARY_BODY}</summary>"


class FakeCompactProvider:
    """脚本化 provider：按调用序号返回摘要文本或异常，并记录每次请求。"""

    def __init__(
        self,
        responses: list[str | Exception] | None = None,
        repeat: str | Exception | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._repeat = repeat
        self.requests: list[Request] = []

    @property
    def name(self) -> str:
        """provider 名称（协议无关接口要求）。"""
        return "fake"

    @property
    def model(self) -> str:
        """模型名（协议无关接口要求）。"""
        return "fake-model"

    @property
    def calls(self) -> int:
        """已发起的请求总数。"""
        return len(self.requests)

    @property
    def summarize_calls(self) -> int:
        """其中摘要请求的次数（摘要请求不传任何工具定义）。"""
        return len([r for r in self.requests if not r.tools])

    async def stream(self, req: Request) -> AsyncIterator[StreamEvent]:
        """按脚本返回一轮事件：异常走 err 事件，正常文本走 text + done。"""
        idx = len(self.requests)
        self.requests.append(req)
        item: str | Exception | None
        if idx < len(self._responses):
            item = self._responses[idx]
        else:
            item = self._repeat
        if isinstance(item, Exception):
            yield StreamEvent(err=item)
            return
        yield StreamEvent(text=item if item is not None else DEFAULT_SUMMARY)
        yield StreamEvent(done=True)


def make_conversation(
    tool_contents: list[str] | None = None,
    *,
    user_text: str = "读文件",
    id_prefix: str = "t",
) -> Conversation:
    """构造一段「user → assistant(工具调用) → tool(结果)」的最小对话。"""
    conv = Conversation()
    conv.add_user(user_text)
    if tool_contents:
        calls = [
            ToolCall(id=f"{id_prefix}{i}", name="read_file", input='{"path": "a.txt"}')
            for i in range(len(tool_contents))
        ]
        conv.add_assistant_with_tool_calls("", calls)
        conv.add_tool_results(
            [
                ToolResult(tool_call_id=f"{id_prefix}{i}", content=content)
                for i, content in enumerate(tool_contents)
            ]
        )
    return conv


def make_input(
    conv: Conversation,
    provider: FakeCompactProvider,
    *,
    context_window: int = 200000,
    tool_defs: list[ToolDefinition] | None = None,
    replacement: ContentReplacementState | None = None,
    recovery: RecoveryState | None = None,
    auto_tracking: CompactCircuitBreaker | None = None,
    session: SessionContext | None = None,
    usage_anchor: int = 0,
    anchor_msg_len: int = 0,
    estimated_token: int = 0,
    trigger: TriggerKind = TriggerKind.AUTO,
) -> ManageInput:
    """按测试默认值组装一次上下文管理入参（未指定的状态就地新建）。"""
    return ManageInput(
        conv=conv,
        provider=provider,
        context_window=context_window,
        tool_defs=tool_defs if tool_defs is not None else [],
        replacement=replacement if replacement is not None else ContentReplacementState(),
        recovery=recovery if recovery is not None else RecoveryState(),
        auto_tracking=auto_tracking if auto_tracking is not None else CompactCircuitBreaker(),
        session=session if session is not None else new_session_context("."),
        usage_anchor=usage_anchor,
        anchor_msg_len=anchor_msg_len,
        estimated_token=estimated_token,
        trigger=trigger,
    )


@pytest.fixture
def make_conv() -> Callable[..., Conversation]:
    """返回最小对话构造器：可传入若干条工具结果来构造 RoleTool 消息。"""

    def _make(tool_contents: list[str] | None = None, **kwargs) -> Conversation:
        return make_conversation(tool_contents, **kwargs)

    return _make


@pytest.fixture
def summary_text() -> str:
    """默认的模型返回：两阶段标签包裹的 9 部分正式摘要。"""
    return DEFAULT_SUMMARY


@pytest.fixture
def summary_body() -> str:
    """正式摘要正文（`<summary>` 内部），供断言比对。"""
    return SUMMARY_BODY


@pytest.fixture
def session(tmp_path) -> SessionContext:
    """隔离的会话上下文：落盘目录在 tmp_path 下。"""
    return new_session_context(str(tmp_path))


@pytest.fixture
def fake_provider() -> Callable[..., FakeCompactProvider]:
    """返回 fake provider 的工厂（可按用例脚本化响应序列）。"""

    def _make(
        responses: list[str | Exception] | None = None,
        repeat: str | Exception | None = None,
    ) -> FakeCompactProvider:
        return FakeCompactProvider(responses, repeat)

    return _make


@pytest.fixture
def manage_input(session: SessionContext) -> Callable[..., ManageInput]:
    """返回 ManageInput 的构造工厂（默认注入隔离会话）。"""

    def _make(conv: Conversation, provider: FakeCompactProvider, **kwargs) -> ManageInput:
        kwargs.setdefault("session", session)
        return make_input(conv, provider, **kwargs)

    return _make


@pytest.fixture
def large_tool_results() -> Callable[[int, int], list[str]]:
    """返回「n 条、每条 size 字节」的 ASCII 工具结果构造器。"""

    def _make(count: int, size: int) -> list[str]:
        return ["x" * size for _ in range(count)]

    return _make
