"""Fork 式子 Agent 的公共启动函数（ch15 G10/F33/T31）。

Skill fork（ch13）与潜在的其它 Fork 调用方统一经 `launch_fork` 走 SubAgent
底座：受限注册中心 + 全新运行时 + `run_to_completion` 前台同步执行。放在
agent 包而非 subagent 包，是因为它要构造 Agent（subagent 包不依赖 agent，
依赖方向保持单向）；放在本函数内的深度标记让 Fork 出去的子 Agent 同样被
嵌套阻断覆盖（F24）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..conversation import Conversation
from ..hook import Engine as HookEngine
from ..llm import Provider
from ..permission import Mode
from ..permission.engine import Engine
from ..tool import Registry
from . import Agent
from .permission_upgrade import ApprovalUpgrader
from .runtime import SessionRuntime, new_sub_runtime
from .subagent_context import SUBAGENT_DEPTH


@dataclass
class ForkLaunchOpts:
    """一次 Fork 启动的全部入参（任务文本已预装填进 `conv`，见 F22/T28）。"""

    provider: Provider  # 子 Agent 沿用的 provider
    registry: Registry  # 已按调用方约束过滤好的子注册中心（如剔除 LoadSkill）
    engine: Engine  # 权限引擎（fork 内审批自动拒绝，见 run_to_completion）
    version: str  # 应用版本号（环境段用）
    conv: Conversation  # 已装填任务/上下文的子对话（Fork Boilerplate 由调用方注入）
    parent_runtime: SessionRuntime  # 父运行时（沿用会话标识与上下文窗口，compact 状态全新）
    hook_engine: HookEngine | None = None  # Hook 引擎（子 Agent 内继续分派）
    system_prompt: str | None = None  # None = 继承主系统提示（N2）
    max_turns: int = 0  # 0 = 全局 MAX_ITERATIONS
    permission_mode: Mode | None = None  # 子 Agent 权限模式兜底（F10）
    dont_ask: bool = False  # dontAsk 档（F12-②）
    approval_upgrader: ApprovalUpgrader | None = None  # Ask 升级回调（F13）


async def launch_fork(opts: ForkLaunchOpts) -> str:
    """启动 Fork 式子 Agent 并前台同步跑完，返回最终文本（F5/G10）。

    异常透传给调用方（SkillExecutor 已有错误文本兜底）；`run_to_completion`
    的 task 传空串——Fork 会话的任务文本已由调用方装填（T28 方案一）。
    """
    sub_agent = Agent(
        opts.provider,
        opts.registry,
        opts.engine,
        opts.version,
        runtime=new_sub_runtime(opts.parent_runtime),
        hook_engine=opts.hook_engine,
        system_prompt=opts.system_prompt,
        max_turns=opts.max_turns,
        permission_mode=opts.permission_mode,
        dont_ask=opts.dont_ask,
        approval_upgrader=opts.approval_upgrader,
    )
    # 深度标记：Fork 子 Agent 内再调 Agent 工具会被结构化拒绝（F24）
    token = SUBAGENT_DEPTH.set(SUBAGENT_DEPTH.get() + 1)
    try:
        return await sub_agent.run_to_completion(opts.conv, "")
    finally:
        SUBAGENT_DEPTH.reset(token)


__all__ = ["ForkLaunchOpts", "launch_fork"]
