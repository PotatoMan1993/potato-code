"""仅归一明确的输入窗口超限，不把网络或输出截断当成可压缩错误。"""

from __future__ import annotations

from collections.abc import Mapping


class ContextWindowExceeded(Exception):
    """输入超过端点上下文窗口；不保存可能含凭据的原始响应。"""

    def __init__(self, message: str = "请求输入超过上下文窗口", code: str = "prompt_too_long"):
        super().__init__(message)
        self.code = code


def classify_error(error: Exception, protocol: str) -> Exception:
    """根据状态、错误码和受限短语分类；其它错误保持原异常。"""
    if isinstance(error, ContextWindowExceeded):
        return error
    if getattr(error, "status_code", None) != 400:
        return error
    body = getattr(error, "body", None)
    if not isinstance(body, Mapping):
        return error
    detail = body.get("error", body)
    if not isinstance(detail, Mapping):
        return error
    code = detail.get("code")
    kind = detail.get("type")
    message = str(detail.get("message", "")).lower()
    if code in ("context_length_exceeded", "prompt_too_long") or kind == "prompt_too_long":
        return ContextWindowExceeded()
    # 参数上限本身非法不能靠摘要解决，也不使用原响应构造展示信息。
    if any(term in message for term in ("max_tokens", "max_completion_tokens", "output limit")):
        if not any(term in message for term in ("prompt", "messages", "requested")):
            return error
    phrases = ("prompt is too long", "maximum context length", "input is too long")
    if (protocol == "anthropic" and kind == "invalid_request_error") or (
        protocol == "openai" and kind in ("invalid_request_error", None)
    ):
        if any(phrase in message for phrase in phrases):
            return ContextWindowExceeded()
    return error
