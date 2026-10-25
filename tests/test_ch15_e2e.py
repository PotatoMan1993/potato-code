"""ch15 端到端冒烟（checklist 场景 1/2 的 Windows 无头版）：真实 PotatoCodeApp 驱动。

Windows 下无法用 tmux 驱动交互 TUI（沿用 test_skills_e2e 的做法）：Textual
`run_test()` 无头跑真实 App，fake provider 脚本化响应。覆盖两条核心链路——
定义式子 Agent 前台同步回流（场景 1）与 Fork 子 Agent 后台启动 +
<task-notification> 注入 reminder（场景 2）。不发起真实网络请求。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from potatocode.agent.agent_tool import AgentTool
from potatocode.config import ProviderConfig
from potatocode.llm import StreamEvent, ToolCall
from potatocode.subagent import load_catalog
from potatocode.task import Manager, SendMessageTool, TaskGetTool, TaskListTool, TaskStopTool
from potatocode.tool import new_default_registry
from potatocode.tui.app import PotatoCodeApp
from potatocode.tui.view import SessionState

from test_skills_e2e import ScriptedProvider, wait_until_idle

ECHO_AGENT_MD = """---
name: echo-agent
description: 端到端冒烟用回声角色
maxTurns: 5
---

你是回声角色。
"""


def _prepare_workspace(workspace: Path) -> None:
    """预置角色定义与 bypass 权限模式（省去主循环审批交互，聚焦子 Agent 链路）。"""
    agents_dir = workspace / ".potatocode" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "echo-agent.md").write_text(ECHO_AGENT_MD, encoding="utf-8")
    local_settings = workspace / ".potatocode" / "settings.local.yaml"
    local_settings.write_text("default_mode: bypassPermissions\n", encoding="utf-8")


def _agent_call(input_payload: dict) -> StreamEvent:
    return StreamEvent(
        tool_calls=[ToolCall(id="x", name="Agent", input=json.dumps(input_payload))]
    )


def make_ch15_app(workspace: Path, provider: ScriptedProvider) -> PotatoCodeApp:
    """按 cli.py 装配顺序构造无头测试 App（注入 ch15 的 task_mgr 与角色目录）。"""
    import potatocode.tui.app as app_module
    from potatocode.permission import new_engine

    app_module.new_provider = lambda cfg: provider  # 替换 provider 构造（不触网）
    _prepare_workspace(workspace)

    registry = new_default_registry()
    catalog = load_catalog(str(workspace))
    task_mgr = Manager()
    for tool in (TaskListTool(task_mgr), TaskGetTool(task_mgr), TaskStopTool(task_mgr), SendMessageTool(task_mgr)):
        registry.register(tool)
    agent_tool = AgentTool(catalog, task_mgr, bg_enabled=True)
    registry.register(agent_tool)
    engine, _ = new_engine(str(workspace))
    app = PotatoCodeApp(
        [ProviderConfig(name="fake", protocol="anthropic", api_key="dummy", model="fake-model")],
        "test-0.0",
        registry,
        engine,
        runtime=None,
        workspace=str(workspace),
        task_mgr=task_mgr,
        subagent_catalog=catalog,
    )
    agent_tool.set_parent_getter(app.ensure_agent)
    agent_tool.set_parent_conv_getter(lambda: app.conv)
    return app


def _tool_results_text(conv) -> str:
    return "\n".join(
        r.content for m in conv.messages() if m.role == "tool" for r in m.tool_results
    )


async def test_e2e_defined_subagent_foreground(tmp_path: Path) -> None:
    """场景 1：主 Agent 调 Agent 工具（定义式前台），tool_result 回流子 Agent 文本。"""
    provider = ScriptedProvider(
        [
            [_agent_call({"prompt": "找一个数", "description": "数数", "subagent_type": "echo-agent"}), StreamEvent(done=True)],
            [StreamEvent(text="子 Agent 报告：42 个"), StreamEvent(done=True)],
            [StreamEvent(text="主对话收尾"), StreamEvent(done=True)],
        ]
    )
    app = make_ch15_app(tmp_path, provider)
    async with app.run_test():
        app.conv.add_user("用子 Agent 找数")
        app._start_turn()
        await wait_until_idle(app)
        assert "子 Agent 报告：42 个" in _tool_results_text(app.conv)
        # 子 Agent 请求（第 2 次）工具列表不含 Agent（F26 全局禁止列表）
        assert "Agent" not in [d.name for d in provider.requests[1].tools or []]


async def test_e2e_fork_background_with_task_notification(tmp_path: Path) -> None:
    """场景 2：Fork 路径强制后台，跑完经 <task-notification> 注入 reminder。"""
    provider = ScriptedProvider(
        [
            # 第 1 轮：主 Agent 铺垫上下文后调用 Fork（不指定 subagent_type）
            [StreamEvent(text="铺垫"), StreamEvent(done=True)],
            [_agent_call({"prompt": "统计行数", "description": "fork 任务"}), StreamEvent(done=True)],
            # 脚本耗尽后恒返最后一个：Fork 子 Agent 与主对话收尾都用它
            [StreamEvent(text="FORK 报告"), StreamEvent(done=True)],
        ]
    )
    app = make_ch15_app(tmp_path, provider)
    async with app.run_test():
        app.conv.add_user("先看点文件")
        app._start_turn()
        await wait_until_idle(app)

        app.conv.add_user("Fork 出去统计")
        app._start_turn()
        await wait_until_idle(app)
        assert "async_launched" in _tool_results_text(app.conv)  # 立即返回（AC9）

        # 等后台任务完成；通知经消费协程进 reminder 队列（F19/AC12），随后被主对话
        # 下一轮请求的 _build_reminder 取出（取出即清空，ch14 机制）——因此断言
        # 「已注入 reminder 通道」= 出现在任一请求的 reminder 里或仍 pending
        notification_seen = False
        for _ in range(200):
            in_pending = any("<task-notification>" in r for r in app.runtime.pending_reminders)
            in_request = any(
                "<task-notification>" in (req.reminder or "") for req in provider.requests
            )
            notification_seen = in_pending or in_request
            if notification_seen:
                break
            await asyncio.sleep(0.05)
        assert notification_seen
        carried = [req for req in provider.requests if "<task-notification>" in (req.reminder or "")]
        assert any("FORK 报告" in req.reminder for req in carried)  # 含 Result 文本
        # 通知只进 reminder（对模型可见），不写对话历史（N7）
        assert all("<task-notification>" not in m.content for m in app.conv.messages())
