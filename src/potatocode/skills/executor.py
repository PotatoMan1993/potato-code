"""Skill 执行器：fork 隔离子 Agent 执行后回流（ch13 F7）。

inline skill 不经执行器：斜杠命令把 SOP 作为 user 消息注入，LoadSkill 工具把
SOP 作为 toolResult 返回。执行器只负责 fork 路径（execute_fork 同步取结果 /
spawn_fork 后台启动回流）。fork 对宿主（TUI App）的依赖经 `SkillHost` 窄协议
在执行期注入，skills 包不反向依赖 TUI；对 Agent 类的引用用局部 import，避免
agent ↔ skills 循环依赖（spec 5）。ch15 起子 Agent 的构造与跑动统一复用
`agent.launch_fork`（G10/F33）：受限注册中心、全新运行时、run_to_completion、
审批自动拒绝与嵌套阻断全部走 SubAgent 底座。
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import TYPE_CHECKING, Protocol

from ..compact import (
    CompactCircuitBreaker,
    ContentReplacementState,
    ManageInput,
    RecoveryState,
    TriggerKind,
)
from ..compact.layer2 import summarize_once
from ..conversation import Conversation
from ..llm import ROLE_ASSISTANT, ROLE_USER, Message
from .loader import SkillLoader
from .parser import DEFAULT_SKILL_CONTEXT, VALID_SKILL_CONTEXTS, SkillDef, substitute_arguments

if TYPE_CHECKING:
    from ..agent import Agent
    from ..agent.runtime import SessionRuntime
    from ..llm import Provider
    from ..permission.engine import Engine
    from ..tool import Registry

logger = logging.getLogger(__name__)

# fork 子 Agent 不提供的「系统级」工具：防止 SOP 在子 Agent 里递归加载 skill（N3）
SYSTEM_TOOL_NAMES = frozenset({"LoadSkill"})

# fork 携带的最近主对话消息条数（context=recent 档，F7）
FORK_RECENT_MESSAGE_COUNT = 5


class SkillHost(Protocol):
    """Skill 执行所需的宿主能力面（TUI App 实现）：窄接口解耦，skills 包不依赖 TUI。"""

    def skill_agent(self) -> Agent | None:
        """当前主 Agent（未选定 provider 时为 None）。"""
        ...

    def skill_provider(self) -> Provider | None:
        """当前主 provider（fork 子 Agent 沿用）。"""
        ...

    def skill_tool_registry(self) -> Registry:
        """主工具注册中心（fork 据此构建剔除系统级工具的子注册中心）。"""
        ...

    def skill_engine(self) -> Engine:
        """权限引擎（fork 子 Agent 沿用；fork 内的权限询问一律自动拒绝）。"""
        ...

    def skill_version(self) -> str:
        """应用版本号（fork 子 Agent 环境段用）。"""
        ...

    def skill_conversation(self) -> Conversation:
        """主对话（fork 按 context 档从中装填历史）。"""
        ...

    def skill_runtime(self) -> SessionRuntime:
        """主会话运行时（fork 只借用其 session 标识与上下文窗口，不复用状态，N3）。"""
        ...

    def skill_workspace(self) -> str:
        """工作目录（fork 运行时构造入参）。"""
        ...

    def skill_deliver_result(self, text: str) -> None:
        """fork 结果回流主对话（回合进行中由宿主暂存，回合收尾再补写）。"""
        ...


class SkillExecutor:
    """Skill 执行器本体：inline / fork 两条路径。

    执行器只持有 loader 与后台 fork 任务引用；agent / provider 等执行期依赖一律经
    `SkillHost` 参数传入，避免构造顺序耦合（Agent 在 provider 选定后才存在）。
    """

    def __init__(self, loader: SkillLoader) -> None:
        self._loader = loader
        self._fork_tasks: set[asyncio.Task] = set()  # 持引用防后台任务被回收

    def track_fork_task(self, task: asyncio.Task) -> None:
        """登记后台 fork 任务：持引用防被 GC，完成后自动出栈。"""
        self._fork_tasks.add(task)
        task.add_done_callback(self._fork_tasks.discard)

    def spawn_fork(self, skill: SkillDef, args: str, host: SkillHost) -> asyncio.Task:
        """后台启动 fork 子 Agent（不阻塞调用方）：结果经 host.skill_deliver_result 回流。

        斜杠命令（Kind.UI）与 LoadSkill 工具的 fork 分支共用本入口；工具侧因此
        免受 30s 工具超时约束。返回后台任务供测试等待，任务引用已登记防被 GC。
        """
        task = asyncio.create_task(self._run_fork_background(skill, args, host))
        self.track_fork_task(task)
        return task

    async def _run_fork_background(self, skill: SkillDef, args: str, host: SkillHost) -> None:
        """跑完 fork 子 Agent 并把最终文本回流宿主；异常兜底成错误文本（F7）。

        execute_fork 内部已把执行异常包成错误文本；这里再兜一层防御，回流路径
        自身出错时也给出可读提示。
        """
        try:
            final_text = await self.execute_fork(skill, args, host)
        except Exception as fork_error:  # 防御：回流路径自身出错时给出可读提示
            logger.exception("fork skill '%s' 异常", skill.name)
            final_text = (
                f"[skill '{skill.name}' fork 失败: {type(fork_error).__name__}: {fork_error}]"
            )
        host.skill_deliver_result(final_text)

    async def execute_fork(self, skill: SkillDef, args: str, host: SkillHost) -> str:
        """fork 执行（F7/T5）：独立 Conversation + 复用 SubAgent 底座，跑完回流最终文本。

        ch15 G10/F33：子 Agent 的构造、运行时隔离与跑动统一交给
        `agent.launch_fork`（run_to_completion 内含审批自动拒绝与 hook 分派）；
        本方法只负责 skill 专属的两件事——按 context 档装填子对话、剔除系统级
        工具的子注册中心。任何一步失败都返回带错误标记的文本，调用方仍把它作为
        assistant 消息写回主对话，无需再处理异常。
        """
        rendered_body = substitute_arguments(skill.prompt_body, args)
        provider = host.skill_provider()
        if provider is None:
            return f"[skill '{skill.name}' fork 失败: 尚未选定 provider]"
        try:
            fork_conversation = await self._build_fork_conversation(skill, rendered_body, host)
            fork_registry = self._build_fork_registry(host.skill_tool_registry())
            # 局部 import 避免循环依赖（spec 5 下行依赖约定）
            from ..agent.launch import ForkLaunchOpts, launch_fork

            return await launch_fork(
                ForkLaunchOpts(
                    provider=provider,
                    registry=fork_registry,
                    engine=host.skill_engine(),
                    version=host.skill_version(),
                    conv=fork_conversation,
                    parent_runtime=host.skill_runtime(),
                )
            )
        except asyncio.CancelledError:
            raise  # 宿主退出等取消：按 asyncio 约定向上传播
        except Exception as fork_error:
            logger.exception("fork skill '%s' 执行失败", skill.name)
            return f"[skill '{skill.name}' fork 失败: {type(fork_error).__name__}: {fork_error}]"

    def _build_fork_registry(self, main_registry: Registry) -> Registry:
        """构造 fork 工具注册中心：剔除系统级工具，其余照搬（N3）。"""
        from ..tool import Registry as RegistryClass

        fork_registry = RegistryClass()
        for tool_name in main_registry.names():
            if tool_name in SYSTEM_TOOL_NAMES:
                continue
            tool = main_registry.get(tool_name)
            if tool is not None:
                fork_registry.register(tool)
        return fork_registry

    async def _build_fork_conversation(
        self,
        skill: SkillDef,
        rendered_body: str,
        host: SkillHost,
    ) -> Conversation:
        """按 skill.context 装填 fork 会话（F7）：none 空 / recent 最近 5 条 / full 全量摘要。"""
        fork_conversation = Conversation()
        context_mode = (
            skill.context if skill.context in VALID_SKILL_CONTEXTS else DEFAULT_SKILL_CONTEXT
        )
        if context_mode == "none":
            fork_conversation.add_user(rendered_body)
            return fork_conversation

        if context_mode == "recent":
            context_messages = _pick_recent_main_messages(
                host.skill_conversation().messages(), FORK_RECENT_MESSAGE_COUNT
            )

            # 上下文消息 + SOP 一起做一次角色规范化（合并连续同角色、去掉开头 assistant），
            # 保证 fork 会话的 user/assistant 交替对两个协议都合法
            normalized = _normalize_conversation_roles(
                [*context_messages, Message(role=ROLE_USER, content=rendered_body)]
            )
            if not normalized:  # 防御：SOP 文本为空等极端情况
                normalized = [Message(role=ROLE_USER, content=rendered_body)]
            for normalized_message in normalized:
                if normalized_message.role == ROLE_USER:
                    fork_conversation.add_user(normalized_message.content)
                else:
                    fork_conversation.add_assistant(normalized_message.content)
        else:  # full：复用 compact 第 2 层摘要管道产出一段摘要（plan fork 分支 step 4）
            # summary_text = await self._summarize_main_conversation(host)
            # context_messages = (
            #     [Message(role=ROLE_USER, content=f"## 主对话摘要\n\n{summary_text}")]
            #     if summary_text
            #     else []
            # )

            # full: 深拷贝全部历史message
            context_messages = copy.deepcopy(host.skill_conversation().messages())

            if context_messages and context_messages[-1].role == ROLE_USER:
                context_messages[-1].content = f"{context_messages[-1].content}\n\n{rendered_body}"
            else:
                context_messages.append(Message(role=ROLE_USER, content=rendered_body))
            fork_conversation = Conversation.from_messages(context_messages)
        return fork_conversation

    async def _summarize_main_conversation(self, host: SkillHost) -> str:
        """用 compact 的摘要管道对主对话做一次全量摘要（F7 full 档）。

        摘要请求失败（撞墙 / 流出错）降级为空串，fork 只带 SOP 继续，不阻断执行。
        """
        main_conversation = host.skill_conversation()
        main_messages = main_conversation.messages()
        if not main_messages:
            return ""
        provider = host.skill_provider()
        assert provider is not None  # execute_fork 入口已保证
        main_runtime = host.skill_runtime()
        manage_input = ManageInput(
            conv=main_conversation,
            provider=provider,
            context_window=main_runtime.context_window,
            tool_defs=[],  # 摘要请求不携带任何工具
            replacement=ContentReplacementState(),
            recovery=RecoveryState(),
            auto_tracking=CompactCircuitBreaker(),
            session=main_runtime.session,
            usage_anchor=0,
            anchor_msg_len=0,
            estimated_token=0,
            trigger=TriggerKind.MANUAL,
        )
        try:
            return await summarize_once(manage_input, main_messages)
        except Exception as summary_error:
            logger.warning("fork full 摘要失败，降级为无上下文: %s", summary_error)
            return ""


def _pick_recent_main_messages(main_messages: list[Message], limit: int) -> list[Message]:
    """取主对话最近的 user/assistant 纯文本消息（跳过带工具调用的回合，F7 recent 档）。

    带工具调用的 assistant 回合需要与 tool 结果成对出现，单独拷贝会构成非法历史。
    """
    picked: list[Message] = []
    for message in reversed(main_messages):
        if len(picked) >= limit:
            break
        if message.role == ROLE_USER:
            picked.append(message)
        elif message.role == ROLE_ASSISTANT and not message.tool_calls and message.content.strip():
            picked.append(message)
    picked.reverse()
    return picked


def _normalize_conversation_roles(messages: list[Message]) -> list[Message]:
    """合并连续同角色文本消息并去掉开头非 user 消息，产出协议合法的 user/assistant 序列。丢掉tool_call/tool_result对应的消息"""
    merged: list[Message] = []
    for message in messages:
        if message.role not in (ROLE_USER, ROLE_ASSISTANT) or not message.content.strip():
            continue  # 防御：跳过 tool 消息与空文本消息
        if merged and merged[-1].role == message.role:
            merged[-1] = Message(
                role=message.role,
                content=f"{merged[-1].content}\n\n{message.content}",
            )
        else:
            merged.append(message)
    while merged and merged[0].role != ROLE_USER:
        merged.pop(0)
    return merged
