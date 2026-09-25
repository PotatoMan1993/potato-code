"""anthropic 适配器单测：上下文超长错误被包装成哨兵异常投递到事件流。"""

from __future__ import annotations

import anthropic
import httpx
import pytest

from potatocode.config import ProviderConfig
from potatocode.llm import PromptTooLongError, Request, StreamEvent
from potatocode.llm.anthropic_provider import AnthropicProvider


def _bad_request(message: str) -> anthropic.BadRequestError:
    """构造真实的 anthropic 400 异常（带 httpx 响应，供 status_code 判定）。"""
    response = httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com"))
    return anthropic.BadRequestError(message, response=response, body=None)


def _server_error() -> anthropic.InternalServerError:
    """构造真实的 anthropic 500 异常（非上下文超长）。"""
    response = httpx.Response(500, request=httpx.Request("POST", "https://api.anthropic.com"))
    return anthropic.InternalServerError("内部错误", response=response, body=None)


class _RaisingStream:
    """模拟 SDK 的流式上下文管理器：进入时抛出预置异常。"""

    def __init__(self, err: Exception) -> None:
        self._err = err

    async def __aenter__(self):
        raise self._err

    async def __aexit__(self, *exc_info) -> bool:
        return False


class _FakeMessages:
    """模拟 client.messages：stream() 返回一个立刻抛错的上下文管理器。"""

    def __init__(self, err: Exception) -> None:
        self._err = err

    def stream(self, **kwargs) -> _RaisingStream:
        return _RaisingStream(self._err)


class _FakeClient:
    """模拟 AsyncAnthropic 客户端，只提供 messages.stream。"""

    def __init__(self, err: Exception) -> None:
        self.messages = _FakeMessages(err)


def _provider(err: Exception) -> AnthropicProvider:
    """构造一个把 SDK 异常固定为 err 的适配器。"""
    cfg = ProviderConfig(name="t", protocol="anthropic", api_key="test", model="m")
    provider = AnthropicProvider(cfg)
    provider._client = _FakeClient(err)  # type: ignore[assignment]
    return provider


async def _collect(provider: AnthropicProvider) -> list[StreamEvent]:
    """消费一轮事件流（异常会走 err 事件，不向外抛）。"""
    return [ev async for ev in provider.stream(Request())]


async def test_anthropic_wraps_prompt_too_long():
    """400 + 'prompt is too long' → 包装成 PromptTooLongError，原异常留在 __cause__。"""
    original = _bad_request("prompt is too long: 300000 tokens > 200000 maximum")
    events = await _collect(_provider(original))

    err = events[-1].err
    assert isinstance(err, PromptTooLongError)
    assert err.__cause__ is original


async def test_anthropic_does_not_wrap_other_errors():
    """鉴权 / 服务端错误不被误判为上下文超长。"""
    original = _server_error()
    events = await _collect(_provider(original))

    err = events[-1].err
    assert err is original
    assert not isinstance(err, PromptTooLongError)


async def test_anthropic_400_without_marker_not_wrapped():
    """同为 400 但文案无关（参数非法）时不包装。"""
    original = _bad_request("invalid request: unknown model")
    events = await _collect(_provider(original))

    assert events[-1].err is original


@pytest.mark.parametrize("marker", ["prompt is too long", "context_length exceeded"])
async def test_anthropic_wraps_both_markers(marker: str):
    """两种常见文案都能识别。"""
    events = await _collect(_provider(_bad_request(marker)))
    assert isinstance(events[-1].err, PromptTooLongError)


async def test_anthropic_stream_yields_single_err_event():
    """出错时只投递一个 err 事件（不继续吐 done）。"""
    events: list[StreamEvent] = []
    async for ev in _provider(_bad_request("prompt is too long")).stream(Request()):
        events.append(ev)

    assert len(events) == 1
    assert events[0].err is not None
