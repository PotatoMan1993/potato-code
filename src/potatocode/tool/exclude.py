"""遍历排除规则：两个遍历类工具（glob / grep）共用的默认排除判定（ch09 F1/F2）。

背景：项目根目录下的文件不问来源一律进入遍历，实测本仓库 98.9% 的行数来自 `.venv`。
本模块把"哪些目录不值得搜"抽成一处，供两个遍历类工具共用，避免规则各自漂移。
"""

from __future__ import annotations

from pathlib import Path

# 固定噪声目录名：非隐藏但公认无搜索价值（F1）。
# 用 frozenset 而非 list：判定在遍历热路径上，集合查找 O(1)；冻结后不可被运行时修改，
# 与"名单内置固定、不开放配置"一致。
# 隐藏目录不在此列举——由"名字以 . 开头"统一覆盖，新增 .tox/.next/.cache 等自动生效。
EXCLUDED_DIR_NAMES: frozenset[str] = frozenset({"__pycache__", "node_modules"})


def _is_excluded_dir(name: str) -> bool:
    """判断单个目录名是否命中排除规则：隐藏目录（`.` 开头）或固定噪声目录名（F1）。"""
    return name.startswith(".") or name in EXCLUDED_DIR_NAMES


def is_excluded(path: Path, root: Path) -> bool:
    """判断候选路径是否落在搜索根之下被排除的目录里（F1/F2）。

    只看**目录分量**（剥掉文件名本身），故根目录下的 `.foo.py` 这类隐藏文件不被排除；
    判定基于**相对 root** 的路径，因此当 root 自身就是某个被排除目录时规则不生效
    ——把搜索根显式指向该目录即成为"搜进去"的逃生口（F2），无需额外参数。

    `path` 不在 `root` 之下时返回 False（不排除）：本函数用于**搜索可见性**过滤，
    静默隐藏用户数据比多显示更糟（注意这与 permission 层的"安全默认"方向相反，
    那里失败必须收紧，这里失败应放开）；且两个遍历 API 的产出必在 root 下，该分支仅作防御。
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False  # 不在搜索根之下：不排除
    return any(_is_excluded_dir(part) for part in rel.parts[:-1])
