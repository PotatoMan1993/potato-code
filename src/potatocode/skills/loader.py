"""Skill 加载器：两级搜索路径扫描、热重载与来源标识（ch13 F2/F3/F4/F17/F20）。

搜索优先级：项目级 `.potatocode/skills/` > 用户级 `~/.potatocode/skills/`；
同 name 首次出现者占位（扫描顺序保证项目级优先），后续同名跳过。
单条解析失败只记 warning 并跳过，不阻断其他 skill 加载（N1）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .parser import SkillDef, SkillParseError, parse_skill_file

logger = logging.getLogger(__name__)

# 项目级 skill 目录（相对工作目录）
PROJECT_SKILLS_DIR = ".potatocode/skills"
# 用户级 skill 目录（相对用户主目录）
USER_SKILLS_DIR = "~/.potatocode/skills"

# 目录型 skill 的入口文件名
SKILL_ENTRY_FILENAME = "SKILL.md"


class SkillLoader:
    """Skill 目录扫描器：name → SkillDef，执行期热重载（F3/F4/F17）。"""

    def __init__(self, work_dir: str, user_skills_dir: str = "") -> None:
        self._work_dir = work_dir
        self._project_dir = (Path(work_dir) / PROJECT_SKILLS_DIR).resolve()
        # user_skills_dir 供测试注入临时用户目录；生产路径固定 ~/.potatocode/skills
        if user_skills_dir:
            self._user_dir = Path(user_skills_dir).expanduser().resolve()
        else:
            self._user_dir = Path(USER_SKILLS_DIR).expanduser().resolve()
        # `_skills`：当前目录清单（load_all / reload 的结果，get_catalog 数据源）；
        # `_cache`：每个 name 最近一次成功解析的版本（热重载失败时回退，F17）
        self._skills: dict[str, SkillDef] = {}
        self._cache: dict[str, SkillDef] = {}

    @property
    def user_dir(self) -> Path:
        """用户级 skill 目录（InstallSkill 的安装目标，F19）。"""
        return self._user_dir

    def load_all(self) -> None:
        """按 项目 → 用户 顺序扫描两级目录，同 name 首次出现者保留（F3/N5）。"""
        self._skills.clear()
        self._scan_directory(self._project_dir, "project")
        self._scan_directory(self._user_dir, "user")

    def reload(self) -> None:
        """重新扫描两级目录（/skill reload 与 InstallSkill 安装后调用，F13/F19）。"""
        self.load_all()

    def _scan_directory(self, skills_dir: Path, source_label: str) -> None:
        """扫描一个 skill 根目录，兼容两种磁盘布局（F2）：

        - 单文件：`<name>.md`
        - 目录型：`<name>/SKILL.md`（可另带 references/ 等资源，is_directory=True）
        没有 SKILL.md 的子目录与其他文件一律静默跳过。
        """
        if not skills_dir.is_dir():
            return
        for entry in sorted(skills_dir.iterdir()):
            try:
                if entry.is_dir():
                    skill_md_path = entry / SKILL_ENTRY_FILENAME
                    if not skill_md_path.is_file():
                        continue  # 目录里没有 SKILL.md：不是 skill，静默跳过
                    skill = parse_skill_file(skill_md_path, is_directory=True)
                elif entry.is_file() and entry.suffix.lower() == ".md":
                    skill = parse_skill_file(entry, is_directory=False)
                else:
                    continue
            except SkillParseError as parse_error:
                logger.warning("Skipping %s skill '%s': %s", source_label, entry.name, parse_error)
                continue
            self._register_skill(skill)

    def _register_skill(self, skill: SkillDef) -> None:
        """登记一个 skill：同 name 首次出现者占位，后续同名跳过（F3/N5）。"""
        if skill.name in self._skills:
            logger.info("跳过重名 skill '%s'（已有更高优先级的同名定义）", skill.name)
            return
        self._skills[skill.name] = skill
        self._cache[skill.name] = skill

    def names(self) -> list[str]:
        """当前清单里的全部 skill 名（保持扫描顺序：项目级在前）。"""
        return list(self._skills)

    def all(self) -> list[SkillDef]:
        """当前清单里的全部 SkillDef（注册斜杠命令时遍历用）。"""
        return list(self._skills.values())

    def get(self, name: str) -> SkillDef | None:
        """按名取 skill；每次都重读源文件实现热重载（F4/F17）。

        重读失败（文件被改坏 / 被删）时回退 `_cache` 里的旧版本并记 warning，
        保证一次改坏不会把 skill 从会话里整个弄丢。
        """
        known = self._skills.get(name)
        if known is None:
            return None
        try:
            fresh = parse_skill_file(Path(known.source_path), is_directory=known.is_directory)
        except SkillParseError as parse_error:
            logger.warning("skill '%s' 热重载失败，回退缓存版本: %s", name, parse_error)
            return self._cache.get(name)
        self._skills[name] = fresh
        self._cache[name] = fresh
        return fresh

    def get_catalog(self) -> list[tuple[str, str]]:
        """目录清单：(name, description) 列表，供注入 Available Skills 段（F11）。"""
        return [(skill.name, skill.description) for skill in self._skills.values()]

    def get_source_label(self, name: str) -> str:
        """按 source_path 前缀返回来源 `project | user`（F20）；未知返回空串。"""
        known = self._skills.get(name)
        if known is None:
            return ""
        source_path = Path(known.source_path)
        if source_path.is_relative_to(self._project_dir):
            return "project"
        if source_path.is_relative_to(self._user_dir):
            return "user"
        return ""
