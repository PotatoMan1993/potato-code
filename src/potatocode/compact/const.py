"""上下文管理的全部硬编码常量：集中一处，便于审查与调整。

这些常量不暴露为配置项，调整属于代码变更；唯一走配置的是 provider 的
`context_window`（见 potatocode.config）。
"""

from __future__ import annotations

# 单条工具结果超过该字节数即落盘，对话里只留预览体
SINGLE_RESULT_LIMIT = 50000
# 同一条 RoleTool 消息内工具结果聚合超过该字节数即继续按倒序落盘
MESSAGE_AGGREGATE_LIMIT = 200000
# 给摘要请求自身输出预留的 token 空间
SUMMARY_RESERVE = 20000
# 自动触发的额外安全余量：防估算误差与单轮波动
AUTO_SAFETY_MARGIN = 13000
# 手动 / 紧急触发的安全余量：只用来判断摘要请求本身能不能塞下
MANUAL_SAFETY_MARGIN = 3000
# 恢复段最多展示几个最近读过的文件
RECOVERY_FILE_LIMIT = 5
# 单个文件快照的 token 上限，超出时保留头部、截掉尾部
RECOVERY_TOKENS_PER_FILE = 5000
# 摘要后保留近期原文的 token 下界
RECENT_KEEP_TOKENS = 10000
# 摘要后保留近期原文的条数下界
RECENT_KEEP_MESSAGES = 5
# 自动摘要连续失败多少次后熔断
MAX_CONSECUTIVE_AUTO_COMPACT_FAILURES = 3
# 摘要请求自身撞 PTL 时的直接重试次数
PTL_RETRY_LIMIT = 3
# 直接重试次数用光后，每次再丢弃的消息组比例
PTL_DROP_PERCENTAGE = 0.2
# 增量估算的字符/token 比
ESTIMATE_CHARS_PER_TOKEN = 3.5
# 预览体头部字节数上限
PREVIEW_HEAD_BYTES = 2048
# 预览体头部行数上限
PREVIEW_HEAD_LINES = 20
