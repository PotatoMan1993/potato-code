"""引擎单测：五层流水线短路、模式矩阵、规则合并、配置降级、安全默认（AC5–AC8/AC15/AC18）。"""

from __future__ import annotations

import json

from potatocode.llm import ToolCall
from potatocode.permission import Mode, new_engine
from potatocode.permission.engine import mode_fallback
from potatocode.permission.types import Category, Decision


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(id="x", name=name, input=json.dumps(args))


def _write(path, content: str) -> None:
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _project_settings(root, content: str) -> None:
    _write(f"{root}/.potatocode/settings.yaml", content)


def _local_settings(root, content: str) -> None:
    _write(f"{root}/.potatocode/settings.local.yaml", content)


# ────────── 黑名单（AC1/F1/N1） ──────────


def test_blacklist_hard_block_bypass(tmp_path):
    """黑名单在 bypass 模式下依然拦截（N1）。"""
    eng, _ = new_engine(str(tmp_path))
    decision, reason = eng.check(Mode.BYPASS, _call("bash", {"command": "rm -rf /"}), False)
    assert decision is Decision.DENY
    assert "危险命令黑名单" in reason


def test_blacklist_only_for_exec(tmp_path):
    """非命令执行工具不受黑名单影响（跳层放行，AC8）。"""
    eng, _ = new_engine(str(tmp_path))
    # read_file 带危险命令字符串当 path 也不会走黑名单（它是文件类，走沙箱）
    decision, _ = eng.check(Mode.DEFAULT, _call("read_file", {"path": "rm -rf /"}), True)
    # 沙箱内路径不存在但祖先回退放行；只读默认 Allow
    assert decision is Decision.ALLOW


# ────────── 沙箱（AC2/F2） ──────────


def test_sandbox_denies_outside(tmp_path):
    eng, _ = new_engine(str(tmp_path))
    decision, reason = eng.check(Mode.DEFAULT, _call("read_file", {"path": "/etc/passwd"}), True)
    assert decision is Decision.DENY
    assert "路径在项目目录之外" in reason


def test_sandbox_allows_inside(tmp_path):
    eng, _ = new_engine(str(tmp_path))
    decision, _ = eng.check(Mode.DEFAULT, _call("read_file", {"path": "pyproject.toml"}), True)
    assert decision is Decision.ALLOW


def test_file_unparseable_params_denied(tmp_path):
    """文件类 JSON 无法解析 → 安全拒绝（AC15/N7）。"""
    eng, _ = new_engine(str(tmp_path))
    call = ToolCall(id="x", name="read_file", input="not json")
    decision, reason = eng.check(Mode.DEFAULT, call, True)
    assert decision is Decision.DENY
    assert "无法解析" in reason


# ────────── 规则（AC3/AC4/F3） ──────────


def test_rule_exact_and_glob(tmp_path):
    _project_settings(tmp_path, 'permissions:\n  allow: ["Bash(git status)", "Bash(git *)"]\n')
    eng, _ = new_engine(str(tmp_path))
    # 精确放行 git status
    d, _ = eng.check(Mode.DEFAULT, _call("bash", {"command": "git status"}), False)
    assert d is Decision.ALLOW
    # git * 放行所有 git 子命令
    d, _ = eng.check(Mode.DEFAULT, _call("bash", {"command": "git push"}), False)
    assert d is Decision.ALLOW
    # 未命中 → 模式兜底 Ask（default + Exec）
    d, _ = eng.check(Mode.DEFAULT, _call("bash", {"command": "npm install"}), False)
    assert d is Decision.ASK


def test_deny_rule_blocks(tmp_path):
    _project_settings(tmp_path, 'permissions:\n  deny: ["Bash(git push)"]\n')
    eng, _ = new_engine(str(tmp_path))
    d, reason = eng.check(Mode.BYPASS, _call("bash", {"command": "git push"}), False)
    assert d is Decision.DENY
    assert "deny 规则" in reason


