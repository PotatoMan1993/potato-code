"""Hook Loader 单测：合法加载、字段校验、加载错误跳过、双层合并（ch14 T9/F6-F8/N1）。"""

from __future__ import annotations

from pathlib import Path

from potatocode.hook import Event, load
from potatocode.hook.rule import ActionType, CombineMode


def _write_hooks(root: Path, yaml_text: str) -> Path:
    hooks_dir = root / ".potatocode"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_file = hooks_dir / "hooks.yaml"
    hook_file.write_text(yaml_text, encoding="utf-8")
    return hook_file


VALID_TWO_HOOKS = """
hooks:
  - name: block-write
    event: PreToolUse
    if:
      all_of:
        - field: tool_name
          match: { type: exact, value: write_file }
    action:
      type: shell
      command: "echo blocked >&2; exit 2"
  - name: zh-cn
    event: SessionStart
    action:
      type: prompt
      text: "用 zh-CN 回复"
"""


def test_load_valid_yaml_returns_engine(tmp_path):
    """合法 YAML：load 返回含全部规则的引擎，来源文件被记录（F6/F8）。"""
    hook_file = _write_hooks(tmp_path, VALID_TWO_HOOKS)
    engine = load(str(tmp_path))
    assert len(engine.rules) == 2
    assert engine.sources == [str(hook_file)]
    assert engine.rules[0].name == "block-write"
    assert engine.rules[0].event is Event.PRE_TOOL_USE
    assert engine.rules[0].action.type is ActionType.SHELL
    assert engine.rules[1].event is Event.SESSION_START
    assert engine.rules[1].condition is None  # 缺省 if 视为无条件（G3）


def test_load_missing_file_returns_empty_engine(tmp_path):
    """hooks.yaml 不存在：空引擎、不报错（F6/N9）。"""
    engine = load(str(tmp_path))
    assert engine.rules == []
    assert engine.sources == []


def test_load_invalid_yaml_skips_file(tmp_path, capsys):
    """YAML 解析失败：stderr 提示、整文件跳过、不抛异常（N9）。"""
    _write_hooks(tmp_path, "hooks: [unclosed\n")
    engine = load(str(tmp_path))
    assert engine.rules == []
    assert "hooks.yaml parse failed" in capsys.readouterr().err


def test_load_invalid_structure_skips_file(tmp_path, capsys):
    """顶层结构非法（缺 hooks: 数组）：stderr 提示、整文件跳过（N9）。"""
    _write_hooks(tmp_path, "not_hooks: []\n")
    engine = load(str(tmp_path))
    assert engine.rules == []
    assert "structure invalid" in capsys.readouterr().err


def test_load_missing_fields_skip_rule(tmp_path, capsys):
    """name 空 / event 未知 / action.type 无效：跳过该条、其余正常（F8/AC11）。"""
    _write_hooks(
        tmp_path,
        """
hooks:
  - name: ""
    event: SessionStart
    action: { type: prompt, text: x }
  - name: bad-event
    event: UnknownEvent
    action: { type: prompt, text: x }
  - name: bad-action
    event: SessionStart
    action: { type: teleport }
  - name: good
    event: Stop
    action: { type: prompt, text: ok }
""",
    )
    engine = load(str(tmp_path))
    captured = capsys.readouterr().err
    assert [r.name for r in engine.rules] == ["good"]
    assert 'unknown event "UnknownEvent"' in captured
    assert "unknown action type" in captured


def test_load_both_all_of_and_any_of_skipped(tmp_path, capsys):
    """`if` 同时含 all_of 与 any_of：报错跳过（F11/AC16）。"""
    _write_hooks(
        tmp_path,
        """
hooks:
  - name: mixed
    event: PreToolUse
    if:
      all_of:
        - field: tool_name
          match: { type: exact, value: write_file }
      any_of:
        - field: tool_name
          match: { type: exact, value: edit_file }
    action: { type: shell, command: "exit 0" }
""",
    )
    engine = load(str(tmp_path))
    assert engine.rules == []
    assert "cannot contain both" in capsys.readouterr().err


def test_load_async_with_blocking_event_skipped(tmp_path, capsys):
    """async + 拦截事件：加载期报错跳过（G8/F28/AC8）。"""
    _write_hooks(
        tmp_path,
        """
hooks:
  - name: bad-async
    event: PreToolUse
    async: true
    action: { type: shell, command: "echo x" }
  - name: good-hook
    event: SessionStart
    action: { type: shell, command: "echo ok" }
""",
    )
    engine = load(str(tmp_path))
    captured = capsys.readouterr().err
    assert [r.name for r in engine.rules] == ["good-hook"]
    assert 'hook "bad-async": async not allowed for blocking events' in captured


def test_load_invalid_regex_matcher_skipped(tmp_path, capsys):
    """条件里的非法正则：编译失败跳过该条（F14）。"""
    _write_hooks(
        tmp_path,
        """
hooks:
  - name: bad-regex
    event: UserPromptSubmit
    if:
      all_of:
        - field: prompt
          match: { type: regex, value: "[unclosed" }
    action: { type: shell, command: "exit 0" }
""",
    )
    engine = load(str(tmp_path))
    assert engine.rules == []
    assert "matcher compile failed" in capsys.readouterr().err


def test_load_invalid_timeout_skipped(tmp_path, capsys):
    """非法 timeout 串：报错跳过（F8）。"""
    _write_hooks(
        tmp_path,
        """
hooks:
  - name: bad-timeout
    event: Stop
    timeout: soon
    action: { type: prompt, text: x }
""",
    )
    engine = load(str(tmp_path))
    assert engine.rules == []
    assert "invalid timeout" in capsys.readouterr().err


def test_load_two_layers_merged_with_conflict_skip(tmp_path, monkeypatch, capsys):
    """两层叠加合并；同名冲突保留项目级、跳过用户级（F7/AC12）。"""
    _write_hooks(
        tmp_path,
        """
hooks:
  - name: project-hook
    event: Stop
    action: { type: prompt, text: from-project }
  - name: shared
    event: Stop
    action: { type: prompt, text: from-project }
""",
    )
    user_root = tmp_path / "home"
    user_root.mkdir()
    _write_hooks(user_root, """
hooks:
  - name: user-hook
    event: SessionStart
    action: { type: prompt, text: from-user }
  - name: shared
    event: SessionStart
    action: { type: prompt, text: from-user }
""")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_root))
    engine = load(str(tmp_path))
    names = [r.name for r in engine.rules]
    assert names == ["project-hook", "shared", "user-hook"]
    # shared 保留项目级的声明
    shared = next(r for r in engine.rules if r.name == "shared")
    assert shared.action.prompt is not None and shared.action.prompt.text == "from-project"
    assert "duplicate name across files" in capsys.readouterr().err


def test_load_condition_modes_and_not(tmp_path):
    """条件编译：any_of 模式与 not 匹配可正常构造（F12-F14）。"""
    _write_hooks(
        tmp_path,
        """
hooks:
  - name: any-not
    event: PreToolUse
    if:
      any_of:
        - field: tool_name
          match: { type: not, inner: { type: exact, value: read_file } }
        - field: tool_input.path
          match: { type: glob, value: "**/*.py" }
    action: { type: shell, command: "exit 0" }
""",
    )
    engine = load(str(tmp_path))
    assert len(engine.rules) == 1
    condition = engine.rules[0].condition
    assert condition is not None
    assert condition.mode is CombineMode.ANY_OF
    assert len(condition.atoms) == 2
