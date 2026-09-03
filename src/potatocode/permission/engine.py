"""权限引擎：前四层判定流水线（黑名单 → 沙箱 → 规则 → 模式兜底）与配置装配。

`check` 返回 Ask 作为「请走第五层人在回路」的信号；第五层由 agent 编排驱动。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..llm import ToolCall
from .blacklist import _BLACKLIST, hits_blacklist
from .rule import Rule, RuleSet, match_pattern
from .sandbox import relpath_for, resolve_root, sandbox_ok
from .settings import (
    Settings,
    SettingsError,
    categorize,
    extract_target,
    friendly_name,
    load_settings,
    to_rule_set,
)
from .types import Category, Decision, Mode, parse_mode

# 类别中文标签（模式兜底 Ask 的原因文案）
_CAT_LABEL = {
    Category.READ: "只读",
    Category.WRITE: "文件写",
    Category.EXEC: "命令执行",
}


def mode_fallback(mode: Mode, cat: Category) -> Decision:
    """模式兜底矩阵（F5）：只产 Allow 或 Ask，绝不产 Deny。"""
    if cat is Category.READ or mode is Mode.BYPASS:
        return Decision.ALLOW
    if mode is Mode.ACCEPT_EDITS and cat is Category.WRITE:
        return Decision.ALLOW
    return Decision.ASK


@dataclass
class Engine:
    """持有项目根、黑名单、三级规则集与启动默认模式（前四层 + 配置）。"""

    root: str  # 项目根（绝对、已解析符号链接）
    blacklist: list[re.Pattern[str]]  # 内置危险命令正则（不可配，N1）
    user: RuleSet  # 用户级
    project: RuleSet  # 项目级
    local: RuleSet  # 本地级
    local_path: str  # 永久放行的写入目标（本地层文件）
    _start_mode: Mode = field(default=Mode.DEFAULT)  # 启动默认模式

    def start_mode(self) -> Mode:
        """启动默认模式。"""
        return self._start_mode

    def persist_local_allow(self, call: ToolCall) -> None:
        """永久放行：生成精确 allow 规则写入本地层文件 + 同步内存（F8）。

        延迟导入避免模块环；异常向上抛，由 agent 捕获后只记日志不阻断。
        """
        from .persist import persist_local_allow

        persist_local_allow(self, call)

    def check(self, mode: Mode, call: ToolCall, read_only: bool) -> tuple[Decision, str]:
        """前四层判定（F6）：黑名单 → 沙箱 → 规则 → 模式兜底，任一层短路返回。

        返回 (裁决, 原因)；Allow 时原因恒为空串。
        """
        cat = categorize(call.name, read_only)
        friendly = friendly_name(call.name)
        target, is_file, ok = extract_target(call)

        # ① 黑名单（仅命令执行类，N1 最高优先，bypass 也拦）
        if cat is Category.EXEC and target != "" and hits_blacklist(target):
            return Decision.DENY, f"命中危险命令黑名单：{target}"

        # ② 沙箱（仅文件类，N2）
        if is_file:
            if not ok:
                return Decision.DENY, "无法解析文件路径参数，安全拒绝"
            if not sandbox_ok(self, target):
                return Decision.DENY, f"路径在项目目录之外：{target}"

        # ③ 规则引擎（三级合并为同一集合，deny 压过 allow、不做层级覆盖，F4/AC5）
        match_target = relpath_for(self.root, target) if is_file else target
        decision, rule = self._match_rules(friendly, match_target)
        if rule is not None:
            if decision is Decision.DENY:
                return Decision.DENY, _deny_rule_reason(rule)
            return Decision.ALLOW, ""

        # ④ 模式兜底（只产 Allow/Ask）
        decision = mode_fallback(mode, cat)
        if decision is Decision.ASK:
            return Decision.ASK, f"{mode} 模式下 {_CAT_LABEL[cat]} 类操作需确认"
        return Decision.ALLOW, ""

    def _match_rules(self, friendly: str, target: str) -> tuple[Decision, Rule | None]:
        """跨三层匹配规则（F4/AC5）：deny 全层优先、allow 随后，层级先后不影响结果。"""
        for ruleset in (self.local, self.project, self.user):
            for rule in ruleset.deny:
                if match_pattern(rule.tool, friendly, path=False) and match_pattern(
                    rule.pattern, target
                ):
                    return Decision.DENY, rule
        for ruleset in (self.local, self.project, self.user):
            for rule in ruleset.allow:
                if match_pattern(rule.tool, friendly, path=False) and match_pattern(
                    rule.pattern, target
                ):
                    return Decision.ALLOW, rule
        return Decision.ALLOW, None


def _deny_rule_reason(rule) -> str:
    """deny 规则命中的原因文案（含 Tool(pattern) 供模型/用户阅读）。"""
    if rule.pattern:
        return f"匹配 deny 规则：{rule.tool}({rule.pattern})"
    return f"匹配 deny 规则：{rule.tool}"


def _load(path: str) -> Settings:
    """加载单个配置文件；缺失/非法一律降级为空 Settings（N5）。"""
    try:
        return load_settings(path)
    except SettingsError:
        return Settings()


def _resolve_start_mode(local: Settings, project: Settings, user: Settings) -> Mode:
    """启动默认模式按 本地 > 项目 > 用户 取首个可识别的 default_mode，皆无则 default。"""
    for s in (local, project, user):
        m, ok = parse_mode(s.default_mode)
        if ok:
            return m
    return Mode.DEFAULT


def new_engine(root: str, *, user_settings: str | None = None) -> tuple[Engine, Exception | None]:
    """构造权限引擎（F4）：解析项目根、加载三层配置、编译黑名单、确定启动模式。

    唯一可能返回非 None err 的情形是 resolve_root 失败；此时仍返回非 None 的
    「空规则安全引擎」（root 退化为传入值、规则空、default 模式）+ err。
    `user_settings` 仅测试注入用户级配置文件路径用，默认取 `~/.potatocode/settings.yaml`。
    """
    err: Exception | None = None
    try:
        resolved = resolve_root(root)
    except Exception as e:  # noqa: BLE001 - 根解析失败降级为传入值，check 不抛
        resolved = root
        err = e

    user_path = user_settings or str(Path.home() / ".potatocode" / "settings.yaml")
    project_path = str(Path(resolved) / ".potatocode" / "settings.yaml")
    local_path = str(Path(resolved) / ".potatocode" / "settings.local.yaml")

    user_s = _load(user_path)
    project_s = _load(project_path)
    local_s = _load(local_path)

    engine = Engine(
        root=resolved,
        blacklist=list(_BLACKLIST),
        user=to_rule_set(user_s),
        project=to_rule_set(project_s),
        local=to_rule_set(local_s),
        local_path=local_path,
        _start_mode=_resolve_start_mode(local_s, project_s, user_s),
    )
    return engine, err


__all__ = ["Engine", "mode_fallback", "new_engine"]
