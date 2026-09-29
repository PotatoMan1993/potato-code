"""记忆子包的常量：层级名、文件名、索引大小上限。"""

from __future__ import annotations

# 笔记层级：项目级（当前仓库）与用户级（跨项目通用）
LEVEL_PROJECT = "project"
LEVEL_USER = "user"

# 每级一个索引文件，每行一条笔记摘要（F30）
INDEX_FILENAME = "MEMORY.md"

# 索引注入上限：超过则截断并追加标注（F34）
MAX_INDEX_BYTES = 25 * 1024
TRUNCATED_MARK = "(index truncated)"

# 索引里一句话描述的长度上限
DESC_LIMIT = 60
