"""openai 适配器单测：上下文超长错误被包装成哨兵异常投递到事件流。"""

from __future__ import annotations

import httpx
import openai
import pytest

from potatocode.config import ProviderConfig
from potatocode.llm import PromptTooLongError, Request, StreamEvent
from potatocode.llm.openai_provider import OpenAIProvider


def _bad_request(message: str, body: dict | None = None) -> openai.BadRequestError:
    """构造真实的 openai 400 异常（body 决定 error.code）。"""
    response = httpx.Response(400, request=httpx.Request("POST", "https://api.openai.com"))
    return openai.BadRequestError(message, response=response, body=body)


def _server_error() -> openai.InternalServerError:
    """构造真实的 openai 500 异常（非上下文超长）。"""
    response = httpx.Response(500, request=httpx.Request("POST", "https://api.openai.com"))
    return openai.InternalServerError("内部错误", response=response, body=None)


class _FakeCompletions:
    """模拟 chat.completions：create() 抛出预置异常。"""

    def __init__(self, err: Exception) -> None:
        self._err = err

    async def create(self, **kwargs):
        raise self._err


class _FakeChat:
    """模拟 client.chat。"""

    def __init__(self, err: Exception) -> None:
        self.completions = _FakeCompletions(err)


class _FakeClient:
    """模拟 AsyncOpenAI 客户端。"""

    def __init__(self, err: Exception) -> None:
        self.chat = _FakeChat(err)


def _provider(err: Exception) -> OpenAIProvider:
    """构造一个把 SDK 异常固定为 err 的适配器。"""
    cfg = ProviderConfig(name="t", protocol="openai", api_key="test", model="m")
    provider = OpenAIProvider(cfg)
    provider._client = _FakeClient(err)  # type: ignore[assignment]
    return provider


async def _collect(provider: OpenAIProvider) -> list[StreamEvent]:
    """消费一轮事件流（异常会走 err 事件，不向外抛）。"""
    return [ev async for ev in provider.stream(Request())]


async def test_openai_wraps_context_length_exceeded():
    """400 + code=context_length_exceeded → 包装成 PromptTooLongError 并保留原异常。"""
    original = _bad_request("too long", body={"code": "context_length_exceeded"})
    events = await _collect(_provider(original))

    err = events[-1].err
    assert isinstance(err, PromptTooLongError)
    assert err.__cause__ is original


async def test_openai_wraps_compatible_endpoint_marker():
    """兼容端点（无标准 code）按文案识别。"""
    original = _bad_request("This model's maximum context length is 128000 tokens")
    events = await _collect(_provider(original))

    assert isinstance(events[-1].err, PromptTooLongError)


async def test_openai_does_not_wrap_other_errors():
    """服务端错误不被误判。"""
    original = _server_error()
    events = await _collect(_provider(original))

    assert events[-1].err is original
    assert not isinstance(events[-1].err, PromptTooLongError)


async def test_openai_400_without_code_not_wrapped():
    """400 但 code 与文案都无关时不包装。"""
    original = _bad_request("invalid api key", body={"code": "invalid_api_key"})
    events = await _collect(_provider(original))

    assert events[-1].err is original


@pytest.mark.parametrize("code", ["context_length_exceeded"])
async def test_openai_error_code_alone_is_enough(code: str):
    """仅靠 code 命中即可识别（文案不含特征片段）。"""
    original = _bad_request("请求被拒绝", body={"code": code})
    events = await _collect(_provider(original))

    assert isinstance(events[-1].err, PromptTooLongError)


async def test_openai_stream_yields_single_err_event():
    """出错时只投递一个 err 事件。"""
    events: list[StreamEvent] = []
    async for ev in _provider(_server_error()).stream(Request()):
        events.append(ev)

    assert len(events) == 1
    assert events[0].err is not None
