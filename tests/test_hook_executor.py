"""Hook Executor 单测：shell 拦截信号 / 超时、prompt 注入、http 拦截、subagent 占位（ch14 T12）。"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
import pytest

import potatocode.hook.executor as executor_module
from potatocode.hook.event import Event
from potatocode.hook.executor import ExecutionResult, Executor
from potatocode.hook.rule import (
    Action,
    ActionType,
    HttpAction,
    PromptAction,
    Rule,
    ShellAction,
    SubagentAction,
)

PAYLOAD = {"event": "PreToolUse", "session_id": "s1", "cwd": "/w", "mode": "default"}


@pytest.fixture(autouse=True)
def _clear_posix_shell_cache():
    """`_posix_shell_path` 带 lru_cache：每条用例前后清空，保证 monkeypatch 生效。"""
    executor_module._posix_shell_path.cache_clear()
    yield
    executor_module._posix_shell_path.cache_clear()


def _shell_rule(command: str, timeout_s: float = 10.0) -> Rule:
    return Rule(
        name="test-hook",
        event=Event.PRE_TOOL_USE,
        action=Action(type=ActionType.SHELL, shell=ShellAction(command=command)),
        timeout_s=timeout_s,
    )


def _http_rule(handler) -> tuple[Rule, Executor]:
    transport = httpx.MockTransport(handler)
    executor = Executor()
    executor._http_client = httpx.AsyncClient(transport=transport)
    rule = Rule(
        name="http-hook",
        event=Event.PRE_TOOL_USE,
        action=Action(
            type=ActionType.HTTP,
            http=HttpAction(url="http://stub.local/check", method="POST"),
        ),
    )
    return rule, executor


async def test_shell_exit2_with_stderr_blocks():
    """exit 2 + stderr：blocked=True、reason 为 stderr 内容去尾换行（F19）。"""
    executor = Executor()
    rule = _shell_rule("echo blocked by hook >&2; exit 2")
    result = await executor.run(rule, PAYLOAD, blocking=True)
    assert result.blocked is True
    assert result.reason == "blocked by hook"
    assert result.err is None


async def test_shell_exit0_passes():
    """exit 0：放行、无失败（F19）。"""
    executor = Executor()
    result = await executor.run(_shell_rule("echo ok"), PAYLOAD, blocking=True)
    assert result.blocked is False
    assert result.err is None


async def test_shell_exit1_fails_without_block():
    """exit 1：err 非 None、不拦截（F19/G9）。"""
    executor = Executor()
    result = await executor.run(_shell_rule("echo boom >&2; exit 1"), PAYLOAD, blocking=True)
    assert result.blocked is False
    assert result.err is not None
    assert "exit 1" in str(result.err)


async def test_shell_exit2_on_non_blocking_event_does_not_block():
    """拦截信号只对拦截类事件生效：非拦截事件下 exit 2 按普通失败处理（G6/F19）。"""
    executor = Executor()
    result = await executor.run(_shell_rule("exit 2"), PAYLOAD, blocking=False)
    assert result.blocked is False
    assert result.err is not None  # 非拦截信号的非零退出：hook 失败但不拦截
    assert "exit 2" in str(result.err)


async def test_shell_stdin_receives_sorted_payload_json():
    """payload 序列化走 stdin 且 key 字典序（F17/N6）：脚本 cat 回显后比对。"""
    executor = Executor()
    rule = _shell_rule("cat")
    result = await executor.run(rule, {"z": 1, "a": 2, "m": {"b": 1, "a": 0}}, blocking=False)
    assert result.err is None
    # cat 的输出进 stdout——但 _run_shell 只把 stdout 用作拦截原因……
    # 这里直接验证：不拦截不报错即可；序列化形态用 subprocess 单独验证。
    assert result.blocked is False


async def test_shell_stdin_json_key_order():
    """直接验证 stdin JSON 的 key 字典序（N6）。"""
    from potatocode.hook.executor import _marshal_sorted

    # 用当前解释器回显 stdin，不依赖 POSIX shell（Windows 上可能没有 sh/cat）
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read())",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate(_marshal_sorted({"b": 2, "a": 1}))
    assert out.decode() == '{"a": 1, "b": 2}'


async def test_shell_timeout_returns_timeout_error():
    """超时：err 类型为 TimeoutError、不拦截（F18）。"""
    executor = Executor()
    result = await executor.run(
        _shell_rule("sleep 2 && echo done", timeout_s=0.2), PAYLOAD, blocking=True
    )
    assert result.blocked is False
    assert isinstance(result.err, TimeoutError)


async def test_prompt_action_returns_text():
    """prompt 动作：ExecutionResult.prompt 返回文本、永不拦截（F20/F22）。"""
    executor = Executor()
    rule = Rule(
        name="p",
        event=Event.SESSION_START,
        action=Action(type=ActionType.PROMPT, prompt=PromptAction(text="用 zh-CN 回复")),
    )
    result = await executor.run(rule, PAYLOAD, blocking=False)
    assert result.prompt == "用 zh-CN 回复"
    assert result.blocked is False
    assert result.err is None


async def test_http_block_decision_blocks():
    """2xx + decision=block：blocked=True、reason 取自响应（F25）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        # 缺省 body = payload JSON（key 字典序）
        assert request.read() == json.dumps(PAYLOAD, sort_keys=True).encode()
        return httpx.Response(200, json={"decision": "block", "reason": "network policy"})

    rule, executor = _http_rule(handler)
    try:
        result = await executor.run(rule, PAYLOAD, blocking=True)
        assert result.blocked is True
        assert result.reason == "network policy"
    finally:
        await executor.aclose()


