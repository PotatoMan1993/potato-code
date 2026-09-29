"""记忆子包单测：笔记 CRUD、索引合并与截断、异步更新的解析与容错（ch11 第 4 层）。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

from potatocode.llm import Message, Request, StreamEvent
from potatocode.memory import (
    LEVEL_PROJECT,
    LEVEL_USER,
    Manager,
    NoteType,
    Store,
    UpdateAction,
    parse_actions,
    project_memory_dir,
    user_memory_dir,
)
from potatocode.memory.const import INDEX_FILENAME, MAX_INDEX_BYTES, TRUNCATED_MARK
from potatocode.memory.store import parse_note_file


class FakeMemoryProvider:
    """脚本化 provider：返回预设文本或异常，并记录每次请求。"""

    def __init__(self, response: str | Exception = "[]") -> None:
        self._response = response
        self.requests: list[Request] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    async def stream(self, req: Request) -> AsyncIterator[StreamEvent]:
        self.requests.append(req)
        if isinstance(self._response, Exception):
            yield StreamEvent(err=self._response)
            return
        yield StreamEvent(text=self._response)
        yield StreamEvent(done=True)


def _action(action: str, level: str, **kwargs) -> UpdateAction:
    return UpdateAction(action=action, level=level, **kwargs)


# ────────── 笔记文件 CRUD（F28/F31/F40） ──────────


def test_store_create_note(tmp_path):
    """create：文件落地、frontmatter 正确、索引追加对应行（AC21/AC22）。"""
    store = Store(str(tmp_path / "memory"))
    store.apply(
        [
            _action(
                "create",
                LEVEL_PROJECT,
                type=NoteType.PROJECT_KNOWLEDGE.value,
                title="构建方式",
                slug="build_setup",
                content="用 uv 管理依赖，测试用 pytest。",
            )
        ]
    )

    path = tmp_path / "memory" / "project_knowledge_build_setup.md"
    assert path.exists()
    meta, body = parse_note_file(path.read_text(encoding="utf-8"))
    assert meta["type"] == "project_knowledge"
    assert meta["title"] == "构建方式"
    assert meta["created"] == meta["updated"]
    assert "pytest" in body

    index = (tmp_path / "memory" / INDEX_FILENAME).read_text(encoding="utf-8")
    assert "[project_knowledge]" in index
    assert "构建方式" in index
    assert "project_knowledge_build_setup.md" in index


def test_store_update_note_keeps_created_refreshes_updated(tmp_path):
    """update：正文更新、created 保留、updated 刷新、索引行同步（AC：笔记更新）。"""
    store = Store(str(tmp_path / "memory"))
    store.apply(
        [
            _action(
                "create",
                LEVEL_USER,
                type=NoteType.USER_PREFERENCE.value,
                title="回复风格",
                slug="style",
                content="偏好简洁回复。",
            )
        ]
    )
    path = tmp_path / "memory" / "user_preference_style.md"
    created = parse_note_file(path.read_text(encoding="utf-8"))[0]["created"]

    store.apply(
        [
            _action(
                "update",
                LEVEL_USER,
                filename="user_preference_style.md",
                title="回复风格",
                content="偏好简洁回复，不要结尾摘要。",
            )
        ]
    )

    meta, body = parse_note_file(path.read_text(encoding="utf-8"))
    assert "不要结尾摘要" in body
    assert meta["created"] == created
    assert meta["updated"] >= created

    index = (tmp_path / "memory" / INDEX_FILENAME).read_text(encoding="utf-8")
    assert "不要结尾摘要" in index
    assert index.count("user_preference_style.md") == 1  # 只更新不新增行


def test_store_delete_note(tmp_path):
    """delete：文件删除、索引行移除（AC：笔记删除）。"""
    store = Store(str(tmp_path / "memory"))
    store.apply(
        [
            _action(
                "create",
                LEVEL_PROJECT,
                type=NoteType.REFERENCE_MATERIAL.value,
                title="旧文档",
                slug="old_doc",
                content="已经过时。",
            )
        ]
    )
    path = tmp_path / "memory" / "reference_material_old_doc.md"
    assert path.exists()

    store.apply([_action("delete", LEVEL_PROJECT, filename="reference_material_old_doc.md")])

    assert not path.exists()
    index = (tmp_path / "memory" / INDEX_FILENAME).read_text(encoding="utf-8")
    assert "old_doc" not in index


def test_store_delete_missing_file_is_idempotent(tmp_path):
    """delete 一个不存在的文件不抛异常（幂等）。"""
    store = Store(str(tmp_path / "memory"))
    store.apply([_action("delete", LEVEL_PROJECT, filename="nope.md")])
    assert store.load_index() == ""


def test_store_sanitizes_filename(tmp_path):
    """文件名里的路径片段被剥掉，不会写到 memory 目录之外。"""
    store = Store(str(tmp_path / "memory"))
    store.apply(
        [
            _action(
                "create",
                LEVEL_PROJECT,
                type=NoteType.PROJECT_KNOWLEDGE.value,
                title="穿越",
                slug="escape",
                filename="../../escaped.md",
                content="内容",
            )
        ]
    )

    assert not (tmp_path / "escaped.md").exists()
    assert (tmp_path / "memory" / "escaped.md").exists()


def test_store_load_index_missing(tmp_path):
    """索引文件不存在时返回空字符串（N3）。"""
    assert Store(str(tmp_path / "nope")).load_index() == ""


def test_store_lists_notes(tmp_path):
    """notes 列出已落地的笔记，跳过索引文件本身。"""
    store = Store(str(tmp_path / "memory"))
    store.apply(
        [
            _action(
                "create",
                LEVEL_PROJECT,
                type=NoteType.PROJECT_KNOWLEDGE.value,
                title="一",
                slug="one",
                content="内容一",
            )
        ]
    )
    notes = store.notes()
    assert [n.filename for n in notes] == ["project_knowledge_one.md"]
    assert notes[0].type is NoteType.PROJECT_KNOWLEDGE


# ────────── 索引加载与截断（F32/F34） ──────────


def test_manager_load_index_merges_project_first(tmp_path):
    """两级索引合并：项目级在前、用户级在后（F32）。"""
    project = tmp_path / "proj-memory"
    user = tmp_path / "user-memory"
    project.mkdir()
    user.mkdir()
    (project / INDEX_FILENAME).write_text("- [project_knowledge] 项目条目\n", encoding="utf-8")
    (user / INDEX_FILENAME).write_text("- [user_preference] 用户条目\n", encoding="utf-8")

    text = Manager(str(project), str(user)).load_index()

    assert text.index("项目条目") < text.index("用户条目")


def test_manager_load_index_empty(tmp_path):
    """两级都没有索引时返回空字符串（干净项目不受影响，N3）。"""
    assert Manager(str(tmp_path / "a"), str(tmp_path / "b")).load_index() == ""


def test_manager_load_index_truncates(tmp_path):
    """超 25KB 的索引被截断并追加标注（AC26/F34）。"""
    project = tmp_path / "proj-memory"
    user = tmp_path / "user-memory"
    project.mkdir()
    user.mkdir()
    (project / INDEX_FILENAME).write_text("x" * (MAX_INDEX_BYTES + 5000), encoding="utf-8")

    text = Manager(str(project), str(user)).load_index()

    assert text.endswith(TRUNCATED_MARK)
    # 截断部分不超过 25KB（多出的 1 字节是标注前用来分行的换行符）
    assert len(text[: -len(TRUNCATED_MARK)].encode("utf-8")) <= MAX_INDEX_BYTES + 1


# ────────── 异步更新（F37/F38/F42） ──────────


async def _update(mgr: Manager, text: str = "你好") -> None:
    await mgr.update_async([Message(role="user", content=text)])


async def test_update_async_creates_notes(tmp_path):
    """模型返回 create 操作 → 笔记文件与索引行落地（AC21/AC22）。"""
    response = json.dumps(
        [
            {
                "action": "create",
                "level": "project",
                "type": "project_knowledge",
                "title": "中文回复",
                "slug": "chinese",
                "content": "本项目统一用中文回复。",
            }
        ],
        ensure_ascii=False,
    )
    provider = FakeMemoryProvider(response)
    mgr = Manager(str(tmp_path / "pm"), str(tmp_path / "um"), provider, "fake-model")

    await _update(mgr)

    assert (tmp_path / "pm" / "project_knowledge_chinese.md").exists()
    assert "中文回复" in (tmp_path / "pm" / INDEX_FILENAME).read_text(encoding="utf-8")


async def test_update_async_routes_levels(tmp_path):
    """按 level 字段分发到项目级 / 用户级两个目录（F29）。"""
    response = json.dumps(
        [
            {
                "action": "create",
                "level": "project",
                "type": "project_knowledge",
                "title": "项目知识",
                "slug": "proj",
                "content": "项目相关内容",
            },
            {
                "action": "create",
                "level": "user",
                "type": "user_preference",
                "title": "用户偏好",
                "slug": "pref",
                "content": "跨项目相关内容",
            },
        ],
        ensure_ascii=False,
    )
    mgr = Manager(str(tmp_path / "pm"), str(tmp_path / "um"), FakeMemoryProvider(response), "m")

    await _update(mgr)

    assert (tmp_path / "pm" / "project_knowledge_proj.md").exists()
    assert (tmp_path / "um" / "user_preference_pref.md").exists()


async def test_update_async_sends_no_tools(tmp_path):
    """记忆更新请求不带工具定义（F38/AC：无工具请求）。"""
    provider = FakeMemoryProvider("[]")
    mgr = Manager(str(tmp_path / "pm"), str(tmp_path / "um"), provider, "m")

    await _update(mgr)

    assert provider.requests[0].tools is None


async def test_update_async_requests_include_existing_index(tmp_path):
    """请求正文包含现有索引，供模型判断去重（F41）。"""
    project = tmp_path / "pm"
    project.mkdir()
    (project / INDEX_FILENAME).write_text("- [project_knowledge] 已有条目\n", encoding="utf-8")
    provider = FakeMemoryProvider("[]")
    mgr = Manager(str(project), str(tmp_path / "um"), provider, "m")

    await _update(mgr, "记住这件事")

    body = provider.requests[0].messages[0].content
    assert "已有条目" in body
    assert "记住这件事" in body


async def test_update_async_silent_on_provider_error(tmp_path):
    """provider 报错时静默记日志，不上抛、不写文件（AC25/F42）。"""
    mgr = Manager(
        str(tmp_path / "pm"), str(tmp_path / "um"), FakeMemoryProvider(RuntimeError("boom")), "m"
    )

    await _update(mgr)  # 不抛异常

    assert not (tmp_path / "pm").exists()


async def test_update_async_silent_on_bad_json(tmp_path):
    """返回不是 JSON 时静默跳过，不写任何文件（F42）。"""
    mgr = Manager(str(tmp_path / "pm"), str(tmp_path / "um"), FakeMemoryProvider("我不太确定"), "m")

    await _update(mgr)

    assert not (tmp_path / "pm").exists()


async def test_update_async_skipped_without_provider(tmp_path):
    """未选定 provider 时更新直接跳过（启动早期场景）。"""
    mgr = Manager(str(tmp_path / "pm"), str(tmp_path / "um"))

    await _update(mgr)

    assert not (tmp_path / "pm").exists()


async def test_update_async_empty_array(tmp_path):
    """返回空数组表示无需更新（F39）。"""
    mgr = Manager(str(tmp_path / "pm"), str(tmp_path / "um"), FakeMemoryProvider("[]"), "m")

    await _update(mgr)

    assert not (tmp_path / "pm").exists()


async def test_set_provider_enables_update(tmp_path):
    """set_provider 之后才具备更新能力（启动时 provider 未选定）。"""
    mgr = Manager(str(tmp_path / "pm"), str(tmp_path / "um"))
    assert mgr.provider is None

    mgr.set_provider(FakeMemoryProvider("[]"), "m")

    assert mgr.provider is not None
    assert mgr.model == "m"


# ────────── 解析（F39/F42） ──────────


def test_parse_actions_accepts_fenced_json():
    """模型套了 Markdown 代码块围栏时也能解析。"""
    text = '```json\n[{"action":"delete","level":"project","filename":"a.md"}]\n```'

    actions = parse_actions(text)

    assert len(actions) == 1
    assert actions[0].action == "delete"


def test_parse_actions_skips_invalid_entries():
    """非法 action / level 的条目被丢弃，合法条目保留。"""
    text = json.dumps(
        [
            {"action": "create", "level": "project", "type": "project_knowledge", "title": "好"},
            {"action": "explode", "level": "project"},
            {"action": "create", "level": "galaxy"},
            "不是字典",
        ],
        ensure_ascii=False,
    )

    actions = parse_actions(text)

    assert len(actions) == 1
    assert actions[0].title == "好"


def test_parse_actions_rejects_non_array():
    """返回 JSON 对象而不是数组时视为无效（F42）。"""
    assert parse_actions('{"action": "create"}') == []
    assert parse_actions("") == []


# ────────── 路径约定（F29） ──────────


def test_memory_dirs():
    """项目级在 .potatocode/memory，用户级在 ~/.potatocode/memory。"""
    proj = project_memory_dir("/tmp/proj").replace("\\", "/")
    assert proj.endswith("/tmp/proj/.potatocode/memory")
    assert user_memory_dir("/tmp/home").replace("\\", "/").endswith("/tmp/home/.potatocode/memory")
    assert Path(user_memory_dir("/tmp/home")).name == "memory"
