"""Hook 动作执行器：shell / prompt / http / subagent 四类动作的执行入口（ch14 G7）。

约定（ch14 G9/F19/F25）：
- 拦截类事件下，shell 以 exit code 2 表达拦截（stderr 优先作为拒绝原因）；
- http 以 2xx 响应体 `{"decision":"block","reason":...}` 表达拦截；
- prompt 动作永不拦截，只返回待注入文本；
- hook 自身失败（非拦截信号的非零退出、网络错、超时等）只返回 err，不中断 Agent。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from dataclasses import dataclass

import httpx

from .rule import ActionType, HttpAction, Payload, PromptAction, Rule, ShellAction, SubagentAction


@dataclass
class ExecutionResult:
    """单个 hook 动作的执行结果：拦截信号、注入文本与失败原因三选一（可空）。"""

    blocked: bool = False
    reason: str = ""  # blocked=True 时的拒绝原因
    prompt: str = ""  # 仅 prompt 动作非空
    err: Exception | None = None  # hook 自身失败（不拦截）


def _marshal_sorted(payload: Payload) -> bytes:
    """把 payload 序列化成 key 字典序稳定的单行 JSON（ch14 N6），供 stdin / 请求体。"""
    return json.dumps(payload, sort_keys=True).encode()


def _shell_argv(command: str) -> list[str] | None:
    """解析 shell 启动参数（ch14 F17：命令由 sh -c 解释执行）。

    PATH 上有 sh（类 Unix 或 Git Bash）时返回 `["sh", "-c", command]` 走 exec；
    否则返回 None，由 `asyncio.create_subprocess_shell` 用平台默认 shell 兜底。
    """
    sh_path = shutil.which("sh")
    if sh_path:
        return [sh_path, "-c", command]
    return None


class Executor:
    """四类动作的执行器；HTTP 客户端单实例复用连接池。"""

    def __init__(self) -> None:
        self._http_client = httpx.AsyncClient(timeout=30.0)

    async def aclose(self) -> None:
        """关闭底层 HTTP 客户端（进程退出路径调用，避免资源警告）。"""
        await self._http_client.aclose()

    async def run(self, rule: Rule, payload: Payload, *, blocking: bool) -> ExecutionResult:
        """按动作类型分发执行；blocking 表示当前事件是拦截类（ch14 G6）。"""
        action = rule.action
        if action.type is ActionType.SHELL and action.shell is not None:
            return await self._run_shell(action.shell, payload, blocking, rule.timeout_s)
        if action.type is ActionType.PROMPT and action.prompt is not None:
            return self._run_prompt(action.prompt)
        if action.type is ActionType.HTTP and action.http is not None:
            return await self._run_http(action.http, payload, blocking, rule.timeout_s)
        if action.type is ActionType.SUBAGENT and action.subagent is not None:
            return self._run_subagent(action.subagent)
        return ExecutionResult(err=RuntimeError(f"unknown action type: {action.type}"))

    async def _run_shell(
        self, shell_action: ShellAction, payload: Payload, blocking: bool, timeout_s: float
    ) -> ExecutionResult:
        """执行 shell 命令：payload JSON 走 stdin，exit 2 表达拦截（ch14 F17-F19）。"""
        shell_argv = _shell_argv(shell_action.command)
        try:
            if shell_argv is not None:
                proc = await asyncio.create_subprocess_exec(
                    *shell_argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    shell_action.command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
        except OSError as e:  # shell 起不来：按 hook 失败处理，不拦截
            return ExecutionResult(err=RuntimeError(f"spawn failed: {e}"))
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(_marshal_sorted(payload)), timeout=timeout_s
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()  # 回收子进程避免僵尸（ch14 F18 超时按失败处理）
            return ExecutionResult(err=TimeoutError(f"command timed out after {timeout_s}s"))

        stderr_text = stderr_bytes.decode(errors="replace")
        stdout_text = stdout_bytes.decode(errors="replace")
        returncode = proc.returncode if proc.returncode is not None else -1

        if blocking and returncode == 2:
            # 拦截命中：stderr 优先作为拒绝原因，合并去尾换行（ch14 F19）
            reason = (stderr_text or stdout_text).rstrip("\n")
            return ExecutionResult(blocked=True, reason=reason)
        if returncode == 0:
            return ExecutionResult()
        # 其它非零退出：hook 失败但不拦截（ch14 F19/F29）
        return ExecutionResult(err=RuntimeError(f"exit {returncode}: {stderr_text}".rstrip()))

    def _run_prompt(self, prompt_action: PromptAction) -> ExecutionResult:
        """prompt 动作：直接返回待注入文本，永不表达拦截（ch14 F20/F22）。"""
        return ExecutionResult(prompt=prompt_action.text)

    async def _run_http(
        self, http_action: HttpAction, payload: Payload, blocking: bool, timeout_s: float
    ) -> ExecutionResult:
        """发 HTTP 请求：缺省 body 为 payload JSON；2xx + block 决策表达拦截（ch14 F23-F25）。"""
        if http_action.body is None:
            body_text = _marshal_sorted(payload).decode()
        else:
            try:
                # str.format_map 只做最基本的字段插值，不开放函数调用（ch14 N10）
                body_text = http_action.body.format_map(payload)
            except (KeyError, IndexError, ValueError) as e:
                return ExecutionResult(err=RuntimeError(f"body template render failed: {e}"))

        method = http_action.method or "POST"
        try:
            response = await self._http_client.request(
                method,
                http_action.url,
                content=body_text.encode(),
                headers=http_action.headers,
                timeout=timeout_s,
            )
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            # 网络错 / 超时：hook 失败但不拦截（ch14 F25/F29）
            return ExecutionResult(err=RuntimeError(f"http {method} failed: {e}"))

        if not (200 <= response.status_code < 300):
            # 非 2xx：视为放行（ch14 F25「其它情况视为放行」），不算失败
            return ExecutionResult()
        if not blocking:
            return ExecutionResult()
        try:
            decision_body = response.json()
        except ValueError as e:  # JSON 解析失败按 hook 失败处理，不拦截（ch14 F25）
            return ExecutionResult(err=RuntimeError(f"response body not json: {e}"))
        if isinstance(decision_body, dict) and decision_body.get("decision") == "block":
            reason = str(decision_body.get("reason", ""))
            return ExecutionResult(blocked=True, reason=reason)
        # body 缺 decision 字段 / decision 非 block：视为放行（ch14 F25）
        return ExecutionResult()

    def _run_subagent(self, subagent_action: SubagentAction) -> ExecutionResult:
        """subagent 占位：固定格式 stderr 日志后直接返回（ch14 F26/N8）。"""
        print(
            f"[hook subagent] not yet implemented, skipped: {subagent_action.agent_name}",
            file=sys.stderr,
        )
        return ExecutionResult()


__all__ = ["ExecutionResult", "Executor"]