def test_deny_rule_overrides_bypass_allow(tmp_path):
    """deny 规则命中在 bypass 模式下仍 Deny（规则层先于模式兜底，AC8）。"""
    _project_settings(tmp_path, 'permissions:\n  deny: ["Bash(git push)"]\n')
    eng, _ = new_engine(str(tmp_path))
    d, _ = eng.check(Mode.BYPASS, _call("bash", {"command": "git push"}), False)
    assert d is Decision.DENY


def test_friendly_name_routing(tmp_path):
    """友好名正确路由到内置工具（AC4）。"""
    _project_settings(
        tmp_path,
        'permissions:\n  allow: ["Write(src/**)", "Read(.env)", "Grep(*)"]\n',
    )
    eng, _ = new_engine(str(tmp_path))
    assert (
        eng.check(Mode.DEFAULT, _call("write_file", {"path": "src/a.py", "content": "x"}), False)[0]
        is Decision.ALLOW
    )
    assert eng.check(Mode.DEFAULT, _call("read_file", {"path": ".env"}), True)[0] is Decision.ALLOW
    assert eng.check(Mode.DEFAULT, _call("grep", {"path": "."}), True)[0] is Decision.ALLOW


def test_write_glob_not_match_outside(tmp_path):
    _project_settings(tmp_path, 'permissions:\n  allow: ["Write(src/**)"]\n')
    eng, _ = new_engine(str(tmp_path))
    # docs/x 未命中 allow → default 下文件写 → Ask
    d, _ = eng.check(Mode.DEFAULT, _call("write_file", {"path": "docs/x", "content": "x"}), False)
    assert d is Decision.ASK


# ────────── 规则合并（AC5/F4） ──────────


def test_rule_merge_deny_wins_cross_layer(tmp_path):
    """跨层冲突：本地 allow、项目 deny → deny 胜（层级不参与裁决，AC5/F4）。"""
    _project_settings(tmp_path, 'permissions:\n  deny: ["Bash(git push)"]\n')
    _local_settings(tmp_path, 'permissions:\n  allow: ["Bash(git push)"]\n')
    eng, _ = new_engine(str(tmp_path))
    # deny 跨层压过 allow，层级先后不影响结果
    d, _ = eng.check(Mode.DEFAULT, _call("bash", {"command": "git push"}), False)
    assert d is Decision.DENY


def test_rule_merge_same_layer_deny_priority(tmp_path):
    """同层 deny 优先 allow。"""
    _project_settings(
        tmp_path, 'permissions:\n  allow: ["Bash(git *)"]\n  deny: ["Bash(git push)"]\n'
    )
    eng, _ = new_engine(str(tmp_path))
    d, _ = eng.check(Mode.DEFAULT, _call("bash", {"command": "git push"}), False)
    assert d is Decision.DENY
    d, _ = eng.check(Mode.DEFAULT, _call("bash", {"command": "git status"}), False)
    assert d is Decision.ALLOW


# ────────── 配置降级（AC6/N5） ──────────


def test_missing_configs_ok(tmp_path):
    """三层文件缺失 → 空规则引擎正常构造。"""
    eng, err = new_engine(str(tmp_path))
    assert err is None
    assert eng.start_mode() is Mode.DEFAULT


def test_invalid_config_degrades(tmp_path):
    """某层格式非法 → 跳过该层、其余正常、不抛（N5）。"""
    _project_settings(tmp_path, "permissions: [unclosed\n")
    _local_settings(tmp_path, 'permissions:\n  allow: ["Bash(pytest)"]\n')
    eng, err = new_engine(str(tmp_path))
    assert err is None  # 非法配置不致命
    # local 层的 allow 仍生效
    d, _ = eng.check(Mode.DEFAULT, _call("bash", {"command": "pytest"}), False)
    assert d is Decision.ALLOW


def test_resolve_root_failure_returns_engine(tmp_path):
    """项目根不可解析 → 仍返回非 None 引擎 + err。"""
    eng, err = new_engine(str(tmp_path / "no" / "such" / "dir"))
    assert eng is not None
    assert err is not None


# ────────── 模式矩阵（AC7/F5） ──────────


