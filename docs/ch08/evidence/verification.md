# ch08 上下文管理 · 实际执行结果

执行环境：Windows 11 + Python 3.14（`.venv`），命令统一用 `uv`/venv 解释器运行。
本节记录的是**实际跑出来的结果**，不是设计预期。

## 一、静态检查

| 检查 | 命令 | 结果 |
|------|------|------|
| B1 语法 | `python -m compileall -q src/potatocode` | 通过，退出码 0 |
| B2 lint | `ruff check src/potatocode/compact/ agent/ config/ llm/ tui/ conversation.py cli.py` | 全部通过 |
| B3 格式 | `ruff format --check src/potatocode/ tests/compact tests/llm` | 除 `tool/read_file.py`（ch07 遗留）外全部已格式化 |
| B4 import 分组 | `ruff check --select I src/potatocode/compact/` | 全部通过 |
| mypy | `mypy src/potatocode` | Success: no issues found in 52 source files |

> `src/potatocode/tool/read_file.py` 的两条 E501（长描述行）来自 ch07，不在本章改动范围，未改动。

## 二、单元测试

| 命令 | 结果 |
|------|------|
| `pytest tests` | **320 passed, 1 failed, 2 skipped** |
| `pytest tests/compact/` | 82 passed（状态 / token / 第 1 层 / 摘要模板 / 恢复段 / 第 2 层 / 编排） |
| `pytest tests/llm/` | 12 passed（两协议 PTL 包装与误判排除） |
| `pytest tests/test_agent.py` | 37 passed（含锚点替换、defs 同引用、文件追踪、紧急压缩三场景、互斥） |
| `pytest tests/test_tui.py` | 19 passed（四条命令路由 + 压缩提示渲染） |
| `pytest tests/test_config.py tests/test_conversation.py` | 28 passed |

唯一失败项 `tests/test_mcp_config.py::test_documented_example_parses` 报
`FileNotFoundError: docs/mcp/mcp-servers.example.yaml`——该示例文件在 ch07 之后已不在仓库中，
与本章无关，未修复。

## 三、端到端证据脚本

`python docs/ch08/evidence/context_e2e.py`（真实 TUI + 真实 Conversation + 真实工具执行与落盘，
只有 LLM 被脚本化）：**14/14 通过**。

```
[PASS] E12 会话目录存在
[PASS] E2 大工具结果已落盘  文件=['call_big']
[PASS] E2 落盘内容是工具结果原文（带行号，大于裸文件）  108889 字节 vs 裸文件 106690 字节
[PASS] E2 对话内已替换为预览体
[PASS] E2 原文未被塞进请求  预览体 731 字节
[PASS] E2 预览体声明的原始字节数与落盘文件一致  108889
[PASS] E4 read_file 已记入文件追踪
[PASS] E5 手动压缩提示 token 变化  ['已压缩，token 从 1284 降至 13535']
[PASS] E5 会话被替换为摘要开头  会话现有 6 条
[PASS] E12 未知命令提示
[PASS] E12 命令未写入对话
[PASS] E13 自动压缩中提示
[PASS] E13 自动压缩完成提示
[PASS] E12 /exit 未发普通对话请求
```

CLI 启动冒烟（`python -m potatocode`，非交互管道下 20s 超时终止）：启动后
`.potatocode/sessions/<unix_ts>-<hex>/tool-results/` 被创建，进程退出后目录保留 —— 对应 I11。

## 四、与 checklist 的逐项对照

### 实现完整性

- C1 compact 子包九文件齐备，`import potatocode.compact` 正常。
- C2 阈值常量集中在 `const.py`；协议默认值在 `config/protocol_defaults.py`。
- C3 `new_session_context` 返回 `<unix_ts>-<hex>` 且落盘目录真实存在，两次调用 id 不同。
- C4 账本两本簿子：kept 只进 `_seen_ids`、replaced 两本都进、skip 都不进（单测三例）。
- C5 熔断器四动作齐备，3 次失败跳闸、success 立即清零。
- C6 文件追踪 50 线程并发写读无异常，快照返回拷贝。
- C7 单条 60K / 聚合 3×80K / 幂等落盘 / 决策冻结四类行为均有用例。
- C8 落盘失败（父路径是文件）时保持原文且不写账本，下一轮能成功补落盘。
- C9 预览体四项信息齐备；头部同时满足 ≤20 行与 ≤2048 字节；相同入参逐字节稳定。
- C10 摘要模板含两阶段标签 + 9 小节；解析丢弃 analysis；用户消息原文逐条进入请求体。
- C11 恢复段三段标题齐备；7 条记录只渲染最近 5 条（第 6、7 条反向断言不出现）；超长文件保留头部并标注截断。
- C12 相同入参连续两次渲染逐字节相等。
- C13 `anchor=5000 / 350 字符 / anchor_msg_len=0` → 5100；`usage_anchor` 四字段求和。
- C13a 阈值下方不触发 layer2、上方触发（边界用例：恰好等于阈值即触发）。
- C14 `/compact` 走命令路径（`compact_calls==1`、`run_calls==0`）；`/unknown` 给提示且不发 LLM。
- C15 `run_force_compact(conv, tool_defs) -> (before, after)`，失败上抛。
- C16 两协议 PTL 被包装成 `PromptTooLongError` 且 `__cause__` 保留原异常；500 与其他 400 不误判。
- C17/C18 `context_window` 可从 YAML 解码；四种场景默认值均正确。

### 集成

- I1 `replace_history` 深拷贝（含子列表）、支持 None/空列表。
- I2 摘要后 `conv.messages()` = 摘要+恢复（1 条）+ 近期原文。
- I3 同一轮迭代 `ManageInput.tool_defs is Request.tools`（对象同一性断言），Plan 模式为只读集。
- I4/I4-bis 锚点被最新 usage 替换（观测序列 `[0, 1000] → 1500`，非累加）；摘要请求的 usage 不写回。
- I5 read_file 成功后记录纯净字节（与磁盘原文逐字节相等）；其他工具不记录。
- I6 撞墙→紧急压缩→重试一次成功；重试再次撞墙上抛；压缩后仍过大直接上抛不重试。
- I7 注册表恰好 4 项，`/exit` `/plan` `/do` 行为与迁移前一致。
- I8 系统消息含压缩前后 token；`(500, 300)` 也能显示；失败显示 `压缩失败：<err>` 且不退出。
- I12 `run` 与 `run_force_compact` 串行：run 阻塞期间手动压缩不发摘要请求，run 结束后才执行。
- I13/I14/I15 自动与紧急各一对 Compact 事件；阈值未达 0 事件；TUI 三处文案与 `format_compact_notice` 逐字节一致。

### 未在本机执行的部分

- **真实 provider 的联网 E2E**（tmux 手测、真实 Anthropic/OpenAI 请求）：本环境无 tmux，
  且需要消耗真实密钥额度，未执行。等价链路已由 `docs/ch08/evidence/context_e2e.py` 用脚本化
  provider 覆盖（真实 TUI / 真实工具 / 真实落盘 / 真实视图层）。
