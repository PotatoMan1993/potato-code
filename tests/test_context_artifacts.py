"""工具结果落盘的阈值、保真、权限与失败原子性。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from potatocode.context import artifacts
from potatocode.context.artifacts import ArtifactStore
from potatocode.conversation import Conversation
from potatocode.llm import ToolCall, ToolResult
from potatocode.permission import Mode
from potatocode.permission.engine import Engine
from potatocode.permission.rule import Rule, RuleSet


def _engine(root: Path) -> Engine:
    return Engine(str(root.resolve()), [], RuleSet(), RuleSet(), RuleSet(), "")


def _conversation(*batches: list[str], error: bool = False) -> Conversation:
    conv = Conversation()
    conv.add_user("处理工具结果")
    for batch_index, batch in enumerate(batches):
        calls = [ToolCall(f"{batch_index}-{i}", "read_file", "{}") for i in range(len(batch))]
        conv.add_assistant_with_tool_calls("", calls)
        conv.add_tool_results(
            [ToolResult(call.id, text, error) for call, text in zip(calls, batch, strict=True)]
        )
    return conv


async def _prepare_commit(store: ArtifactStore, conv: Conversation):
    snapshot = conv.snapshot()
    patches = await store.prepare(snapshot, Mode.DEFAULT, asyncio.Event())
    if patches:
        conv.replace_tool_results(snapshot.revision, patches)
    return patches


@pytest.mark.parametrize("size,expected", [(50_000, 0), (50_001, 1)])
async def test_single_boundary_does_not_modify_history_before_commit(tmp_path, size, expected):
    conv = _conversation(["a" * size])
    before = conv.messages()
    store = ArtifactStore(_engine(tmp_path), conv.session_id)
    patches = await store.prepare(conv.snapshot(), Mode.DEFAULT, asyncio.Event())
    assert len(patches) == expected
    assert conv.messages() == before


async def test_full_text_newlines_preview_and_error_survive(tmp_path):
    content = "甲\r\n" * 16_667
    conv = _conversation([content], error=True)
    store = ArtifactStore(_engine(tmp_path), conv.session_id)
    patches = await _prepare_commit(store, conv)
    result = patches[0].result
    assert result.content.startswith(content[:1500])
    assert result.tool_call_id == "0-0"
    assert result.is_error
    paths = list(store.directory.glob("*.txt"))
    assert len(paths) == 1
    assert paths[0].read_bytes() == content.encode("utf-8")
    assert str(paths[0]) in result.content
    assert store.visible_text(patches[0].entry_id, 0, result.content) == content[:1500]


@pytest.mark.parametrize(
    "sizes,selected", [([45_000] * 5, 0), ([40_000, 50_000, 48_000, 45_000, 20_000], 1)]
)
async def test_aggregate_largest_first_stable_ties(tmp_path, sizes, selected):
    conv = _conversation([str(i) * size for i, size in enumerate(sizes)])
    store = ArtifactStore(_engine(tmp_path), conv.session_id)
    patches = await _prepare_commit(store, conv)
    assert [p.result_index for p in patches] == [selected]
    assert sum(len(r.content) for r in conv.messages()[-1].tool_results) <= 200_000


async def test_exact_aggregate_and_separate_messages_do_not_spill(tmp_path):
    conv = _conversation(["a" * 50_000] * 4, ["b" * 45_000] * 3)
    store = ArtifactStore(_engine(tmp_path), conv.session_id)
    assert await _prepare_commit(store, conv) == []
    assert not store.directory.exists()


async def test_repeated_checks_and_original_retry_reuse_one_file(tmp_path):
    conv = _conversation(["x" * 50_001])
    store = ArtifactStore(_engine(tmp_path), conv.session_id)
    first = await store.prepare(conv.snapshot(), Mode.DEFAULT, asyncio.Event())
    second = await store.prepare(conv.snapshot(), Mode.DEFAULT, asyncio.Event())
    assert first == second
    conv.replace_tool_results(conv.revision, second)
    assert await _prepare_commit(store, conv) == []
    assert len(list(store.directory.glob("*.txt"))) == 1


async def test_original_text_resembling_preview_is_not_internal_metadata(tmp_path):
    conv = _conversation(["[工具结果已存盘]" + "x" * 50_001])
    store = ArtifactStore(_engine(tmp_path), conv.session_id)
    assert len(await _prepare_commit(store, conv)) == 1


async def test_read_deny_stops_and_preserves_original(tmp_path):
    conv = _conversation(["x" * 50_001])
    engine = _engine(tmp_path)
    engine.local.deny.append(Rule("Read", ".potatocode/context/**", False))
    store = ArtifactStore(engine, conv.session_id)
    before = conv.messages()
    with pytest.raises(ValueError, match="不可读取"):
        await store.prepare(conv.snapshot(), Mode.DEFAULT, asyncio.Event())
    assert conv.messages() == before


async def test_disk_failure_does_not_return_partial_patches(tmp_path, monkeypatch):
    conv = _conversation(["x" * 50_001, "y" * 50_001])
    store = ArtifactStore(_engine(tmp_path), conv.session_id)
    original_write = store._write
    count = 0

    def fail_second(path, content, mode):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("模拟磁盘失败")
        original_write(path, content, mode)

    monkeypatch.setattr(store, "_write", fail_second)
    before = conv.messages()
    with pytest.raises(OSError, match="磁盘"):
        await store.prepare(conv.snapshot(), Mode.DEFAULT, asyncio.Event())
    assert conv.messages() == before
    assert len(list(store.directory.glob("*.txt"))) == 1


async def test_cancel_during_io_never_commits_history(tmp_path, monkeypatch):
    conv = _conversation(["x" * 50_001])
    store = ArtifactStore(_engine(tmp_path), conv.session_id)
    cancel = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_write = store._write

    def cancel_after_write(path, content, mode):
        original_write(path, content, mode)
        loop.call_soon_threadsafe(cancel.set)

    monkeypatch.setattr(store, "_write", cancel_after_write)
    before = conv.messages()
    with pytest.raises(asyncio.CancelledError):
        await store.prepare(conv.snapshot(), Mode.DEFAULT, cancel)
    assert conv.messages() == before


async def test_no_reducible_results_reports_without_looping(tmp_path):
    conv = _conversation(["x" * 1000] * 201)
    store = ArtifactStore(_engine(tmp_path), conv.session_id)
    with pytest.raises(ValueError, match="无法进一步缩减"):
        await store.prepare(conv.snapshot(), Mode.DEFAULT, asyncio.Event())
    assert not store.directory.exists()


async def test_already_spilled_results_are_not_wrapped_when_previews_exceed_budget(
    tmp_path, monkeypatch
):
    """降低测试预算模拟大量预览，仍只报告无进展而不对预览再次存盘。"""
    conv = _conversation(["x" * 50_001, "y" * 50_001, "z" * 50_001])
    store = ArtifactStore(_engine(tmp_path), conv.session_id)
    await _prepare_commit(store, conv)
    before = conv.messages()
    monkeypatch.setattr(artifacts, "AGGREGATE_LIMIT", 4_000)
    with pytest.raises(ValueError, match="无法进一步缩减"):
        await store.prepare(conv.snapshot(), Mode.DEFAULT, asyncio.Event())
    assert conv.messages() == before
    assert len(list(store.directory.glob("*.txt"))) == 3


async def test_tampered_or_missing_artifact_is_not_claimed_readable(tmp_path):
    conv = _conversation(["x" * 50_001])
    store = ArtifactStore(_engine(tmp_path), conv.session_id)
    await _prepare_commit(store, conv)
    path = next(store.directory.glob("*.txt"))
    path.write_text("已变化", encoding="utf-8")
    with pytest.raises(ValueError, match="内容已变化"):
        await store.prepare(conv.snapshot(), Mode.DEFAULT, asyncio.Event())
    path.unlink()
    with pytest.raises(OSError):
        await store.prepare(conv.snapshot(), Mode.DEFAULT, asyncio.Event())


async def test_symlinked_storage_cannot_escape_project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    folder = root / ".potatocode" / "context"
    folder.mkdir(parents=True)
    try:
        (folder / "session").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("运行环境未开放创建目录符号链接")
    store = ArtifactStore(_engine(root), "session")
    conv = _conversation(["x" * 50_001])
    with pytest.raises(ValueError, match="超出"):
        await store.prepare(conv.snapshot(), Mode.DEFAULT, asyncio.Event())
    assert list(outside.iterdir()) == []
