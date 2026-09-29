"""会话存档单测：JSONL 读写、列表扫描、恢复容错、过期清理（ch11 第 2/3 层）。"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

from potatocode.llm import Message, ToolCall, ToolResult
from potatocode.session import (
    Writer,
    clean_expired,
    list_sessions,
    load_session,
    load_session_full,
    relative_time,
)
from potatocode.session.writer import JSONL_FILENAME


def _session_dir(tmp_path, session_id: str = "20260601-143022-a1b2") -> Path:
    """建出一个会话目录并返回（不写任何内容）。"""
    path = tmp_path / "sessions" / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _lines(session_dir: Path) -> list[dict]:
    """读回存档的每一行（跳过解析失败的半行，仅供测试断言用）。"""
    text = (session_dir / JSONL_FILENAME).read_text(encoding="utf-8")
    out = []
    for line in text.splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


# ────────── 写入（F11/F13/F15） ──────────


def test_writer_append_and_read(tmp_path):
    """写入三条消息 → 逐行读回，字段与顺序正确（AC8）。"""
    session_dir = _session_dir(tmp_path)
    with Writer(str(session_dir), model="fake-model") as writer:
        writer.append(Message(role="user", content="你好"))
        writer.append(Message(role="assistant", content="你好！"))
        writer.append(
            Message(role="assistant", content="", tool_calls=[ToolCall("c1", "read_file", "{}")])
        )

    entries = _lines(session_dir)
    assert [e["role"] for e in entries] == ["user", "assistant", "assistant"]
    assert entries[0]["content"] == "你好"
    assert entries[1]["content"] == "你好！"
    assert all(e["ts"] > 0 for e in entries)
    assert entries[2]["tool_calls"] == [{"id": "c1", "name": "read_file", "input": "{}"}]


def test_first_line_carries_model(tmp_path):
    """只有首行带 model 字段，供会话列表展示（F11/AC8）。"""
    session_dir = _session_dir(tmp_path)
    with Writer(str(session_dir), model="fake-model") as writer:
        writer.append(Message(role="user", content="第一条"))
        writer.append(Message(role="assistant", content="第二条"))

    entries = _lines(session_dir)
    assert entries[0]["model"] == "fake-model"
    assert "model" not in entries[1]


def test_tool_results_round_trip(tmp_path):
    """tool 回合的 tool_results 字段可序列化并可还原（AC8）。"""
    session_dir = _session_dir(tmp_path)
    results = [ToolResult(tool_call_id="c1", content="读取成功", is_error=False)]
    with Writer(str(session_dir)) as writer:
        writer.append(Message(role="tool", tool_results=results))

    entries = _lines(session_dir)
    assert entries[0]["tool_results"][0]["tool_call_id"] == "c1"
    assert entries[0]["tool_results"][0]["content"] == "读取成功"


def test_writer_reopen_appends_without_duplicating_model(tmp_path):
    """恢复后的写入器以追加模式打开同一文件，行号递增（AC18）。"""
    session_dir = _session_dir(tmp_path)
    first = Writer(str(session_dir), model="m")
    first.append(Message(role="user", content="旧消息"))
    first.close()

    second = Writer.open_existing(str(session_dir), model="m")
    second.append(Message(role="user", content="新消息"))
    second.close()

    entries = _lines(session_dir)
    assert [e["content"] for e in entries] == ["旧消息", "新消息"]
    assert "model" not in entries[1]  # 首行的 model 不会被重复写


def test_writer_concurrent_append_is_atomic(tmp_path):
    """多线程并发追加：行数与写入次数一致，每行都是完整 JSON（N2）。"""
    session_dir = _session_dir(tmp_path)
    writer = Writer(str(session_dir))
    n = 60
    barrier = threading.Barrier(n)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            barrier.wait()
            writer.append(Message(role="user", content=f"消息-{i}"))
        except Exception as e:  # pragma: no cover - 竞态命中才会走到
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    writer.close()

    assert errors == []
    assert len(_lines(session_dir)) == n


def test_writer_close_is_idempotent(tmp_path):
    """close 可重复调用，之后再写入只是空操作（不抛异常）。"""
    session_dir = _session_dir(tmp_path)
    writer = Writer(str(session_dir))
    writer.close()
    writer.close()
    writer.append(Message(role="user", content="写不进去了"))
    assert _lines(session_dir) == []


# ────────── 读取与恢复（F12/F21） ──────────


def test_load_session_returns_messages(tmp_path):
    """正常存档能完整还原消息列表。"""
    session_dir = _session_dir(tmp_path)
    with Writer(str(session_dir)) as writer:
        writer.append(Message(role="user", content="你好"))
        writer.append(Message(role="assistant", content="你好！"))

    msgs = load_session(str(session_dir))
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert [m.content for m in msgs] == ["你好", "你好！"]


def test_load_session_starts_after_last_compact_marker(tmp_path):
    """有压缩标记时只加载最后一个标记之后的消息（F12/AC9）。"""
    session_dir = _session_dir(tmp_path)
    with Writer(str(session_dir)) as writer:
        writer.append(Message(role="user", content="旧问题"))
        writer.append(Message(role="assistant", content="旧回答"))
        writer.write_compact_marker()
        writer.append(Message(role="user", content="（摘要）"))
        writer.append(Message(role="assistant", content="压缩后继续"))

    msgs = load_session(str(session_dir))
    assert [m.content for m in msgs] == ["（摘要）", "压缩后继续"]


def test_load_session_skips_bad_lines(tmp_path):
    """手动插入的坏行被跳过，其余消息正常加载（AC14）。"""
    session_dir = _session_dir(tmp_path)
    path = session_dir / JSONL_FILENAME
    path.write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "content": "一", "ts": 1}, ensure_ascii=False),
                "{this is bad json",
                json.dumps({"role": "assistant", "content": "二", "ts": 2}, ensure_ascii=False),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    msgs = load_session(str(session_dir))
    assert [m.content for m in msgs] == ["一", "二"]


def test_load_session_truncates_orphaned_tool_calls(tmp_path):
    """结尾悬空的工具调用回合被截断（AC15/F21-3）。"""
    session_dir = _session_dir(tmp_path)
    with Writer(str(session_dir)) as writer:
        writer.append(Message(role="user", content="读文件"))
        writer.append(Message(role="assistant", content="正在读"))
        writer.append(
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall("c1", "read_file", '{"path": "a.txt"}')],
            )
        )

    msgs = load_session(str(session_dir))
    assert [m.content for m in msgs] == ["读文件", "正在读"]  # 悬空那一条被截断


def test_load_session_keeps_paired_tool_calls(tmp_path):
    """工具调用与结果配对完整时不被截断。"""
    session_dir = _session_dir(tmp_path)
    with Writer(str(session_dir)) as writer:
        writer.append(
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall("c1", "read_file", "{}")],
            )
        )
        writer.append(
            Message(role="tool", tool_results=[ToolResult(tool_call_id="c1", content="内容")])
        )

    msgs = load_session(str(session_dir))
    assert len(msgs) == 2
    assert msgs[1].tool_results[0].content == "内容"


def test_load_session_full_reports_last_ts(tmp_path):
    """load_session_full 返回最后一条有效消息的时间戳（供时间跨度提醒用）。"""
    session_dir = _session_dir(tmp_path)
    with Writer(str(session_dir)) as writer:
        writer.append(Message(role="user", content="一"))

    loaded = load_session_full(str(session_dir))
    assert loaded.last_ts == _lines(session_dir)[0]["ts"]


def test_load_session_missing_file(tmp_path):
    """存档文件不存在 → 返回空会话，不抛异常（N5）。"""
    session_dir = _session_dir(tmp_path)
    assert load_session(str(session_dir)) == []
    assert load_session_full(str(session_dir)).last_ts == 0


# ────────── 会话列表（F18/F20） ──────────


def test_list_sessions_sorted_desc_and_fields(tmp_path):
    """三个有效会话按修改时间倒序返回，字段齐全（AC12）。"""
    sessions_root = tmp_path / "sessions"
    ids = ["20260601-100000-aaaa", "20260602-100000-bbbb", "20260603-100000-cccc"]
    texts = {
        "20260601-100000-aaaa": "题一",
        "20260602-100000-bbbb": "题二",
        "20260603-100000-cccc": "题三",
    }
    for session_id in ids:
        with Writer(str(sessions_root / session_id), model="fake-model") as writer:
            writer.append(Message(role="user", content=texts[session_id]))
            writer.append(Message(role="assistant", content="回复"))

    items = list_sessions(str(sessions_root))

    assert [i.id for i in items] == list(reversed(ids))  # 最新写入的排最前
    assert items[0].title == "题三"
    assert items[0].model == "fake-model"
    assert items[0].size > 0
    assert items[0].dir.endswith(items[0].id)
    assert isinstance(items[0].modified_at, datetime)


def test_list_sessions_skips_old_format_and_empty_dirs(tmp_path):
    """旧格式 id、没有存档文件、空存档的目录都不出现在列表里（AC20/N3）。"""
    sessions_root = tmp_path / "sessions"
    with Writer(str(sessions_root / "20260601-100000-aaaa")) as writer:
        writer.append(Message(role="user", content="新格式"))
    (sessions_root / "1717000000-abc12345").mkdir(parents=True)
    (sessions_root / "1717000000-abc12345" / JSONL_FILENAME).write_text(
        '{"role":"user","content":"旧格式","ts":1}\n', encoding="utf-8"
    )
    (sessions_root / "20260602-100000-bbbb").mkdir(parents=True)  # 无存档文件
    sessions_root.joinpath("20260603-100000-cccc").mkdir(parents=True)
    (sessions_root / "20260603-100000-cccc" / JSONL_FILENAME).write_text("", encoding="utf-8")

    items = list_sessions(str(sessions_root))

    assert [i.id for i in items] == ["20260601-100000-aaaa"]


def test_list_sessions_missing_root(tmp_path):
    """会话根目录不存在时返回空列表，不抛异常（N3）。"""
    assert list_sessions(str(tmp_path / "nope")) == []


def test_title_truncated_to_fifty(tmp_path):
    """标题截断到 50 个字符（含省略号）（F20）。"""
    sessions_root = tmp_path / "sessions"
    with Writer(str(sessions_root / "20260601-100000-aaaa")) as writer:
        writer.append(Message(role="user", content="字" * 200))

    item = list_sessions(str(sessions_root))[0]
    assert len(item.title) == 50
    assert item.title.endswith("…")


def test_list_sessions_skips_unparsable_line_for_title(tmp_path):
    """标题扫描遇到坏行继续往后找第一条 user 消息（N5）。"""
    sessions_root = tmp_path / "sessions"
    session_dir = sessions_root / "20260601-100000-aaaa"
    session_dir.mkdir(parents=True)
    (session_dir / JSONL_FILENAME).write_text(
        '{"role":"assistant","content":"先说话"}\n{坏行\n{"role":"user","content":"真正的问题"}\n',
        encoding="utf-8",
    )

    assert list_sessions(str(sessions_root))[0].title == "真正的问题"


def test_relative_time_buckets():
    """相对时间文案覆盖分钟 / 小时 / 天 / 月四档。"""
    now = datetime(2026, 6, 1, 12, 0, 0)
    assert relative_time(now - timedelta(seconds=5), now) == "just now"
    assert relative_time(now - timedelta(minutes=3), now) == "3 minutes ago"
    assert relative_time(now - timedelta(hours=1), now) == "1 hour ago"
    assert relative_time(now - timedelta(hours=3), now) == "3 hours ago"
    assert relative_time(now - timedelta(days=1), now) == "1 day ago"
    assert relative_time(now - timedelta(days=45), now) == "1 month ago"


# ────────── 过期清理（F25/F26） ──────────


def test_clean_expired_removes_only_old_new_format(tmp_path):
    """31 天前的会话被删，1 天前的保留，旧格式目录不动（AC19/AC20）。"""
    sessions_root = tmp_path / "sessions"
    old_name = (datetime.now() - timedelta(days=31)).strftime("%Y%m%d-%H%M%S") + "-dead"
    fresh_name = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d-%H%M%S") + "-live"
    old = sessions_root / old_name
    fresh = sessions_root / fresh_name
    legacy = sessions_root / "1717000000-abc12345"
    for path in (old, fresh, legacy):
        path.mkdir(parents=True)
        (path / JSONL_FILENAME).write_text("", encoding="utf-8")

    removed = clean_expired(str(sessions_root), timedelta(days=30))

    assert removed == 1
    assert not old.exists()
    assert fresh.exists()
    assert legacy.exists()  # 旧格式不清理，避免误删（N3）


def test_clean_expired_ignores_missing_root(tmp_path):
    """目录不存在时安静返回 0。"""
    assert clean_expired(str(tmp_path / "nope")) == 0


def test_clean_expired_skips_plain_files(tmp_path):
    """根目录下的普通文件不会被当成会话目录处理。"""
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    stray = sessions_root / "20200101-000000-old.md"
    stray.write_text("not a dir", encoding="utf-8")

    assert clean_expired(str(sessions_root)) == 0
    assert stray.exists()
