"""上下文超出窗口错误的识别与包装：把各协议的原生异常统一成 PromptTooLongError。

识别只按"400 + 特征文案/code"判断，不做模糊匹配，避免把限流、鉴权等其他错误
误判成上下文超长；原异常挂在包装异常的 `__cause__` 上供调试。
"""

from __future__ import annotations

import anthropic
import openai

from . import PromptTooLongError

# anthropic 上下文超长时错误文案里出现的特征片段（小写比较）
_ANTHROPIC_MARKERS = ("prompt is too long", "context_length", "context length")
# openai 上下文超长时 error.code 的取值
_OPENAI_CODE = "context_length_exceeded"


def _status_of(err: BaseException) -> int | None:
    """取异常携带的 HTTP 状态码（SDK 异常与 mock 对象都兼容）。"""
    status = getattr(err, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(err, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def is_anthropic_prompt_too_long(err: BaseException) -> bool:
    """判断 anthropic 异常是否为"上下文超出窗口"（400 且文案含特征片段）。"""
    if not isinstance(err, anthropic.BadRequestError) and _status_of(err) != 400:
        return False
    text = str(err).lower()
    return any(marker in text for marker in _ANTHROPIC_MARKERS)


def is_openai_prompt_too_long(err: BaseException) -> bool:
    """判断 openai 异常是否为"上下文超出窗口"（400 且 code 为超长码）。"""
    if not isinstance(err, openai.BadRequestError) and _status_of(err) != 400:
        return False
    code = getattr(err, "code", None)
    if code == _OPENAI_CODE:
        return True
    text = str(err).lower()
    return any(marker in text for marker in _ANTHROPIC_MARKERS)


def wrap_prompt_too_long(err: Exception, detail: str = "") -> PromptTooLongError:
    """把原始异常包装成 PromptTooLongError，原异常保留在 `__cause__`。"""
    wrapped = PromptTooLongError(detail or str(err))
    wrapped.__cause__ = err
    return wrapped
