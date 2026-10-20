"""CLI 对 MCP Manager 的装配与关闭测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from potatocode import cli
from potatocode.agent import new_runtime
from potatocode.compact import sessions_root
from potatocode.config import ProviderConfig
from potatocode.permission import Mode, new_engine
from potatocode.tool import new_default_registry
from potatocode.tui import PotatoCodeApp


class FakeTool:
    def name(self):
        return "mcp__demo__echo"

    def description(self):
        return "echo"

    def parameters(self):
        return {"type": "object"}

    def read_only(self):
        return True

    async def execute(self, args):
        raise AssertionError("CLI 装配测试不应执行工具")


class FakeManager:
    def __init__(self, tools=()) -> None:
        self._tools = list(tools)
        self.closed = False

    def tools(self):
        return list(self._tools)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_amain_registers_mcp_tools_and_closes_manager(monkeypatch):
    observed = {}
    manager = FakeManager([FakeTool()])

    async def fake_new_manager(config, version):
        observed["mcp_config"] = config
        observed["version"] = version
        return manager

    class FakeApp:
        def __init__(
            self,
            providers,
            version,
            registry,
            engine,
            manager=None,
            runtime=None,
            writer=None,
            mem_mgr=None,
            instruction_text="",
            memory_text="",
            sessions_dir="",
            workspace="",
            skill_loader=None,
            skill_executor=None,
            load_skill_tool=None,
            skill_catalog_text="",
            hook_engine=None,  # ch14：cli 装配注入 hook 引擎
        ):
            observed["providers"] = providers
            observed["definitions"] = registry.definitions()
            observed["runtime"] = runtime
            observed["writer"] = writer
            observed["mem_mgr"] = mem_mgr
            observed["instruction_text"] = instruction_text
            observed["sessions_dir"] = sessions_dir

        def rebind_skill_commands(self):
            observed["rebound"] = True

        mode = Mode.DEFAULT  # 退出兜底 SessionEnd 读当前模式（ch14 T22）

        async def run_async(self):
            observed["ran"] = True

    monkeypatch.setattr(cli.config, "load", lambda path: SimpleNamespace(providers=["provider"]))
    monkeypatch.setattr(cli.mcp, "load_config", lambda root: "loaded-mcp-config")
    monkeypatch.setattr(cli.mcp, "new_manager", fake_new_manager)
    monkeypatch.setattr(cli.permission, "new_engine", lambda root: ("engine", None))
    monkeypatch.setattr(cli, "PotatoCodeApp", FakeApp)

    exit_code = await cli._amain()

    assert exit_code == 0
    assert observed["mcp_config"] == "loaded-mcp-config"
    assert observed["version"] == cli.__version__
    # 6 个核心工具 + ch13 新增的 LoadSkill / InstallSkill + 1 个 MCP 远程工具
    assert len(observed["definitions"]) == 9
    assert observed["definitions"][-1].name == "mcp__demo__echo"
    assert observed["ran"] is True
    assert manager.closed is True
    # ch11：启动流程把存档写入器、记忆管理器与会话目录一起交给 TUI
    assert observed["writer"] is not None
    assert observed["mem_mgr"] is not None
    assert observed["sessions_dir"] == sessions_root(str(Path.cwd()))


@pytest.mark.asyncio
async def test_amain_closes_manager_when_tui_fails(monkeypatch):
    manager = FakeManager()

    class BrokenApp:
        def __init__(self, *args, **kwargs):
            pass

        def rebind_skill_commands(self):
            pass

        async def run_async(self):
            raise RuntimeError("TUI failed")

    monkeypatch.setattr(cli.config, "load", lambda path: SimpleNamespace(providers=[]))
    monkeypatch.setattr(cli.mcp, "load_config", lambda root: "config")

    async def fake_new_manager(config, version):
        return manager

    monkeypatch.setattr(cli.mcp, "new_manager", fake_new_manager)
    monkeypatch.setattr(cli.permission, "new_engine", lambda root: ("engine", None))
    monkeypatch.setattr(cli, "PotatoCodeApp", BrokenApp)

    with pytest.raises(RuntimeError, match="TUI failed"):
        await cli._amain()
    assert manager.closed is True


@pytest.mark.asyncio
async def test_textual_headless_starts_with_mcp_tool(tmp_path):
    registry = new_default_registry()
    registry.register(FakeTool())
    engine, error = new_engine(str(tmp_path))
    assert error is None
    provider = ProviderConfig(
        name="headless",
        protocol="anthropic",
        api_key="test-key",
        model="test-model",
    )
    app = PotatoCodeApp(
        [provider], "test", registry, engine, runtime=new_runtime(workspace=str(tmp_path))
    )

    async with app.run_test():
        names = [definition.name for definition in app._tool_registry.definitions()]
        assert names[-1] == "mcp__demo__echo"
        assert len(names) == 7