def test_mode_fallback_matrix():
    """逐档逐类断言兜底裁决（只产 Allow/Ask）。"""
    # default
    assert mode_fallback(Mode.DEFAULT, Category.READ) is Decision.ALLOW
    assert mode_fallback(Mode.DEFAULT, Category.WRITE) is Decision.ASK
    assert mode_fallback(Mode.DEFAULT, Category.EXEC) is Decision.ASK
    # acceptEdits
    assert mode_fallback(Mode.ACCEPT_EDITS, Category.READ) is Decision.ALLOW
    assert mode_fallback(Mode.ACCEPT_EDITS, Category.WRITE) is Decision.ALLOW
    assert mode_fallback(Mode.ACCEPT_EDITS, Category.EXEC) is Decision.ASK
    # plan（矩阵同 default 作防御兜底）
    assert mode_fallback(Mode.PLAN, Category.READ) is Decision.ALLOW
    assert mode_fallback(Mode.PLAN, Category.WRITE) is Decision.ASK
    assert mode_fallback(Mode.PLAN, Category.EXEC) is Decision.ASK
    # bypass
    assert mode_fallback(Mode.BYPASS, Category.READ) is Decision.ALLOW
    assert mode_fallback(Mode.BYPASS, Category.WRITE) is Decision.ALLOW
    assert mode_fallback(Mode.BYPASS, Category.EXEC) is Decision.ALLOW


def test_check_matrix(tmp_path):
    """端到端逐档断言（write_file / bash / read_file）。"""
    eng, _ = new_engine(str(tmp_path))
    write = _call("write_file", {"path": "a.txt", "content": "x"})
    bash = _call("bash", {"command": "git status"})
    read = _call("read_file", {"path": "a.txt"})

    assert eng.check(Mode.DEFAULT, read, True)[0] is Decision.ALLOW
    assert eng.check(Mode.DEFAULT, write, False)[0] is Decision.ASK
    assert eng.check(Mode.DEFAULT, bash, False)[0] is Decision.ASK

    assert eng.check(Mode.ACCEPT_EDITS, write, False)[0] is Decision.ALLOW
    assert eng.check(Mode.ACCEPT_EDITS, bash, False)[0] is Decision.ASK

    assert eng.check(Mode.BYPASS, write, False)[0] is Decision.ALLOW
    assert eng.check(Mode.BYPASS, bash, False)[0] is Decision.ALLOW


# ────────── 安全默认（AC15/N7） ──────────


def test_unknown_tool_not_silently_allowed(tmp_path):
    """未注册工具名归命令执行类 → 触发 Ask 而非直接 Allow。"""
    eng, _ = new_engine(str(tmp_path))
    d, _ = eng.check(Mode.DEFAULT, _call("mystery_tool", {}), False)
    assert d is Decision.ASK


def test_bypass_unknown_tool_allow(tmp_path):
    """bypass 下未知工具（无副作用可判定）走 Exec → Allow，但不被黑名单/沙箱拦。"""
    eng, _ = new_engine(str(tmp_path))
    d, _ = eng.check(Mode.BYPASS, _call("mystery_tool", {}), False)
    assert d is Decision.ALLOW


# ────────── 启动默认模式（AC18/F4） ──────────


def test_start_mode_priority(tmp_path):
    """defaultMode 按 本地 > 项目 > 用户 生效。"""
    user = tmp_path / "user_settings.yaml"
    _write(str(user), "default_mode: plan\n")
    _project_settings(tmp_path, "default_mode: acceptEdits\n")
    _local_settings(tmp_path, "default_mode: bypassPermissions\n")
    eng, _ = new_engine(str(tmp_path), user_settings=str(user))
    assert eng.start_mode() is Mode.BYPASS  # local 优先


def test_start_mode_project_when_no_local(tmp_path):
    user = tmp_path / "user_settings.yaml"
    _write(str(user), "default_mode: plan\n")
    _project_settings(tmp_path, "default_mode: acceptEdits\n")
    eng, _ = new_engine(str(tmp_path), user_settings=str(user))
    assert eng.start_mode() is Mode.ACCEPT_EDITS


def test_start_mode_default(tmp_path):
    eng, _ = new_engine(str(tmp_path))
    assert eng.start_mode() is Mode.DEFAULT