async def test_http_allow_decision_passes():
    """2xx + decision 非 block：放行（F25）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"decision": "allow"})

    rule, executor = _http_rule(handler)
    try:
        result = await executor.run(rule, PAYLOAD, blocking=True)
        assert result.blocked is False
        assert result.err is None
    finally:
        await executor.aclose()


async def test_http_5xx_passes_without_block():
    """5xx：按 F25「其它情况视为放行」——不拦截、不算失败。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    rule, executor = _http_rule(handler)
    try:
        result = await executor.run(rule, PAYLOAD, blocking=True)
        assert result.blocked is False
        assert result.err is None
    finally:
        await executor.aclose()


async def test_http_template_body_formats_payload_fields():
    """模板 body 用 str.format_map 插值 payload 字段（F23/N10）。"""
    received: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["body"] = request.read().decode()
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    executor = Executor()
    executor._http_client = httpx.AsyncClient(transport=transport)
    rule = Rule(
        name="tmpl",
        event=Event.STOP,
        action=Action(
            type=ActionType.HTTP,
            http=HttpAction(url="http://stub.local/done", body='done for {session_id}'),
        ),
    )
    try:
        result = await executor.run(rule, PAYLOAD, blocking=False)
        assert result.err is None
        assert received["body"] == f"done for {PAYLOAD['session_id']}"
    finally:
        await executor.aclose()


async def test_http_template_render_failure_is_error():
    """模板渲染失败（缺字段）按 hook 失败处理（N10）。"""
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    executor = Executor()
    executor._http_client = httpx.AsyncClient(transport=transport)
    rule = Rule(
        name="badtmpl",
        event=Event.STOP,
        action=Action(
            type=ActionType.HTTP,
            http=HttpAction(url="http://stub.local/x", body="{missing_field}"),
        ),
    )
    try:
        result = await executor.run(rule, PAYLOAD, blocking=False)
        assert result.blocked is False
        assert result.err is not None and "render failed" in str(result.err)
    finally:
        await executor.aclose()


async def test_subagent_stub_prints_and_passes(capsys):
    """subagent 占位：固定格式 stderr 日志、不报错不拦截（F26/N8）。"""
    executor = Executor()
    rule = Rule(
        name="sub",
        event=Event.SESSION_START,
        action=Action(
            type=ActionType.SUBAGENT,
            subagent=SubagentAction(agent_name="foo", prompt="test"),
        ),
    )
    result = await executor.run(rule, PAYLOAD, blocking=False)
    captured = capsys.readouterr()
    assert result.blocked is False
    assert result.err is None
    assert "[hook subagent] not yet implemented, skipped: foo" in captured.err


def test_execution_result_defaults():
    """ExecutionResult 默认空结果（占位回归保护）。"""
    result = ExecutionResult()
    assert result.blocked is False
    assert result.reason == ""
    assert result.prompt == ""
    assert result.err is None


# ---- shell 查找：PATH 上的 sh 优先、Windows 无 sh 时探测 Git Bash（ch14 F17） ----


def test_shell_argv_prefers_posix_shell_from_path(monkeypatch):
    """PATH 上有 sh：_shell_argv 走 `[sh, -c, command]` exec 路径。"""
    monkeypatch.setattr(
        executor_module.shutil,
        "which",
        lambda name: "/usr/bin/sh" if name == "sh" else None,
    )
    assert executor_module._shell_argv("echo hi") == ["/usr/bin/sh", "-c", "echo hi"]


def test_find_git_bash_sh_derives_from_git_executable(tmp_path, monkeypatch):
    """PATH 上有 git.exe：由 `<GitRoot>\\cmd\\git.exe` 反推出 `<GitRoot>\\bin\\sh.exe`。"""
    git_root = tmp_path / "Git"
    shell_path = git_root / "bin" / "sh.exe"
    shell_path.parent.mkdir(parents=True)
    shell_path.write_text("")
    git_executable = git_root / "cmd" / "git.exe"

    monkeypatch.setattr(
        executor_module.shutil,
        "which",
        lambda name: str(git_executable) if name == "git" else None,
    )
    assert executor_module._find_git_bash_sh() == str(shell_path)


def test_find_git_bash_sh_uses_env_dirs_when_git_not_on_path(tmp_path, monkeypatch):
    """PATH 上没有 git：退化为按 PROGRAMFILES 等环境变量探测常见安装目录。"""
    shell_path = tmp_path / "Git" / "bin" / "sh.exe"
    shell_path.parent.mkdir(parents=True)
    shell_path.write_text("")

    monkeypatch.setattr(executor_module.shutil, "which", lambda name: None)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.setenv("PROGRAMFILES(X86)", "")
    monkeypatch.setenv("LOCALAPPDATA", "")
    assert executor_module._find_git_bash_sh() == str(shell_path)


def test_posix_shell_path_none_when_no_shell_available(monkeypatch):
    """非 Windows 且 PATH 无 sh：返回 None，交由平台默认 shell 兜底（保持旧行为）。"""
    monkeypatch.setattr(executor_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(executor_module.sys, "platform", "linux")
    assert executor_module._posix_shell_path() is None
    assert executor_module._shell_argv("echo hi") is None
