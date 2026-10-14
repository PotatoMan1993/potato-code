"""Skill 远程安装：GitHub Contents API 递归拉取 + 暂存校验 + 原子落位（ch13 F19/T12）。

支持三种来源 URL：skills.sh / github.com tree / raw.githubusercontent.com；一律走
GitHub Contents API 递归拉取目录树（无需本地 git）。skills.sh 第三段是逻辑 skill 名，
物理路径探测 404 时用 git trees 递归清单重定位。文件内容经 Contents API raw 媒体类型
从 api.github.com 获取（raw.githubusercontent.com 在部分网络环境不可达），其
download_url 仅作回退。限额：单文件 ≤1 MiB、总大小 ≤8 MiB、文件数 ≤64、目录深度 ≤4。
先暂存到安装根目录下的兄弟 tempdir，验证含 SKILL.md 后原子改名落位；任何失败都清理
暂存目录。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .loader import SKILL_ENTRY_FILENAME
from .parser import SKILL_NAME_PATTERN, SkillParseError, _validate_meta, parse_frontmatter

logger = logging.getLogger(__name__)

# 拉取限额（F19）
MAX_FILE_SIZE = 1 * 1024 * 1024  # 单文件 ≤1 MiB
MAX_TOTAL_SIZE = 8 * 1024 * 1024  # 总大小 ≤8 MiB
MAX_FILE_COUNT = 64  # 文件数 ≤64
MAX_RECURSION_DEPTH = 4  # 目录深度 ≤4

# GitHub API 与 raw 下载基址
GITHUB_API_BASE = "https://api.github.com"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com"

# 下载超时（秒）与请求头（Contents API 要求显式 User-Agent）
DOWNLOAD_TIMEOUT_SECONDS = 60.0
GITHUB_ACCEPT_HEADER = "application/vnd.github+json"
GITHUB_RAW_MEDIA_TYPE = "application/vnd.github.raw"
HTTP_USER_AGENT = "PotatoCode-InstallSkill"

# 暂存目录名前缀：位于安装根目录内，保证与目标目录同卷、可原子改名
STAGING_DIR_PREFIX = ".staging-"


class InstallSkillError(Exception):
    """安装失败：URL 不受支持 / 来源不存在 / 超限 / 缺少 SKILL.md 等。"""


@dataclass(frozen=True)
class SkillSourceLocation:
    """解析后的 skill 来源：GitHub 仓库 + 可选 ref 与子目录。"""

    owner: str
    repo: str
    ref: str = ""  # 空 = 仓库默认分支
    subdir: str = ""  # 仓库内子路径（skill 根目录）
    single_file: bool = False  # True：URL 直接指向单个 SKILL.md 文件
    source_host: str = ""  # 原始 URL 的 host（归一化后）；skills.sh 用于逻辑名重定位

    @property
    def fallback_name(self) -> str:
        """来源推导的兜底 skill 名（frontmatter name 缺失 / 非法时用）。"""
        if self.subdir:
            return self.subdir.rstrip("/").split("/")[-1]
        return self.repo


@dataclass
class _DownloadBudget:
    """一次安装的拉取用量账本：文件数与总字节数（超限即中止）。"""

    file_count: int = 0
    total_bytes: int = 0
    limits_message: str = (
        f"限额：单文件 ≤{MAX_FILE_SIZE // 1024 // 1024}MiB、"
        f"总大小 ≤{MAX_TOTAL_SIZE // 1024 // 1024}MiB、"
        f"文件数 ≤{MAX_FILE_COUNT}、深度 ≤{MAX_RECURSION_DEPTH}"
    )


def parse_skill_url(url: str) -> SkillSourceLocation:
    """解析三种受支持的 skill 来源 URL；其余 host 抛 ValueError（F19）。

    - `https://skills.sh/<owner>/<repo>[/<skill 名>]`：skill 市场地址，按同名 GitHub
      仓库处理（skills.sh 的 skill 即 GitHub 仓库）；第三段是**逻辑 skill 名**，
      不一定等于仓库内物理路径，404 时会用 git trees 清单重定位
    - `https://github.com/<owner>/<repo>/tree/<ref>[/<子目录>]`：仓库目录页
    - `https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<路径以 SKILL.md 结尾>`：
      单文件直链
    """
    text = url.strip()
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = parsed.netloc.lower()
    # 归一化 www. 前缀：浏览器地址栏常复制出 www.skills.sh / www.github.com
    if host.startswith("www."):
        host = host[4:]
    logger.debug(f"url host: {host}")
    path_parts = [part for part in parsed.path.split("/") if part]

    if host == "skills.sh":
        if len(path_parts) < 2:
            raise ValueError(f"skills.sh URL 需包含 owner 与 skill 名: {url}")
        owner, repo = path_parts[0], path_parts[1]
        subdir = "/".join(path_parts[2:])
        return SkillSourceLocation(owner=owner, repo=repo, subdir=subdir, source_host=host)

    if host == "github.com":
        if len(path_parts) < 2:
            raise ValueError(f"github.com URL 需包含 owner 与 repo: {url}")
        owner, repo = path_parts[0], path_parts[1]
        rest = path_parts[2:]
        if rest and rest[0] == "tree":
            if len(rest) < 2:
                raise ValueError(f"github tree URL 需包含分支或标签名: {url}")
            # 注：含斜杠的分支名在 tree URL 里天然有歧义，这里取首段为 ref
            return SkillSourceLocation(
                owner=owner,
                repo=repo,
                ref=rest[1],
                subdir="/".join(rest[2:]),
            )
        return SkillSourceLocation(owner=owner, repo=repo)  # 仓库根 = 默认分支

    if host == "raw.githubusercontent.com":
        if len(path_parts) < 3:
            raise ValueError(f"raw URL 需包含 owner/repo/ref: {url}")
        owner, repo, ref = path_parts[0], path_parts[1], path_parts[2]
        raw_path = "/".join(path_parts[3:])
        return SkillSourceLocation(
            owner=owner,
            repo=repo,
            ref=ref,
            subdir=raw_path,
            single_file=raw_path.endswith(SKILL_ENTRY_FILENAME),
        )

    raise ValueError(
        f"不支持的 skill 来源 URL: {url}（支持 skills.sh / github.com / raw.githubusercontent.com）"
    )


async def install_skill(
    src: str,
    install_root: Path,
    client: httpx.AsyncClient | None = None,
) -> str:
    """从 URL 安装 skill 到 `install_root/<name>/`，返回 skill 名（F19/T12）。

    流程：Contents API 递归拉取到兄弟暂存目录 → 校验含 SKILL.md 且 name 合法 →
    原子改名落位（同名先删旧）。任何失败都清理暂存并抛 InstallSkillError / ValueError。
    `client` 供测试注入 MockTransport；生产路径内部自建并负责关闭。
    """
    location = parse_skill_url(src)
    root = Path(install_root)
    root.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=STAGING_DIR_PREFIX, dir=root))
    if client is not None:
        await _download_into_staging(client, location, staging_dir)
    else:
        async with httpx.AsyncClient(
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
            headers=_github_headers(),
            follow_redirects=True,
        ) as owned_client:
            await _download_into_staging(owned_client, location, staging_dir)
    try:
        if not (staging_dir / SKILL_ENTRY_FILENAME).is_file():
            raise InstallSkillError(
                f"拉取结果缺少 {SKILL_ENTRY_FILENAME}，拒绝安装（已清理暂存）"
            )
        skill_name = _resolve_skill_name(staging_dir, location.fallback_name)
        target_dir = root / skill_name
        if target_dir.exists():
            # 同名覆盖：先删旧再改名。删除与改名之间存在极小窗口，属可接受妥协
            # （os.replace 无法直接覆盖非空目录）。
            shutil.rmtree(target_dir)
        os.replace(staging_dir, target_dir)
        logger.info("skill '%s' 已安装到 %s", skill_name, target_dir)
        return skill_name
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


async def _download_into_staging(
    client: httpx.AsyncClient,
    location: SkillSourceLocation,
    staging_dir: Path,
) -> None:
    """按来源类型把远程内容拉进暂存目录：单文件直链或目录树递归。"""
    if location.single_file:
        await _download_single_skill_file(client, location, staging_dir)
        return
    budget = _DownloadBudget()
    try:
        await _fetch_tree(client, location, location.subdir, staging_dir, depth=0, budget=budget)
    except InstallSkillError as first_error:
        # skills.sh 第三段是逻辑 skill 名，可能 ≠ 仓库内物理路径（如
        # anthropics/skills 的 frontend-design 实际在 skills/frontend-design/）。
        # 仅当逻辑名探测 404 时用 git trees 清单重定位后重试一次。
        if location.source_host != "skills.sh" or not location.subdir:
            raise
        resolved_subdir = await _resolve_marketplace_subdir(client, location)
        if resolved_subdir is None:
            raise first_error
        logger.info("skills.sh 逻辑名 '%s' 重定位到仓库路径 '%s'", location.subdir, resolved_subdir)
        location = replace(location, subdir=resolved_subdir)
        budget = _DownloadBudget()
        await _fetch_tree(client, location, location.subdir, staging_dir, depth=0, budget=budget)


async def _resolve_marketplace_subdir(
    client: httpx.AsyncClient,
    location: SkillSourceLocation,
) -> str | None:
    """在仓库树中定位逻辑 skill 名对应的物理目录；找不到或结果不唯一返回 None。

    git trees API（recursive）一次拿全仓库清单，找 basename 匹配且含 SKILL.md
    的目录；多个候选时无法替用户做主，返回 None（错误信息里保留原始 404）。
    """
    api_url = (
        f"{GITHUB_API_BASE}/repos/{location.owner}/{location.repo}"
        f"/git/trees/{location.ref or 'HEAD'}"
    )
    response = await client.get(api_url, params={"recursive": "1"})
    if response.status_code != 200:
        return None
    payload = response.json()
    skill_name = location.subdir.rstrip("/").split("/")[-1]
    suffix = f"/{skill_name}/{SKILL_ENTRY_FILENAME}"
    candidates: list[str] = []
    for entry in payload.get("tree", []):
        entry_path = str(entry.get("path") or "")
        if entry.get("type") == "blob" and (
            entry_path == f"{skill_name}/{SKILL_ENTRY_FILENAME}" or entry_path.endswith(suffix)
        ):
            candidates.append(entry_path[: -len(SKILL_ENTRY_FILENAME)].rstrip("/"))
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        logger.info("仓库内存在多个同名 skill 目录，不自动选择: %s", candidates)
    return None


def _github_headers() -> dict[str, str]:
    """GitHub API / raw 下载的公共请求头。"""
    return {
        "User-Agent": HTTP_USER_AGENT,
        "Accept": GITHUB_ACCEPT_HEADER,
    }


def _raw_file_headers() -> dict[str, str]:
    """单文件内容下载的请求头：raw 媒体类型让 Contents API 直接返回文件字节。"""
    return {"Accept": GITHUB_RAW_MEDIA_TYPE}


async def _download_single_skill_file(
    client: httpx.AsyncClient,
    location: SkillSourceLocation,
    staging_dir: Path,
) -> None:
    """单文件直链安装：把 SKILL.md 下载为暂存目录的入口文件。

    优先走 Contents API raw 媒体类型（api.github.com，部分网络环境可达而
    raw.githubusercontent.com 被墙）；失败再回退原始 raw 直链。
    """
    api_url = (
        f"{GITHUB_API_BASE}/repos/{location.owner}/{location.repo}"
        f"/contents/{location.subdir}"
    )
    params = {"ref": location.ref} if location.ref else None
    response = await client.get(api_url, params=params, headers=_raw_file_headers())
    if response.status_code != 200:
        raw_url = f"{GITHUB_RAW_BASE}/{location.owner}/{location.repo}/{location.ref}"
        if location.subdir:
            raw_url = f"{raw_url}/{location.subdir}"
        response = await client.get(raw_url)
    if response.status_code != 200:
        raise InstallSkillError(f"下载 {SKILL_ENTRY_FILENAME} 失败: HTTP {response.status_code}")
    if len(response.content) > MAX_FILE_SIZE:
        raise InstallSkillError(
            f"文件超过单文件上限 {MAX_FILE_SIZE // 1024 // 1024}MiB: {location.subdir}"
        )
    (staging_dir / SKILL_ENTRY_FILENAME).write_bytes(response.content)


async def _fetch_tree(
    client: httpx.AsyncClient,
    location: SkillSourceLocation,
    repo_path: str,
    dest_dir: Path,
    depth: int,
    budget: _DownloadBudget,
) -> None:
    """Contents API 递归拉取 `repo_path` 目录树到 `dest_dir`（T12）。

    每层先做路径安全校验，再按 entry 类型分派：文件进限额账本后下载，目录递归。
    """
    if depth > MAX_RECURSION_DEPTH:
        raise InstallSkillError(
            f"目录深度超过上限 {MAX_RECURSION_DEPTH}（{budget.limits_message}）"
        )
    api_url = f"{GITHUB_API_BASE}/repos/{location.owner}/{location.repo}/contents"
    if repo_path:
        api_url = f"{api_url}/{repo_path}"
    params = {"ref": location.ref} if location.ref else None
    response = await client.get(api_url, params=params)
    if response.status_code == 404:
        raise InstallSkillError(f"来源路径不存在: {repo_path or '(仓库根)'}")
    if response.status_code != 200:
        raise InstallSkillError(f"GitHub API 请求失败: HTTP {response.status_code}")
    entries = response.json()
    if isinstance(entries, dict):  # repo_path 直接指向文件时的单对象响应
        entries = [entries]

    root_prefix = f"{location.subdir.strip('/')}/" if location.subdir.strip("/") else ""
    for entry in entries:
        entry_path = str(entry.get("path") or "")
        entry_type = entry.get("type")
        if not entry_path.startswith(root_prefix):
            continue  # 防御：只处理 skill 根目录内的条目
        relative_path = entry_path[len(root_prefix) :]
        _assert_safe_relative_path(relative_path)
        if entry_type == "file":
            await _download_entry_file(client, location, entry, relative_path, dest_dir, budget)
        elif entry_type == "dir":
            await _fetch_tree(client, location, entry_path, dest_dir, depth + 1, budget)
        # 其余类型（symlink / submodule）一律跳过：不落盘、不计数


async def _download_entry_file(
    client: httpx.AsyncClient,
    location: SkillSourceLocation,
    entry: dict,
    relative_path: str,
    dest_dir: Path,
    budget: _DownloadBudget,
) -> None:
    """下载单个文件：先按声明的 size 与账本限额预检，再写入目标路径。

    内容优先经 Contents API raw 媒体类型取（api.github.com）；raw.githubusercontent.com
    在部分网络环境不可达，故其 download_url 仅作回退。
    """
    declared_size = int(entry.get("size") or 0)
    if declared_size > MAX_FILE_SIZE:
        raise InstallSkillError(
            f"文件超过单文件上限 {MAX_FILE_SIZE // 1024 // 1024}MiB: {relative_path}"
        )
    budget.file_count += 1
    if budget.file_count > MAX_FILE_COUNT:
        raise InstallSkillError(f"文件数超过上限 {MAX_FILE_COUNT}（{budget.limits_message}）")
    budget.total_bytes += declared_size
    if budget.total_bytes > MAX_TOTAL_SIZE:
        raise InstallSkillError(f"总大小超过上限 {MAX_TOTAL_SIZE // 1024 // 1024}MiB")

    entry_path = str(entry.get("path") or relative_path)
    api_file_url = (
        f"{GITHUB_API_BASE}/repos/{location.owner}/{location.repo}/contents/{entry_path}"
    )
    params = {"ref": location.ref} if location.ref else None
    response = await client.get(api_file_url, params=params, headers=_raw_file_headers())
    if response.status_code != 200:
        download_url = entry.get("download_url")
        if not download_url:
            raise InstallSkillError(
                f"文件下载失败 HTTP {response.status_code}，且缺少 download_url: {relative_path}"
            )
        response = await client.get(download_url)
    if response.status_code != 200:
        raise InstallSkillError(f"下载失败 HTTP {response.status_code}: {relative_path}")
    if len(response.content) > MAX_FILE_SIZE:  # 以实际字节为准再核一遍
        raise InstallSkillError(
            f"文件超过单文件上限 {MAX_FILE_SIZE // 1024 // 1024}MiB: {relative_path}"
        )
    target_path = dest_dir / relative_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(response.content)


def _assert_safe_relative_path(relative_path: str) -> None:
    """路径安全校验：拒绝空段 / `..` / 反斜杠 / 绝对路径（zip-slip 同源的防御）。"""
    segments = relative_path.split("/")
    if (
        not relative_path
        or relative_path.startswith("/")
        or "\\" in relative_path
        or any(segment in ("", ".", "..") for segment in segments)
    ):
        raise InstallSkillError(f"来源路径不安全，拒绝安装: {relative_path!r}")


def _resolve_skill_name(staging_dir: Path, fallback_name: str) -> str:
    """确定安装名：优先 SKILL.md frontmatter 的 name，失败回退来源名清洗结果。"""
    try:
        meta, _ = parse_frontmatter(
            (staging_dir / SKILL_ENTRY_FILENAME).read_text(encoding="utf-8")
        )
        _validate_meta(meta)
        return str(meta["name"])
    except (SkillParseError, OSError) as parse_error:
        logger.info("frontmatter 校验失败，回退来源目录名: %s", parse_error)
    sanitized = _sanitize_skill_name(fallback_name)
    if not sanitized:
        raise InstallSkillError(
            f"无法确定 skill 名：frontmatter 缺少合法 name，来源名 {fallback_name!r} 也无法清洗"
        )
    return sanitized


def _sanitize_skill_name(raw: str) -> str:
    """把来源名清洗成合法 skill 名：非 [a-zA-Z0-9-] 归一为 `-`，修正首字符。"""
    cleaned = re.sub(r"[^a-zA-Z0-9-]+", "-", raw).strip("-").lower()
    if not cleaned:
        return ""
    if not SKILL_NAME_PATTERN.fullmatch(cleaned):  # 通常是首字符为数字
        cleaned = f"skill-{cleaned}"
    return cleaned if SKILL_NAME_PATTERN.fullmatch(cleaned) else ""
