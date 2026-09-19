"""上下文附件的来源、最近次序、权限、版本与实际可见范围。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from potatocode.context.artifacts import ArtifactStore
from potatocode.context.attachments import ContextAttachments
from potatocode.conversation import Conversation
from potatocode.llm import ToolCall, ToolResult
from potatocode.permission import Decision, Mode
from potatocode.permission.engine import Engine
from potatocode.permission.rule import Rule, RuleSet
from potatocode.tool.file_observation import FileObservation, text_version
from potatocode.tool.read_file import ReadFileTool


def _engine(root: Path) -> Engine:
    return Engine(str(root.resolve()), [], RuleSet(), RuleSet(), RuleSet(), "")


async def _read_skill(attachments, conv, path, *, offset=0, limit=2000):
    call_id = f"read-{conv.revision}"
    args = json.dumps({"path": str(path), "offset": offset, "limit": limit})
    result = await ReadFileTool().execute(args)
    assert not result.is_error and result.file_observation is not None
    attachments.observe_file(call_id, result.file_observation)
    conv.add_assistant_with_tool_calls("", [ToolCall(call_id, "read_file", args)])
    conv.add_tool_results([ToolResult(call_id, result.content)])
    return result


async def _confirm(attachments, conv, store):
    snapshot = conv.snapshot()
    patches = await store.prepare(snapshot, Mode.DEFAULT, asyncio.Event())
    if patches:
        conv.replace_tool_results(snapshot.revision, patches)
    attachments.confirm_delivered(conv.snapshot(), store)


async def test_recent_files_deduplicate_follow_success_order_and_limit(tmp_path):
    attachments = ContextAttachments(_engine(tmp_path))
    paths = []
    for i in range(7):
        path = tmp_path / f"{i}.py"
        path.write_text(str(i) * 10_005, encoding="utf-8")
        paths.append(path)
        attachments.observe_file(str(i), FileObservation(str(path), "write"))
    attachments.observe_file("again", FileObservation(str(paths[1]), "read"))
    bundle = await attachments.collect(Mode.DEFAULT, asyncio.Event())
    assert [Path(f.path).name for f in bundle.files] == ["1.py", "6.py", "5.py", "4.py", "3.py"]
    assert all(len(f.text) == 10_000 and f.truncated for f in bundle.files)
    repeated = await attachments.collect(Mode.DEFAULT, asyncio.Event())
    assert repeated == bundle


async def test_deleted_denied_and_ask_files_report_without_read_or_approval(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    attachments = ContextAttachments(engine)
    path = tmp_path / "source.py"
    path.write_text("原始内容", encoding="utf-8")
    attachments.observe_file("write", FileObservation(str(path), "write"))
    engine.local.deny.append(Rule("Read", "source.py", False))
    bundle = await attachments.collect(Mode.DEFAULT, asyncio.Event())
    assert bundle.files[0].text == ""
    assert "deny" in bundle.files[0].omitted_reason
    engine.local.deny.clear()
    path.unlink()
    bundle = await attachments.collect(Mode.DEFAULT, asyncio.Event())
    assert "FileNotFoundError" in bundle.files[0].omitted_reason
    monkeypatch.setattr(engine, "check", lambda *_: (Decision.ASK, "需要批准"))
    bundle = await attachments.collect(Mode.DEFAULT, asyncio.Event())
    assert bundle.files[0].text == ""
    assert bundle.files[0].omitted_reason == "需要批准"


@pytest.mark.parametrize(
    "name",
    [
        ".potatocode/config.yaml",
        ".potatocode.yaml",
        "settings.local.json",
        ".env",
        ".env.dev",
        "api_keys.txt",
        "client.pem",
        "client.key",
        "auth.json",
    ],
)
async def test_credential_sources_are_not_reinjected(tmp_path, name):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("仅测试用的敏感配置占位", encoding="utf-8")
    attachments = ContextAttachments(_engine(tmp_path))
    attachments.observe_file("read", FileObservation(str(path), "read"))
    attachments.record_loaded_skill(str(path), "占位定义", "v1", True)
    bundle = await attachments.collect(Mode.DEFAULT, asyncio.Event())
    assert bundle.files == bundle.skills == []


async def test_partial_skill_uses_loaded_version_and_survives_removed_history(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text("第一行\n第二行\n第三行", encoding="utf-8")
    conv = Conversation()
    attachments = ContextAttachments(_engine(tmp_path))
    store = ArtifactStore(attachments.engine, conv.session_id)
    await _read_skill(attachments, conv, path, offset=1, limit=1)
    await _confirm(attachments, conv, store)
    path.write_text("这是磁盘上的另一个版本", encoding="utf-8")
    bundle = await attachments.collect(Mode.DEFAULT, asyncio.Event())
    assert bundle.files == []
    assert len(bundle.skills) == 1
    skill = bundle.skills[0]
    assert "第二行" in skill.text and "另一个版本" not in skill.text
    assert (skill.start_line, skill.end_line, skill.complete) == (2, 2, False)
    assert skill.source_version == text_version("第一行\n第二行\n第三行")
    # 即使原始工具消息不再参与新快照，使用记录仍保留。
    attachments.confirm_delivered(Conversation().snapshot(), store)
    assert (await attachments.collect(Mode.DEFAULT, asyncio.Event())).skills == bundle.skills


async def test_spilled_skill_only_records_the_visible_preview(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text("skill definition " * 4_000, encoding="utf-8")
    conv = Conversation()
    attachments = ContextAttachments(_engine(tmp_path))
    store = ArtifactStore(attachments.engine, conv.session_id)
    result = await _read_skill(attachments, conv, path)
    await _confirm(attachments, conv, store)
    skill = (await attachments.collect(Mode.DEFAULT, asyncio.Event())).skills[0]
    assert skill.text == result.content[:1500]
    assert skill.truncated and not skill.complete
    assert "完整内容：" not in skill.text


async def test_same_version_short_read_keeps_full_skill_new_version_replaces(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text("line one\nline two", encoding="utf-8")
    conv = Conversation()
    attachments = ContextAttachments(_engine(tmp_path))
    store = ArtifactStore(attachments.engine, conv.session_id)
    await _read_skill(attachments, conv, path)
    await _confirm(attachments, conv, store)
    await _read_skill(attachments, conv, path, offset=1, limit=1)
    await _confirm(attachments, conv, store)
    skill = (await attachments.collect(Mode.DEFAULT, asyncio.Event())).skills[0]
    assert skill.complete and "line one" in skill.text
    path.write_text("new one\nnew two", encoding="utf-8")
    await _read_skill(attachments, conv, path, offset=1, limit=1)
    await _confirm(attachments, conv, store)
    skill = (await attachments.collect(Mode.DEFAULT, asyncio.Event())).skills[0]
    assert not skill.complete and "new two" in skill.text and "line one" not in skill.text


async def test_skill_cap_recent_order_and_current_deny(tmp_path):
    attachments = ContextAttachments(_engine(tmp_path))
    for i in range(3):
        path = tmp_path / str(i) / "SKILL.md"
        attachments.record_loaded_skill(str(path), str(i) * 30_000, f"v{i}", True)
    bundle = await attachments.collect(Mode.DEFAULT, asyncio.Event())
    assert [len(s.text) for s in bundle.skills] == [30_000, 20_000, 0]
    assert bundle.skills[1].truncated and not bundle.skills[1].complete
    assert bundle.skills[2].omitted_reason
    attachments.engine.local.deny.append(Rule("Read", "2/SKILL.md", False))
    bundle = await attachments.collect(Mode.DEFAULT, asyncio.Event())
    assert bundle.skills[0].text == "" and "deny" in bundle.skills[0].omitted_reason
    assert sum(len(s.text) for s in bundle.skills) == 50_000


async def test_mentions_and_unconfirmed_reads_do_not_load_skills(tmp_path):
    attachments = ContextAttachments(_engine(tmp_path))
    path = tmp_path / "SKILL.md"
    path.write_text("definition", encoding="utf-8")
    attachments.observe_file("pending", FileObservation(str(path), "read", "definition"))
    # 还没有回灌到即将发送的历史，不声称模型加载了这份 Skill。
    assert (await attachments.collect(Mode.DEFAULT, asyncio.Event())).skills == []


async def test_reused_call_id_matches_latest_result(tmp_path):
    """不同请求复用工具 ID 时不能拿旧结果确认新的 Skill 版本。"""
    attachments = ContextAttachments(_engine(tmp_path))
    conv = Conversation()
    store = ArtifactStore(attachments.engine, conv.session_id)
    path = tmp_path / "SKILL.md"
    for text in ("old definition", "new definition"):
        observation = FileObservation(
            str(path), "read", text, 1, 1, True, False, text_version(text)
        )
        attachments.observe_file("reused", observation)
        conv.add_assistant_with_tool_calls("", [ToolCall("reused", "read_file", "{}")])
        conv.add_tool_results([ToolResult("reused", text)])
        await _confirm(attachments, conv, store)
    skill = (await attachments.collect(Mode.DEFAULT, asyncio.Event())).skills[0]
    assert skill.text == "new definition"


async def test_skill_recency_uses_completion_order_not_result_display_order(tmp_path):
    attachments = ContextAttachments(_engine(tmp_path))
    conv = Conversation()
    store = ArtifactStore(attachments.engine, conv.session_id)
    first = tmp_path / "first" / "SKILL.md"
    last = tmp_path / "last" / "SKILL.md"
    attachments.observe_file("second-call", FileObservation(str(first), "read", "first"))
    attachments.observe_file("first-call", FileObservation(str(last), "read", "last"))
    conv.add_assistant_with_tool_calls(
        "", [ToolCall("first-call", "read_file", "{}"), ToolCall("second-call", "read_file", "{}")]
    )
    conv.add_tool_results([ToolResult("first-call", "last"), ToolResult("second-call", "first")])
    await _confirm(attachments, conv, store)
    bundle = await attachments.collect(Mode.DEFAULT, asyncio.Event())
    assert [s.text for s in bundle.skills] == ["last", "first"]


async def test_cancelled_collection_performs_no_read(tmp_path):
    attachments = ContextAttachments(_engine(tmp_path))
    cancel = asyncio.Event()
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await attachments.collect(Mode.DEFAULT, cancel)
