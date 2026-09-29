"""prompt 包单测：模块化装配、环境采集、补充消息（ch05）。"""

from __future__ import annotations

import sys

from potatocode.prompt import (
    Environment,
    assemble_system,
    build_system_prompt,
    fixed_modules,
    gather_environment,
    optional_modules,
    plan_reminder,
    system_reminder,
)
from potatocode.prompt.modules import Module

# ────────── 模块化装配（AC1/AC2/F1） ──────────


def test_fixed_modules_ordered_by_priority():
    """七个固定模块按 priority 10..70 排列、内容非空。"""
    mods = fixed_modules()
    assert [m.priority for m in mods] == [10, 20, 30, 40, 50, 60, 70]
    assert [m.name for m in mods] == [
        "身份",
        "系统约束",
        "任务模式",
        "动作执行",
        "工具使用",
        "语气风格",
        "文本输出",
    ]
    assert all(m.content for m in mods)


def test_optional_modules_are_empty_slots():
    """三个可选空槽 priority 80..100、content 为空（AC2）。"""
    mods = optional_modules()
    assert [m.priority for m in mods] == [80, 90, 100]
    assert all(m.content == "" for m in mods)


def test_assemble_system_order_and_blank_lines():
    """按优先级排序、以空行分隔（AC1）。"""
    s = build_system_prompt()
    # 身份段在工具使用段之前
    assert s.index("你是 PotatoCode") < s.index("优先使用专用工具")
    # 模块间以空行分隔，但无连续多空行
    assert "\n\n" in s
    assert "\n\n\n" not in s


def test_assemble_skips_empty_modules():
    """空 content 模块被跳过、不产生多余空行（AC2）。"""
    only_fixed = assemble_system(fixed_modules())
    assert only_fixed == build_system_prompt()  # 三空槽不改变结果
    # 空模块直接传也不出现
    assert assemble_system([Module(name="空", priority=5, content="")]) == ""


def test_mount_new_module_by_priority_only():
    """挂载即扩展：新增模块只需出现在列表，装配自动按 priority 落位（AC1）。"""
    extra = Module(name="扩展", priority=55, content="EXTRA_MODULE")
    combined = assemble_system(fixed_modules() + optional_modules() + [extra])
    # 落在工具使用(50)之后、语气风格(60)之前
    i_extra = combined.index("EXTRA_MODULE")
    assert combined.index("old_string") < i_extra < combined.index("简洁、直接")


# ────────── 缓存确定性（AC5/F3/N1） ──────────


def test_stable_prompt_byte_deterministic():
    """连续两次构造稳定系统提示逐字节相等（N1）。"""
    assert build_system_prompt() == build_system_prompt()


def test_stable_prompt_excludes_environment():
    """稳定块不含随环境/时间变化的成分（cwd/date/git）（N1）。"""
    s = build_system_prompt()
    for token in ("Working directory", "Date:", "Git status", "Platform:"):
        assert token not in s


# ────────── 双重强化（AC7/F5） ──────────


def test_system_prompt_reinforces_key_conventions():
    """系统提示含「优先用专用工具」与「编辑前必先读」（AC7）。"""
    s = build_system_prompt()
    assert "优先" in s
    for tool in ("read_file", "glob", "grep"):
        assert tool in s
    assert "编辑" in s and "读取" in s
    assert "bash" in s  # 明确点名 bash 拼凑


# ────────── 环境采集（AC3/F2/N4） ──────────


def test_environment_render_contains_core_fields():
    """render 含工作目录、平台、日期、版本、模型（AC3）。"""
    env = gather_environment(version="9.9.9", model="fake-model")
    text = env.render()
    assert "Working directory:" in text
    assert "Platform:" in text
    assert "Date:" in text
    assert "Version: 9.9.9" in text
    assert "Model: fake-model" in text


def test_environment_drops_empty_fields():
    """render 省略空值项。"""
    env = Environment()  # 全空
    assert env.render() == ""
    env.version = "1.0"
    assert env.render() == "Version: 1.0"


def test_gather_environment_degrades_in_non_git_dir(monkeypatch, tmp_path):
    """非 git 目录下 git_status 降级为空、不抛异常（AC13/N4）。"""
    monkeypatch.chdir(tmp_path)  # 临时目录非 git 仓库
    env = gather_environment(version="0", model="m")
    assert env.git_status == ""
    assert env.working_dir == str(tmp_path)
    assert env.platform == sys.platform


def test_gather_environment_in_git_repo():
    """git 仓库下能取到状态摘要（干净或 N 个文件改动）。"""
    env = gather_environment(version="0", model="m")
    assert env.git_status in ("干净",) or env.git_status.endswith("个文件改动")


# ────────── 补充消息与规划提醒（AC8/F6/F7） ──────────


def test_system_reminder_wraps_in_tag():
    """补充消息以 <system-reminder> 标签包裹（AC8）。"""
    out = system_reminder("请勿回复本条")
    assert out.startswith("<system-reminder>")
    assert out.endswith("</system-reminder>")
    assert "请勿回复本条" in out


def test_plan_reminder_full_and_concise():
    """plan_reminder 完整版含只读工具/计划/等待批准；精简版一句（F7）。"""
    full = plan_reminder(True)
    assert "<system-reminder>" in full
    assert "PLAN MODE" in full
    assert "read-only tools" in full
    assert "/do" in full

    concise = plan_reminder(False)
    assert "<system-reminder>" in concise
    assert "PLAN MODE" in concise
    assert len(concise) < len(full)


# ────────── 指令与记忆注入（ch11 F43/AC27） ──────────


def test_optional_modules_accept_injected_content():
    """可选槽按参数填充内容：空参数仍是空槽（F43）。"""
    mods = optional_modules(instructions="项目规范", memory="记忆索引")
    by_priority = {m.priority: m.content for m in mods}
    assert by_priority[80] == "项目规范"
    assert by_priority[90] == ""
    assert by_priority[100] == "记忆索引"


def test_build_system_prompt_injects_instructions_and_memory():
    """非空参数分别落到自定义指令与长期记忆槽位，且排在固定模块之后（AC27）。"""
    s = build_system_prompt("项目规范：代码块必须带语言标记", "- [user_preference] 简洁回复")

    assert "项目规范：代码块必须带语言标记" in s
    assert "- [user_preference] 简洁回复" in s
    # 固定模块（10..70）在前，自定义指令（80）在长期记忆（100）之前
    assert s.index("你是 PotatoCode") < s.index("项目规范")
    assert s.index("项目规范：代码块必须带语言标记") < s.index("[user_preference]")


def test_build_system_prompt_empty_args_match_previous_output():
    """传空字符串时与旧版无参调用逐字节一致（AC27 向后兼容）。"""
    assert build_system_prompt("", "") == assemble_system(fixed_modules() + optional_modules())


def test_build_system_prompt_skips_empty_slot():
    """只给记忆不给指令时，指令槽位被跳过、不留空行。"""
    only_memory = build_system_prompt("", "记忆内容")

    assert "记忆内容" in only_memory
    assert "\n\n\n" not in only_memory
    assert build_system_prompt("", "") != only_memory
