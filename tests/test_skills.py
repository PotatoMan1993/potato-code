"""ch13 Skill 系统单测：parser / loader / executor / LoadSkill / Agent 集成 / 命令注册。

Agent 以 MagicMock 替代真实 provider；fork 路径用脚本化 fake provider 驱动，
不发起任何真实网络请求。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from potatocode.agent import Agent, new_runtime
from potatocode.command import NopUI
from potatocode.command import Registry as CommandRegistry
from potatocode.command.builtin_skill import register_skill_manage_command
from potatocode.command.command import Command, Kind
from potatocode.command.skill_register import (
    SKILL_COMMAND_DESCRIPTION_PREFIX,
    build_skill_catalog_text,
    register_skill_commands,
)
from potatocode.conversation import Conversation
from potatocode.llm import Request, StreamEvent
from potatocode.permission import new_engine
from potatocode.prompt import (
    SkillCatalogItem,
    render_skills_catalog,
)
from potatocode.skills.executor import SkillExecutor
from potatocode.skills.loader import SkillLoader
from potatocode.skills.parser import (
    SkillDef,
    SkillParseError,
    parse_frontmatter,
    parse_skill_file,
    substitute_arguments,
)
from potatocode.tool.load_skill import LoadSkill

# ────────────────────────── 测试脚手架 ──────────────────────────


def write_skill_file(
    path: Path,
    name: str = "alpha",
    description: str = "A test skill",
    mode: str | None = None,
    context: str | None = None,
    body: str = "SOP BODY",
) -> Path:
    """写一个最小可解析的 SKILL.md（单文件布局），返回文件路径。"""
    lines = ["---", f"name: {name}", f'description: "{description}"']
    if mode is not None:
        lines.append(f"mode: {mode}")
    if context is not None:
        lines.append(f"context: {context}")
    lines += ["---", "", body, ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def make_loader(tmp_path: Path) -> SkillLoader:
    """构造项目/用户两级目录都在临时目录里的 SkillLoader（不污染真实用户目录）。"""
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return SkillLoader(str(workspace), user_skills_dir=str(tmp_path / "user-skills"))


class RecordingUI(NopUI):
    """记录 println / error 输出的测试桩（/skill 命令断言用）。"""

    def __init__(self) -> None:
        super().__init__()
        self.printed: list[str] = []
        self.errors: list[str] = []

    def println(self, msg: str) -> None:
        self.printed.append(msg)

    def error(self, msg: str) -> None:
        self.errors.append(msg)


class ScriptedProvider:
    """脚本化 provider：按请求次数逐轮返回事件（fork 摘要 + fork 主跑共用）。"""

    def __init__(self, scripts: list[list[StreamEvent]]) -> None:
        self._scripts = scripts
        self.calls = 0
        self.requests: list[Request] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    async def stream(self, req: Request) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        self.requests.append(req)
        script = self._scripts[self.calls - 1] if self.calls <= len(self._scripts) else []
        for event in script:
            yield event


class FakeHost:
    """SkillHost 测试桩：fork 执行所需的宿主能力面，全部注入可控对象。

    用 `_UNSET` 哨兵区分「未注入（给默认桩）」与「显式注入 None（模拟未就绪）」。
    """

    _UNSET = object()

    def __init__(
        self,
        tmp_path: Path,
        agent: MagicMock | None = _UNSET,
        provider: ScriptedProvider | None = _UNSET,
        conversation: Conversation | None = None,
    ) -> None:
        from potatocode.tool import Registry as ToolRegistry

        engine, _ = new_engine(str(tmp_path))
        self._agent = MagicMock() if agent is self._UNSET else agent
        self._provider = ScriptedProvider([]) if provider is self._UNSET else provider
        self._tool_registry = ToolRegistry()
        self._engine = engine
        self._conversation = conversation if conversation is not None else Conversation()
        self._runtime = new_runtime(workspace=str(tmp_path))
        self.delivered: list[str] = []  # skill_deliver_result 回流的 fork 结果

    def skill_agent(self):
        return self._agent

    def skill_provider(self):
        return self._provider

    def skill_tool_registry(self):
        return self._tool_registry

    def skill_engine(self):
        return self._engine

    def skill_version(self) -> str:
        return "test-0.0"

    def skill_conversation(self) -> Conversation:
        return self._conversation

    def skill_runtime(self):
        return self._runtime

    def skill_workspace(self) -> str:
        return "."

    def skill_deliver_result(self, text: str) -> None:
        self.delivered.append(text)


# ────────────────────────── parser（T1/T11） ──────────────────────────


class TestSkillParser:
    def test_parse_valid_frontmatter(self, tmp_path: Path) -> None:
        path = write_skill_file(tmp_path / "alpha.md", name="alpha", mode="fork", context="recent")
        skill = parse_skill_file(path)
        assert skill.name == "alpha"
        assert skill.description == "A test skill"
        assert skill.prompt_body == "SOP BODY"
        assert skill.mode == "fork"
        assert skill.context == "recent"
        assert skill.is_directory is False
        assert Path(skill.source_path) == path.resolve()

    def test_parse_frontmatter_returns_meta_and_body(self) -> None:
        meta, body = parse_frontmatter("---\nname: x\ndescription: d\n---\n\nBODY\n")
        assert meta == {"name": "x", "description": "d"}
        assert body == "BODY"

    def test_missing_opening_delimiter(self) -> None:
        with pytest.raises(SkillParseError, match="frontmatter 缺失"):
            parse_frontmatter("name: x\n---\nbody")

    def test_unclosed_frontmatter(self) -> None:
        with pytest.raises(SkillParseError, match="未闭合"):
            parse_frontmatter("---\nname: x\n")

    def test_invalid_yaml(self) -> None:
        with pytest.raises(SkillParseError, match="YAML"):
            parse_frontmatter("---\nname: [unclosed\n---\nbody")

    def test_non_dict_frontmatter(self) -> None:
        with pytest.raises(SkillParseError, match="键值映射"):
            parse_frontmatter("---\n- just a list\n---\nbody")

    def test_missing_name(self, tmp_path: Path) -> None:
        bad = tmp_path / "noname.md"
        bad.write_text("---\ndescription: d\n---\nbody", encoding="utf-8")
        with pytest.raises(SkillParseError, match="name"):
            parse_skill_file(bad)

    def test_missing_description(self, tmp_path: Path) -> None:
        bad = tmp_path / "nodesc.md"
        bad.write_text("---\nname: alpha\n---\nbody", encoding="utf-8")
        with pytest.raises(SkillParseError, match="description"):
            parse_skill_file(bad)

    def test_invalid_name_format(self) -> None:
        with pytest.raises(SkillParseError, match="name 非法"):
            parse_skill_file(_skill_file_with(tmp_path_none(), name="Bad_Name"))

    def test_invalid_mode(self) -> None:
        with pytest.raises(SkillParseError, match="mode"):
            parse_skill_file(_skill_file_with(tmp_path_none(), mode="weird"))

    def test_invalid_context(self) -> None:
        with pytest.raises(SkillParseError, match="context"):
            parse_skill_file(_skill_file_with(tmp_path_none(), context="everything"))

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        with pytest.raises(SkillParseError, match="读取 SKILL.md 失败"):
            parse_skill_file(tmp_path / "nope.md")

    def test_fork_mode_with_context(self, tmp_path: Path) -> None:
        path = write_skill_file(tmp_path / "f.md", name="f", mode="fork", context="full")
        skill = parse_skill_file(path)
        assert skill.is_fork() is True
        assert skill.context == "full"


def tmp_path_none() -> Path:
    """invalid name/mode/context 用例共用的临时文件位置（每个用例独立文件名）。"""
    import tempfile

    return Path(tempfile.mkdtemp(prefix="skill-meta-test-")) / "case.md"


def _skill_file_with(
    path: Path,
    name: str = "alpha",
    mode: str | None = None,
    context: str | None = None,
) -> Path:
    return write_skill_file(path, name=name, mode=mode, context=context)


class TestSubstituteArguments:
    def test_replaces_placeholder(self) -> None:
        assert substitute_arguments("run $ARGUMENTS now", "--fast") == "run --fast now"

    def test_without_args_keeps_empty(self) -> None:
        assert substitute_arguments("run $ARGUMENTS now", "") == "run  now"

    def test_no_placeholder_returns_as_is(self) -> None:
        body = "no placeholder here"
        assert substitute_arguments(body, "args") is body

    def test_multiple_placeholders_all_replaced(self) -> None:
        assert substitute_arguments("$ARGUMENTS and $ARGUMENTS", "x") == "x and x"


# ────────────────────────── loader（T2/T11） ──────────────────────────


class TestSkillLoader:
    def test_project_load(self, tmp_path: Path) -> None:
        loader = make_loader(tmp_path)
        write_skill_file(tmp_path / "ws" / ".potatocode" / "skills" / "alpha.md")
        loader.load_all()
        skill = loader.get("alpha")
        assert skill is not None
        assert skill.prompt_body == "SOP BODY"
        assert loader.get_source_label("alpha") == "project"

    def test_user_load(self, tmp_path: Path) -> None:
        loader = make_loader(tmp_path)
        write_skill_file(tmp_path / "user-skills" / "beta.md", name="beta")
        loader.load_all()
        assert loader.get("beta") is not None
        assert loader.get_source_label("beta") == "user"

    def test_project_overrides_user(self, tmp_path: Path) -> None:
        loader = make_loader(tmp_path)
        write_skill_file(
            tmp_path / "user-skills" / "dup.md", name="dup", description="user version"
        )
        write_skill_file(
            tmp_path / "ws" / ".potatocode" / "skills" / "dup.md",
            name="dup",
            description="project version",
        )
        loader.load_all()
        skill = loader.get("dup")
        assert skill is not None
        assert skill.description == "project version"
        assert loader.get_source_label("dup") == "project"

    def test_get_catalog(self, tmp_path: Path) -> None:
        loader = make_loader(tmp_path)
        write_skill_file(tmp_path / "ws" / ".potatocode" / "skills" / "alpha.md", name="alpha")
        loader.load_all()
        assert ("alpha", "A test skill") in loader.get_catalog()

    def test_get_unknown_returns_none(self, tmp_path: Path) -> None:
        loader = make_loader(tmp_path)
        loader.load_all()
        assert loader.get("ghost") is None

    def test_hot_reload_success(self, tmp_path: Path) -> None:
        loader = make_loader(tmp_path)
        skill_path = write_skill_file(tmp_path / "ws" / ".potatocode" / "skills" / "alpha.md")
        loader.load_all()
        write_skill_file(skill_path, name="alpha", body="NEW BODY")
        skill = loader.get("alpha")
        assert skill is not None
        assert skill.prompt_body == "NEW BODY"

    def test_hot_reload_failure_falls_back_to_cache(self, tmp_path: Path, pkg_logs: object) -> None:
        loader = make_loader(tmp_path)
        skill_path = write_skill_file(tmp_path / "ws" / ".potatocode" / "skills" / "alpha.md")
        loader.load_all()
        skill_path.write_text("---\nname: [broken\n---\n", encoding="utf-8")  # 改坏 YAML
        skill = loader.get("alpha")
        assert skill is not None
        assert skill.prompt_body == "SOP BODY"  # 回退缓存旧版本
        assert "热重载失败" in str(pkg_logs.text())

    def test_directory_skill_recognized(self, tmp_path: Path) -> None:
        loader = make_loader(tmp_path)
        skills_dir = tmp_path / "ws" / ".potatocode" / "skills"
        write_skill_file(skills_dir / "dirskill" / "SKILL.md", name="dirskill")
        (skills_dir / "dirskill" / "references").mkdir()
        loader.load_all()
        skill = loader.get("dirskill")
        assert skill is not None
        assert skill.is_directory is True

    def test_bad_file_skipped_others_load(self, tmp_path: Path, pkg_logs: object) -> None:
        loader = make_loader(tmp_path)
        skills_dir = tmp_path / "ws" / ".potatocode" / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "bad.md").write_text("---\nname: [broken\n---\n", encoding="utf-8")
        write_skill_file(skills_dir / "good.md", name="good")
        loader.load_all()  # 不抛异常（N1）
        assert loader.get("good") is not None
        assert loader.get("bad") is None
        assert "Skipping" in str(pkg_logs.text())

    def test_reload_picks_up_new_skill(self, tmp_path: Path) -> None:
        loader = make_loader(tmp_path)
        loader.load_all()
        write_skill_file(tmp_path / "user-skills" / "late.md", name="late")
        loader.reload()
        assert loader.get("late") is not None

    def test_all_preserves_project_first_order(self, tmp_path: Path) -> None:
        loader = make_loader(tmp_path)
        write_skill_file(tmp_path / "user-skills" / "ub.md", name="ub")
        write_skill_file(tmp_path / "ws" / ".potatocode" / "skills" / "pa.md", name="pa")
        loader.load_all()
        assert [skill.name for skill in loader.all()] == ["pa", "ub"]


# ────────────────────────── executor（T4/T5） ──────────────────────────


class TestSkillExecutorSpawnFork:
    @pytest.mark.asyncio
    async def test_spawn_fork_delivers_result_to_host(self, tmp_path: Path) -> None:
        provider = ScriptedProvider([[StreamEvent(text="BG RESULT"), StreamEvent(done=True)]])
        host = FakeHost(tmp_path, provider=provider)
        executor = SkillExecutor(make_loader(tmp_path))
        skill = SkillDef(name="fk", description="d", prompt_body="GO", mode="fork", context="none")
        task = executor.spawn_fork(skill, "", host)
        await asyncio.wait_for(task, 5)
        assert host.delivered == ["BG RESULT"]

    @pytest.mark.asyncio
    async def test_spawn_fork_substitutes_arguments(self, tmp_path: Path) -> None:
        provider = ScriptedProvider([[StreamEvent(text="ok"), StreamEvent(done=True)]])
        host = FakeHost(tmp_path, provider=provider)
        executor = SkillExecutor(make_loader(tmp_path))
        skill = SkillDef(
            name="fk", description="d", prompt_body="GO $ARGUMENTS", mode="fork", context="none"
        )
        task = executor.spawn_fork(skill, "NOW", host)
        await asyncio.wait_for(task, 5)
        assert provider.requests[-1].messages[-1].content.endswith("GO NOW")

    @pytest.mark.asyncio
    async def test_spawn_fork_delivers_error_text_when_execute_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host = FakeHost(tmp_path)
        executor = SkillExecutor(make_loader(tmp_path))
        skill = SkillDef(name="fk", description="d", prompt_body="GO", mode="fork")

        async def _boom(*args: object, **kwargs: object) -> str:
            raise RuntimeError("boom")

        monkeypatch.setattr(executor, "execute_fork", _boom)
        task = executor.spawn_fork(skill, "", host)
        await asyncio.wait_for(task, 5)
        assert len(host.delivered) == 1
        assert "fork 失败" in host.delivered[0]


class TestSkillExecutorFork:
    @pytest.mark.asyncio
    async def test_fork_context_none_collects_stream_text(self, tmp_path: Path) -> None:
        provider = ScriptedProvider([[StreamEvent(text="FORK "), StreamEvent(text="RESULT"),
                                      StreamEvent(done=True)]])
        host = FakeHost(tmp_path, provider=provider)
        executor = SkillExecutor(make_loader(tmp_path))
        skill = SkillDef(name="fk", description="d", prompt_body="GO", mode="fork", context="none")
        final_text = await executor.execute_fork(skill, "", host)
        assert final_text == "FORK RESULT"
        # fork 请求不带 LoadSkill（系统级工具被剔除），空注册中心下也没有其他工具
        assert all(call.tools == [] for call in provider.requests)

    @pytest.mark.asyncio
    async def test_fork_context_full_copies_main_history(self, tmp_path: Path) -> None:
        """full 档：深拷贝主对话历史，并把 SOP 追加为末条 user 消息。"""
        main_conv = Conversation()
        main_conv.add_user("user asked something")
        main_conv.add_assistant("assistant answered")
        provider = ScriptedProvider([
            [StreamEvent(text="FORK DONE"), StreamEvent(done=True)],  # fork 主请求
        ])
        host = FakeHost(tmp_path, provider=provider, conversation=main_conv)
        executor = SkillExecutor(make_loader(tmp_path))
        skill = SkillDef(name="fk", description="d", prompt_body="GO", mode="fork", context="full")
        final_text = await executor.execute_fork(skill, "", host)
        assert final_text == "FORK DONE"
        # fork 主跑的历史应完整继承主对话，末条 user 消息带 SOP
        fork_messages = provider.requests[0].messages
        assert [message.content for message in fork_messages[:2]] == [
            "user asked something",
            "assistant answered",
        ]
        assert fork_messages[-1].role == "user"
        assert "GO" in fork_messages[-1].content

    @pytest.mark.asyncio
    async def test_fork_context_recent_carries_last_messages(self, tmp_path: Path) -> None:
        main_conv = Conversation()
        main_conv.add_user("q1")
        main_conv.add_assistant("a1")
        main_conv.add_user("q2")
        provider = ScriptedProvider([[StreamEvent(text="OK"), StreamEvent(done=True)]])
        host = FakeHost(tmp_path, provider=provider, conversation=main_conv)
        executor = SkillExecutor(make_loader(tmp_path))
        skill = SkillDef(
            name="fk", description="d", prompt_body="GO", mode="fork", context="recent"
        )
        final_text = await executor.execute_fork(skill, "", host)
        assert final_text == "OK"
        history_contents = [message.content for message in provider.requests[0].messages]
        assert any("q1" in content for content in history_contents)
        assert any("a1" in content for content in history_contents)
        # q2 与 SOP 同为 user：规范化后合并进最后一条 user 消息
        assert history_contents[-1].endswith("GO")
        assert "q2" in history_contents[-1]

    @pytest.mark.asyncio
    async def test_fork_without_provider_returns_error_text(self, tmp_path: Path) -> None:
        host = FakeHost(tmp_path, provider=None)
        executor = SkillExecutor(make_loader(tmp_path))
        skill = SkillDef(name="fk", description="d", prompt_body="GO", mode="fork")
        final_text = await executor.execute_fork(skill, "", host)
        assert "fork 失败" in final_text


# ────────────────────────── LoadSkill 工具（T7/T11） ──────────────────────────


class TestLoadSkillTool:
    def test_tool_metadata(self) -> None:
        tool = LoadSkill()
        assert tool.name() == "LoadSkill"
        assert tool.read_only() is True  # read-only 类别：不弹权限提示（N2）

    @pytest.mark.asyncio
    async def test_uninitialized_returns_error(self) -> None:
        tool = LoadSkill()
        result = await tool.execute(json.dumps({"name": "alpha"}))
        assert result.is_error is True
        assert "not properly initialized" in result.content

    @pytest.mark.asyncio
    async def test_inline_returns_sop_body_with_args(self, tmp_path: Path) -> None:
        loader = make_loader(tmp_path)
        write_skill_file(
            tmp_path / "ws" / ".potatocode" / "skills" / "alpha.md", body="DO $ARGUMENTS"
        )
        loader.load_all()
        tool = LoadSkill()
        tool.set_loader(loader)
        tool.set_agent(lambda: MagicMock())
        result = await tool.execute(json.dumps({"name": "alpha", "args": "NOW"}))
        assert result.is_error is False
        assert "activated" in result.content
        assert "DO NOW" in result.content  # $ARGUMENTS 已替换

    @pytest.mark.asyncio
    async def test_fork_starts_background_and_reports(self, tmp_path: Path) -> None:
        loader = make_loader(tmp_path)
        write_skill_file(tmp_path / "ws" / ".potatocode" / "skills" / "fk.md",
                         name="fk", mode="fork")
        loader.load_all()
        provider = ScriptedProvider([[StreamEvent(text="BG DONE"), StreamEvent(done=True)]])
        host = FakeHost(tmp_path, provider=provider)
        executor = SkillExecutor(loader)
        tool = LoadSkill()
        tool.set_loader(loader)
        tool.set_executor(executor)
        tool.set_host(host)
        tool.set_agent(lambda: MagicMock())
        result = await tool.execute(json.dumps({"name": "fk"}))
        assert result.is_error is False
        assert "后台启动" in result.content  # 只报告启动状态，不阻塞等结果
        deadline = asyncio.get_running_loop().time() + 5
        while not host.delivered and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert host.delivered == ["BG DONE"]  # 结果经宿主回流

    @pytest.mark.asyncio
    async def test_fork_without_executor_returns_error(self, tmp_path: Path) -> None:
        loader = make_loader(tmp_path)
        write_skill_file(tmp_path / "ws" / ".potatocode" / "skills" / "fk.md",
                         name="fk", mode="fork")
        loader.load_all()
        tool = LoadSkill()
        tool.set_loader(loader)
        tool.set_agent(lambda: MagicMock())
        result = await tool.execute(json.dumps({"name": "fk"}))
        assert result.is_error is True
        assert "not properly initialized" in result.content

    @pytest.mark.asyncio
    async def test_load_unknown_lists_catalog(self, tmp_path: Path) -> None:
        loader = make_loader(tmp_path)
        write_skill_file(tmp_path / "ws" / ".potatocode" / "skills" / "alpha.md")
        loader.load_all()
        tool = LoadSkill()
        tool.set_loader(loader)
        tool.set_agent(lambda: MagicMock())
        result = await tool.execute(json.dumps({"name": "ghost"}))
        assert result.is_error is True
        assert "unknown skill: ghost" in result.content
        assert "alpha" in result.content  # 错误信息里列出可用 skill

    @pytest.mark.asyncio
    async def test_missing_name_param(self, tmp_path: Path) -> None:
        tool = LoadSkill()
        tool.set_loader(make_loader(tmp_path))
        tool.set_agent(lambda: MagicMock())
        result = await tool.execute("{}")
        assert result.is_error is True
        assert "name" in result.content


# ────────────────────────── Agent 集成（T6/T11） ──────────────────────────


class TestAgentSkillIntegration:
    def make_agent(self, tmp_path: Path, skill_catalog: str = "") -> Agent:
        return Agent(
            MagicMock(),
            MagicMock(),
            MagicMock(),
            skill_catalog=skill_catalog,
            runtime=new_runtime(workspace=str(tmp_path)),
        )

    def test_constructor_stores_skill_catalog(self, tmp_path: Path) -> None:
        agent = self.make_agent(tmp_path, skill_catalog="CATALOG TEXT")
        assert agent._skill_catalog == "CATALOG TEXT"

    def test_set_skill_catalog_updates(self, tmp_path: Path) -> None:
        agent = self.make_agent(tmp_path)
        agent.set_skill_catalog("NEW CATALOG")
        assert agent._skill_catalog == "NEW CATALOG"


class TestSkillsCatalogBlock:
    def test_render_catalog_empty(self) -> None:
        assert render_skills_catalog([]) == ""

    def test_render_catalog_lists_items(self) -> None:
        text = render_skills_catalog([SkillCatalogItem(name="alpha", description="d")])
        assert "## Available Skills" in text
        assert "- alpha: d" in text


# ────────────────────────── 命令注册（T9/T11） ──────────────────────────


class TestRegisterSkillCommands:
    def test_each_skill_becomes_command_with_skill_prefix(self, tmp_path: Path) -> None:
        loader = make_loader(tmp_path)
        skills_dir = tmp_path / "ws" / ".potatocode" / "skills"
        write_skill_file(skills_dir / "alpha.md", name="alpha")
        write_skill_file(skills_dir / "forky.md", name="forky", mode="fork")
        loader.load_all()
        reg = CommandRegistry()
        register_skill_commands(reg, loader, SkillExecutor(loader))
        alpha_cmd = reg.lookup("alpha")
        assert alpha_cmd is not None
        assert alpha_cmd.description.startswith(SKILL_COMMAND_DESCRIPTION_PREFIX)
        assert alpha_cmd.kind is Kind.PROMPT  # inline 走提示词注入
        forky_cmd = reg.lookup("forky")
        assert forky_cmd is not None
        assert forky_cmd.kind is Kind.UI  # fork 走后台任务

    def test_reload_removes_deleted_skill_command(self, tmp_path: Path) -> None:
        loader = make_loader(tmp_path)
        skill_path = write_skill_file(tmp_path / "ws" / ".potatocode" / "skills" / "alpha.md")
        loader.load_all()
        reg = CommandRegistry()
        executor = SkillExecutor(loader)
        register_skill_commands(reg, loader, executor)
        skill_path.unlink()
        loader.reload()
        register_skill_commands(reg, loader, executor)  # 幂等重建
        assert reg.lookup("alpha") is None

    def test_skill_command_shadows_builtin(self, tmp_path: Path) -> None:
        loader = make_loader(tmp_path)
        write_skill_file(tmp_path / "ws" / ".potatocode" / "skills" / "review.md", name="review")

        async def _builtin_handler(ui) -> None:  # noqa: ANN001 — 测试桩
            pass

        reg = CommandRegistry()
        reg.register(Command(name="review", description="builtin review", kind=Kind.LOCAL,
                             handler=_builtin_handler))
        loader.load_all()
        register_skill_commands(reg, loader, SkillExecutor(loader))  # F14：skill 优先覆盖
        review_cmd = reg.lookup("review")
        assert review_cmd is not None
        assert "builtin" not in review_cmd.description
        assert review_cmd.description.startswith("[skill]")

    def test_build_skill_catalog_text(self, tmp_path: Path) -> None:
        loader = make_loader(tmp_path)
        write_skill_file(tmp_path / "ws" / ".potatocode" / "skills" / "alpha.md", name="alpha")
        loader.load_all()
        catalog_text = build_skill_catalog_text(loader)
        assert "- alpha: A test skill" in catalog_text
        assert "LoadSkill" in catalog_text


class TestSkillManageCommand:
    def test_list_subcommand(self, tmp_path: Path) -> None:
        import asyncio

        loader = make_loader(tmp_path)
        write_skill_file(tmp_path / "ws" / ".potatocode" / "skills" / "alpha.md", name="alpha")
        loader.load_all()
        reg = CommandRegistry()
        register_skill_manage_command(reg, loader, on_reload=lambda: None)
        cmd = reg.lookup("skill")
        assert cmd is not None and cmd.accepts_args is True
        ui = RecordingUI()
        assert cmd.args_handler is not None
        asyncio.run(cmd.args_handler(ui, "list"))
        joined = "\n".join(ui.printed)
        assert "alpha" in joined and "[project]" in joined

    def test_reload_subcommand_calls_callback(self, tmp_path: Path) -> None:
        import asyncio

        loader = make_loader(tmp_path)
        loader.load_all()
        reload_calls: list[int] = []
        reg = CommandRegistry()
        register_skill_manage_command(reg, loader, on_reload=lambda: reload_calls.append(1))
        cmd = reg.lookup("skill")
        assert cmd is not None and cmd.args_handler is not None
        asyncio.run(cmd.args_handler(RecordingUI(), "reload"))
        assert reload_calls == [1]

    def test_info_unknown_skill_errors(self, tmp_path: Path) -> None:
        import asyncio

        loader = make_loader(tmp_path)
        loader.load_all()
        reg = CommandRegistry()
        register_skill_manage_command(reg, loader, on_reload=lambda: None)
        cmd = reg.lookup("skill")
        assert cmd is not None and cmd.args_handler is not None
        ui = RecordingUI()
        asyncio.run(cmd.args_handler(ui, "info ghost"))
        assert ui.errors and "ghost" in ui.errors[0]


# ────────────────────────── InstallSkill（T12/T11） ──────────────────────────

SKILL_MD_BYTES = (
    b"---\nname: alpha\ndescription: \"installed skill\"\nmode: inline\n---\n\nSOP\n"
)


class TestParseSkillUrl:
    def test_skills_sh_repo(self) -> None:
        from potatocode.skills.install import parse_skill_url

        location = parse_skill_url("https://skills.sh/owner/my-skill")
        assert (location.owner, location.repo, location.subdir, location.single_file) == (
            "owner",
            "my-skill",
            "",
            False,
        )

    def test_skills_sh_with_subdir(self) -> None:
        from potatocode.skills.install import parse_skill_url

        location = parse_skill_url("https://skills.sh/owner/my-skill/nested/dir")
        assert location.subdir == "nested/dir"

    def test_github_repo_root(self) -> None:
        from potatocode.skills.install import parse_skill_url

        location = parse_skill_url("https://github.com/owner/repo")
        assert (location.owner, location.repo, location.ref, location.subdir) == (
            "owner",
            "repo",
            "",
            "",
        )

    def test_github_tree_with_ref_and_subdir(self) -> None:
        from potatocode.skills.install import parse_skill_url

        location = parse_skill_url("https://github.com/owner/repo/tree/v1.2/sub/dir")
        assert (location.ref, location.subdir) == ("v1.2", "sub/dir")

    def test_raw_single_skill_md(self) -> None:
        from potatocode.skills.install import parse_skill_url

        location = parse_skill_url(
            "https://raw.githubusercontent.com/owner/repo/main/deep/SKILL.md"
        )
        assert location.single_file is True
        assert (location.ref, location.subdir) == ("main", "deep/SKILL.md")

    def test_skills_sh_www_prefix(self) -> None:
        """浏览器复制出的地址常带 www. 前缀，需归一化识别（regression）。"""
        from potatocode.skills.install import parse_skill_url

        location = parse_skill_url("https://www.skills.sh/anthropics/skills/frontend-design")
        assert (location.owner, location.repo, location.subdir) == (
            "anthropics",
            "skills",
            "frontend-design",
        )

    def test_github_www_prefix(self) -> None:
        from potatocode.skills.install import parse_skill_url

        location = parse_skill_url("https://www.github.com/owner/repo/tree/main/sub")
        assert (location.owner, location.repo, location.ref, location.subdir) == (
            "owner",
            "repo",
            "main",
            "sub",
        )

    def test_rejects_other_hosts(self) -> None:
        from potatocode.skills.install import parse_skill_url

        with pytest.raises(ValueError, match="不支持"):
            parse_skill_url("https://example.com/owner/repo")


def make_install_client(
    listings: dict[str, object],
    files: dict[str, bytes],
    trees: dict[str, object] | None = None,
) -> httpx.AsyncClient:
    """MockTransport 客户端：按 URL 路由 contents 列表 / 文件下载 / git trees（不触网）。

    files 的键既可以是完整 URL（download_url 回退路径），也可以是 contents 相对路径
    （raw 媒体类型的 API 直取路径）；trees 的键是 ref 名（如 "HEAD"）。
    """
    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/git/trees/" in url:
            ref = url.split("/git/trees/", 1)[1].split("?")[0]
            if trees and ref in trees:
                return httpx.Response(200, json=trees[ref])
            return httpx.Response(404)
        if "/contents" in url:
            # "/contents"（仓库根）或 "/contents/<path>"；去 ref 查询参数后匹配清单
            after_contents = url.split("/contents", 1)[1]
            path_after_contents = after_contents.split("?")[0].lstrip("/")
            # raw 媒体类型 = 单文件内容下载，按 contents 相对路径匹配 files
            if request.headers.get("accept") == "application/vnd.github.raw":
                if path_after_contents in files:
                    return httpx.Response(200, content=files[path_after_contents])
                return httpx.Response(404)
            for listed_path, listing in listings.items():
                if listed_path == path_after_contents:
                    return httpx.Response(200, json=listing)
            return httpx.Response(404)
        for file_url, content in files.items():
            if url == file_url:
                return httpx.Response(200, content=content)
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


class TestInstallSkill:
    @pytest.mark.asyncio
    async def test_directory_install_happy_path(self, tmp_path: Path) -> None:

        from potatocode.skills.install import install_skill

        listings = {
            "sub": [
                {
                    "name": "SKILL.md",
                    "path": "sub/SKILL.md",
                    "type": "file",
                    "size": len(SKILL_MD_BYTES),
                    "download_url": "https://raw.test/sub/SKILL.md",
                },
                {
                    "name": "helper.py",
                    "path": "sub/references/helper.py",
                    "type": "file",
                    "size": 10,
                    "download_url": "https://raw.test/sub/references/helper.py",
                },
            ]
        }
        files = {
            "https://raw.test/sub/SKILL.md": SKILL_MD_BYTES,
            "https://raw.test/sub/references/helper.py": b"# helper",
        }
        client = make_install_client(listings, files)
        try:
            skill_name = await install_skill(
                "https://github.com/owner/repo/tree/main/sub", tmp_path, client=client
            )
        finally:
            await client.aclose()
        assert skill_name == "alpha"
        installed = tmp_path / "alpha" / "SKILL.md"
        assert installed.read_text(encoding="utf-8").startswith("---")
        assert (tmp_path / "alpha" / "references" / "helper.py").read_bytes() == b"# helper"
        # 暂存目录已清理：安装根目录下只剩 alpha
        assert [entry.name for entry in tmp_path.iterdir()] == ["alpha"]

    @pytest.mark.asyncio
    async def test_missing_skill_md_rejects_and_cleans(self, tmp_path: Path) -> None:
        from potatocode.skills.install import InstallSkillError, install_skill

        listings = {
            "": [
                {
                    "name": "README.md",
                    "path": "README.md",
                    "type": "file",
                    "size": 5,
                    "download_url": "https://raw.test/README.md",
                }
            ]
        }
        client = make_install_client(listings, {"https://raw.test/README.md": b"readme"})
        try:
            with pytest.raises(InstallSkillError, match="SKILL.md"):
                await install_skill("https://github.com/owner/repo", tmp_path, client=client)
        finally:
            await client.aclose()
        # 拒绝安装且暂存已清理（checklist §1.6）
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_unsafe_path_rejected(self, tmp_path: Path) -> None:
        from potatocode.skills.install import InstallSkillError, install_skill

        listings = {
            "": [
                {
                    "name": "SKILL.md",
                    "path": "../SKILL.md",
                    "type": "file",
                    "size": 5,
                    "download_url": "https://raw.test/SKILL.md",
                }
            ]
        }
        client = make_install_client(listings, {})
        try:
            with pytest.raises(InstallSkillError, match="不安全"):
                await install_skill("https://github.com/owner/repo", tmp_path, client=client)
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_file_count_limit(self, tmp_path: Path) -> None:
        from potatocode.skills.install import MAX_FILE_COUNT, InstallSkillError, install_skill

        entries = [
            {
                "name": f"f{i}.txt",
                "path": f"f{i}.txt",
                "type": "file",
                "size": 1,
                "download_url": f"https://raw.test/f{i}.txt",
            }
            for i in range(MAX_FILE_COUNT + 1)
        ]
        # 前 64 个文件正常可下载，第 65 个在预算检查处被拒（不发起下载）
        limit_files = {
            f"https://raw.test/f{i}.txt": b"x" for i in range(MAX_FILE_COUNT)
        }
        client = make_install_client({"": entries}, limit_files)
        try:
            with pytest.raises(InstallSkillError, match="文件数"):
                await install_skill("https://github.com/owner/repo", tmp_path, client=client)
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_raw_single_file_install(self, tmp_path: Path) -> None:
        from potatocode.skills.install import install_skill

        client = make_install_client(
            {}, {"https://raw.githubusercontent.com/owner/repo/main/SKILL.md": SKILL_MD_BYTES}
        )
        try:
            skill_name = await install_skill(
                "https://raw.githubusercontent.com/owner/repo/main/SKILL.md",
                tmp_path,
                client=client,
            )
        finally:
            await client.aclose()
        assert skill_name == "alpha"
        assert (tmp_path / "alpha" / "SKILL.md").is_file()

    @pytest.mark.asyncio
    async def test_file_download_prefers_contents_api(self, tmp_path: Path) -> None:
        """文件内容经 Contents API raw 媒体类型直取；entry 无 download_url 也应成功。"""
        from potatocode.skills.install import install_skill

        listings = {
            "sub": [
                {
                    "name": "SKILL.md",
                    "path": "sub/SKILL.md",
                    "type": "file",
                    "size": len(SKILL_MD_BYTES),
                    # 刻意不给 download_url：回退路径不可用，直取必须成功
                }
            ]
        }
        client = make_install_client(listings, {"sub/SKILL.md": SKILL_MD_BYTES})
        try:
            skill_name = await install_skill(
                "https://github.com/owner/repo/tree/main/sub", tmp_path, client=client
            )
        finally:
            await client.aclose()
        assert skill_name == "alpha"
        assert (tmp_path / "alpha" / "SKILL.md").is_file()

    @pytest.mark.asyncio
    async def test_skills_sh_logical_name_relocated(self, tmp_path: Path) -> None:
        """skills.sh 第三段是逻辑 skill 名：物理路径 404 时用 git trees 重定位。"""
        from potatocode.skills.install import install_skill

        listings = {
            # 逻辑名 frontend-design 不是仓库物理路径 → 首次探测 404
            "skills/frontend-design": [
                {
                    "name": "SKILL.md",
                    "path": "skills/frontend-design/SKILL.md",
                    "type": "file",
                    "size": len(SKILL_MD_BYTES),
                }
            ]
        }
        trees = {
            "HEAD": {
                "truncated": False,
                "tree": [
                    {"path": "README.md", "type": "blob"},
                    {"path": "skills/frontend-design/SKILL.md", "type": "blob"},
                    {"path": "skills/other/SKILL.md", "type": "blob"},
                ],
            }
        }
        client = make_install_client(
            listings, {"skills/frontend-design/SKILL.md": SKILL_MD_BYTES}, trees=trees
        )
        try:
            skill_name = await install_skill(
                "https://skills.sh/anthropics/skills/frontend-design", tmp_path, client=client
            )
        finally:
            await client.aclose()
        assert skill_name == "alpha"
        assert (tmp_path / "alpha" / "SKILL.md").is_file()
