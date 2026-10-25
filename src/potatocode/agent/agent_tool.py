"""Agent 工具：主 Agent 分发子任务的统一入口（ch15 F1-F3/F22-F31）。

主 Agent 经本工具按 `subagent_type` 选择预定义角色（定义式，空白对话起步）
或留空走 Fork 路径（克隆父历史 + Fork Boilerplate，借 prompt cache 降首次成本）。
工具列表对模型始终稳定——角色增减只影响 description 文本，不影响注册（G1/N1）。

嵌套阻断（F24）：入口检测协程深度标记（`SUBAGENT_DEPTH`，QuerySource 等价物），
子 Agent 再调 Agent 工具一律结构化拒绝；`is_fork_context` 的 Boilerplate 标记
扫描作为 conv 可达场景的兜底识别。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ..conversation import Conversation
from ..permission import Outcome
from ..subagent import Definition
from ..task.manager import PartialState
from ..tool import Result
from . import AUTO_BACKGROUND_SECONDS, Agent, ApprovalRequest, Event, Phase
from .fork import build_forked_messages
from .runtime import new_sub_runtime
from .subagent_context import SUBAGENT_DEPTH

if TYPE_CHECKING:
    from .permission_upgrade import ApprovalUpgrader

logger = logging.getLogger(__name__)

# 模型看到的工具名（与 ALL_AGENT_DISALLOWED_TOOLS 中的禁用名对应）
TOOL_NAME_AGENT = "Agent"

# 前台子 Agent 超时转后台的等待上限在 agent 包定义（AUTO_BACKGROUND_SECONDS，
# 与 Registry 对 Agent 工具的加长超时同源）；此处只定义结果里的状态字面量（F17/F2）
STATUS_ASYNC_LAUNCHED = "async_launched"
STATUS_TIMED_OUT_TO_BACKGROUND = "timed_out_to_background"


class SubAgentCatalog(Protocol):
    """Agent 工具所需的目录能力面（与 subagent.Catalog 结构化兼容）。"""

    def resolve(self, name: str) -> Definition | None: ...

    def fork_definition(self) -> Definition: ...

    def list(self) -> list[Definition]: ...


class TaskManagerLike(Protocol):
    """Agent 工具所需的后台管理能力面（与 task.Manager 结构化兼容）。"""

    async def launch(self, agent: Agent, conv: Conversation, name: str, task_text: str) -> str: ...

    async def adopt_running(  # noqa: D401
        self,
        agent: Agent,
        conv: Conversation,
        name: str,
        events: asyncio.Queue,
        handle: asyncio.Task,
        partial: PartialState,
    ) -> str: ...

    async def upgrade_approval(self, req: ApprovalRequest) -> tuple[Outcome, bool]: ...


@dataclass
class AgentArgs:
    """Agent 工具的解析后参数（F1）。"""

    prompt: str = ""
    description: str = ""
    subagent_type: str = ""
    model: str = ""
    run_in_background: bool = False
    name: str = ""


def parse_agent_args(args: str) -> AgentArgs | None:
    """把 raw JSON 参数解析为 AgentArgs；非法 JSON / 非对象返回 None。"""
    try:
        data = json.loads(args) if args and args.strip() else {}
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return AgentArgs(
        prompt=str(data.get("prompt") or ""),
        description=str(data.get("description") or ""),
        subagent_type=str(data.get("subagent_type") or ""),
        model=str(data.get("model") or ""),
        run_in_background=bool(data.get("run_in_background") or False),
        name=str(data.get("name") or ""),
    )


class AgentTool:
    """注册到 Registry 的统一 Agent 工具（F1/F2）。

    `parent`（主 Agent）与主对话经 getter 注入——主 Agent 在 provider 选定后才
    构造，getter 保证换 provider 重建后引用依然有效（同 LoadSkill 的注入模式）。
    """

    def __init__(
        self, catalog: SubAgentCatalog, task_mgr: TaskManagerLike, bg_enabled: bool
    ) -> None:
        self._catalog = catalog
        self._task_mgr = task_mgr
        self._bg_enabled = bg_enabled  # N6：enable_subagent_background 配置开关
        self._parent_getter: Callable[[], Agent | None] | None = None
        self._conv_getter: Callable[[], Conversation] | None = None

    # ────────────────────────── 注入 ──────────────────────────

    def set_parent_getter(self, getter: Callable[[], Agent | None]) -> None:
        """注入主 Agent getter（cli 在 App 构造后调用；惰性兼容 provider 选择）。"""
        self._parent_getter = getter

    def set_parent(self, agent: Agent | None) -> None:
        """注入静态主 Agent 实例（测试便捷入口；包成 getter 统一路径）。"""
        self._parent_getter = lambda: agent  # noqa: E731 - 简单闭包即可

    def set_parent_conv_getter(self, getter: Callable[[], Conversation]) -> None:
        """注入主对话 getter（Fork 路径克隆父历史用，F22）。"""
        self._conv_getter = getter

    # ────────────────────────── Tool 协议 ──────────────────────────

    def name(self) -> str:
        return TOOL_NAME_AGENT

    def read_only(self) -> bool:
        return False  # 子 Agent 可能做任何事：按 EXEC 类走完整权限链

    def description(self) -> str:
        """动态描述：列出当前可用的 subagent_type（角色增减即时反映，F1）。"""
        lines = [
            "启动一个子 Agent 独立完成任务，避免污染主对话上下文。",
            "subagent_type 留空时走 Fork 路径：克隆当前对话历史并在后台继续（强制后台）。",
            "可用 subagent_type:",
        ]
        for definition in self._catalog.list():
            lines.append(f"- {definition.name}: {definition.description}")
        return "\n".join(lines)

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "交给子 Agent 的任务指令"},
                "description": {
                    "type": "string",
                    "description": "一句话描述任务，供 UI 展示",
                },
                "subagent_type": {
                    "type": "string",
                    "description": "预定义角色名；留空走 Fork 路径",
                },
                "model": {
                    "type": "string",
                    "enum": ["haiku", "sonnet", "opus", "inherit"],
                    "description": "模型覆盖（预留：本期沿用主 provider）",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "true 时后台启动，立即返回 task_id",
                },
                "name": {
                    "type": "string",
                    "description": "给本次任务命名，供 SendMessage 续派寻址；同名后启动覆盖前者",
                },
            },
            "required": ["prompt", "description"],
        }

    # ────────────────────────── 执行 ──────────────────────────

    async def execute(self, args: str) -> Result:
        """主流程（F2）：解析 → 防嵌套 → resolve → 过滤 → 构造 → 前台/后台（F30）。"""
        parsed = parse_agent_args(args)
        if parsed is None:
            return Result(content="参数不是合法 JSON 对象", is_error=True)
        if not parsed.prompt.strip():
            return Result(content="缺少参数 prompt（任务指令）", is_error=True)
        if not parsed.description.strip():
            return Result(content="缺少参数 description（任务描述）", is_error=True)

        parent = self._parent_getter() if self._parent_getter is not None else None
        if parent is None:
            return Result(content="Agent 工具尚未就绪（主 Agent 未构造）", is_error=True)

        # 防嵌套（F24-②/F3）：调用方已在某个子 Agent 内 → 一律拒绝
        if SUBAGENT_DEPTH.get() > 0:
            return Result(content="子 Agent 不能再启动 Agent（嵌套被禁止）", is_error=True)

        # resolve 定义（F2）：指定角色 / Fork 临时定义
        if parsed.subagent_type:
            definition = self._catalog.resolve(parsed.subagent_type)
            if definition is None:
                available = ", ".join(d.name for d in self._catalog.list()) or "（无可用角色）"
                return Result(
                    content=f"未知 subagent_type: {parsed.subagent_type}（可用: {available}）",
                    is_error=True,
                )
        else:
            definition = self._catalog.fork_definition()

        # 后台判定（F17/F18）：显式 run_in_background / 角色强制 / Fork 强制
        background = definition.background or parsed.run_in_background or definition.is_fork()
        if background and not self._bg_enabled:
            return Result(
                content=(
                    "后台模式已被配置禁用（enable_subagent_background=false），无法启动后台子 Agent"
                ),
                is_error=True,
            )

        sub_agent, sub_conv = self._build_sub_agent(parent, definition, parsed)
        if sub_agent is None:
            return Result(content="Fork 路径需要主对话，但当前不可达", is_error=True)
        # Fork 的任务文本已随 Boilerplate 装填进子对话首条 user 消息（F22），置空避免重复
        task_text = "" if definition.is_fork() else parsed.prompt

        # 深度标记：前台 await 与后台 create_task 都会继承，子 Agent 内再调 Agent 被拦（F24）
        token = SUBAGENT_DEPTH.set(SUBAGENT_DEPTH.get() + 1)
        try:
            if background:
                task_id = await self._task_mgr.launch(sub_agent, sub_conv, parsed.name, task_text)
                return Result(
                    content=json.dumps(
                        {"task_id": task_id, "status": STATUS_ASYNC_LAUNCHED},
                        ensure_ascii=False,
                    )
                )
            return await self._run_foreground(sub_agent, sub_conv, task_text, parsed.name)
        finally:
            SUBAGENT_DEPTH.reset(token)

    # ────────────────────────── 内部 ──────────────────────────

    def _build_sub_agent(
        self, parent: Agent, definition: Definition, parsed: AgentArgs
    ) -> tuple[Agent | None, Conversation]:
        """构造子 Agent 与子对话（F30 过滤 + F22 Fork 装填）；主对话不可达时返回 (None, _)。"""
        # 工具过滤多层防线（F30）：五层顺序过滤后落成独立子注册中心
        from ..tool.filter import FilterParams, apply_agent_tool_filter, build_sub_registry

        allowed = apply_agent_tool_filter(
            FilterParams(
                all=parent.registry.names(),
                source=int(definition.source),
                background=(
                    definition.background or parsed.run_in_background or definition.is_fork()
                ),
                allowed=definition.tools,
                disallowed=definition.disallowed_tools,
            )
        )
        if definition.is_fork() and "Agent" in parent.registry.names():
            # F24-①/F22：Fork 子 Agent 的工具列表保留 Agent 工具（保 prompt cache 一致性，
            # 供模型尝试后收到结构化拒绝）——绕过后台白名单对元工具的剔除；
            # 调用时由深度标记拦截（AC5）
            allowed = [*allowed, "Agent"]
        sub_registry = build_sub_registry(parent.registry, allowed)

        sub_conv = Conversation()
        if definition.is_fork():
            parent_conv = self._conv_getter() if self._conv_getter is not None else None
            if parent_conv is None:
                return None, sub_conv
            sub_conv = Conversation.from_messages(
                build_forked_messages(parent_conv.messages(), parsed.prompt)
            )

        sub_agent = Agent(
            parent.provider,
            sub_registry,
            parent.engine,
            parent.version,
            runtime=new_sub_runtime(parent.runtime),
            hook_engine=parent.hook_engine,
            system_prompt=definition.system_prompt or None,  # 空 = 继承主系统提示（N2）
            max_turns=definition.max_turns,
            permission_mode=definition.permission_mode,
            dont_ask=definition.dont_ask,
            approval_upgrader=self._make_approval_upgrader(
                parsed.name or ("fork" if definition.is_fork() else definition.name)
            ),
        )
        return sub_agent, sub_conv

    def _make_approval_upgrader(self, label: str) -> ApprovalUpgrader:
        """包装升级回调：给父 TUI 弹窗的 reason 加「来自 SubAgent」标识（F12-③/AC8）。

        不改写原请求对象（升级失败回退默认路径时，拒绝结果仍用原始 reason）；
        转发的副本与原请求共享同一个 respond Future，裁决经其回传。
        """
        task_mgr = self._task_mgr

        async def upgrade(req: ApprovalRequest) -> tuple[Outcome, bool]:
            forwarded = ApprovalRequest(
                name=req.name,
                args=req.args,
                reason=f"[来自 SubAgent {label}] {req.reason}",
                respond=req.respond,
            )
            return await task_mgr.upgrade_approval(forwarded)

        return upgrade

    async def _run_foreground(
        self, sub_agent: Agent, sub_conv: Conversation, task_text: str, name: str
    ) -> Result:
        """前台同步跑子 Agent：完成返回 final_text；超时转后台（F17-②，plan 模块 D）。"""
        events: asyncio.Queue = asyncio.Queue(maxsize=64)
        partial = PartialState()
        aggregator = asyncio.create_task(self._aggregate_partial(events, partial))
        run_handle = asyncio.create_task(sub_agent.run_to_completion(sub_conv, task_text, events))
        # N6：后台关闭时无限等（强制前台同步），超时切后台随之失效
        foreground_timeout = AUTO_BACKGROUND_SECONDS if self._bg_enabled else None
        try:
            done, _pending = await asyncio.wait({run_handle}, timeout=foreground_timeout)
        except asyncio.CancelledError:
            # 工具层超时 / 用户取消杀掉了本工具：终止子 Agent 防孤儿跑动（N3）
            run_handle.cancel()
            raise
        finally:
            aggregator.cancel()

        if run_handle in done:
            try:
                final_text = run_handle.result()
            except asyncio.CancelledError:
                raise
            except Exception as sub_error:  # 子 Agent 异常包成结构化错误回灌（N3）
                logger.warning("前台子 Agent 异常: %s", sub_error)
                return Result(content=f"子 Agent 执行出错: {sub_error}", is_error=True)
            return Result(content=final_text)

        # 超时：前台聚合协程已取消，Manager 接管事件流继续后台跑（F17-②/AC10）
        task_id = await self._task_mgr.adopt_running(
            sub_agent, sub_conv, name, events, run_handle, partial
        )
        return Result(
            content=json.dumps(
                {"task_id": task_id, "status": STATUS_TIMED_OUT_TO_BACKGROUND},
                ensure_ascii=False,
            )
        )

    async def _aggregate_partial(self, events: asyncio.Queue, partial: PartialState) -> None:
        """前台阶段的事件聚合：工具次数 / 最近活动 / token 用量（超时移交并入快照）。"""
        try:
            while True:
                event: Event = await events.get()
                if event.tool is not None and event.tool.phase is Phase.START:
                    partial.tool_count += 1
                    partial.last_activity = event.tool.name
                if event.usage is not None:
                    partial.usage.input += event.usage.input
                    partial.usage.output += event.usage.output
                    partial.usage.cache_write += event.usage.cache_write
                    partial.usage.cache_read += event.usage.cache_read
        except asyncio.CancelledError:
            pass  # 前台结束或转后台：聚合协程被取消，残留事件由 Manager 接管消费


__all__ = [
    "AUTO_BACKGROUND_SECONDS",
    "STATUS_ASYNC_LAUNCHED",
    "STATUS_TIMED_OUT_TO_BACKGROUND",
    "TOOL_NAME_AGENT",
    "AgentArgs",
    "AgentTool",
    "SubAgentCatalog",
    "TaskManagerLike",
    "parse_agent_args",
]
