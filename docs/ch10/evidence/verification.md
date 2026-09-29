# 运行日志落盘 验收证据

> 实测日期：2026-09-29。命令均在仓库根执行。
> 端到端脚本：`docs/ch10/evidence/logging_e2e.py`（`uv run python docs/ch10/evidence/logging_e2e.py`），
> 结果 **14/14 通过**。

## 一、改动前后的基线对照

改动前的数据均在 `git stash` 后的 HEAD 工作区实测取得，非推断。

| 检查项 | 改动前（HEAD） | 改动后 | 结论 |
|--------|---------------|--------|------|
| `uv run pytest -q` | `2 failed, 341 passed, 2 skipped` | `2 failed, 356 passed, 2 skipped` | 失败项相同；通过数 +15，恰为本章新增用例数 |
| `uv run ruff check .` | `Found 6 errors` | `Found 6 errors` | 数量与位置均未变，全部位于本章未触碰的文件 |
| `uv run ruff format --check .` | `3 files would be reformatted, 87 already` | `3 files would be reformatted, 88 already` | 待格式化文件仍是同样 3 个；总数 +1 为新增文件 |
| `uv run mypy src/potatocode` | `Found 2 errors in 1 file (53 files)` | `Found 2 errors in 1 file (54 files)` | 同为 `agent/__init__.py:299,341`，未新增 |

**既有失败与既有告警的归属**（均与本章无关）：

- `tests/test_mcp_config.py::test_documented_example_parses`：用例引用的示例文件不在仓库中，ch07 遗留，ch09 已记录。
- `tests/test_mcp_manager.py::test_real_http_server_headers_call_and_delete`：依赖真实本地 HTTP server，环境相关。
- `ruff check` 的 6 项：`_probe_grep.py` / `_probe_grep2.py` 各 1 项导入排序，`compact/compact.py` 2 项超长行，`tool/read_file.py` 2 项超长行。
- `ruff format` 的 3 个文件：`compact/compact.py`、`tool/grep_tool.py`、`tool/read_file.py`。
- `mypy` 的 2 项：`agent/__init__.py` 中 `executed_error: str | None` 传给 `err: Exception | None`，来自提交 `1e7a7f4`。

**本章新增/修改的 10 个文件，格式检查与静态检查均零告警。**

## 二、端到端场景结果

### S1 正常启动落盘 — 通过

真实启动链路（真实会话创建、真实日志装配、真实配置解析、真实权限引擎与运行时构造）跑通，退出码 0。

实测会话目录：`.potatocode/sessions/1790656963-1c7fd5f6/session.log`，与同会话的 `tool-results/` 同目录。

### S2 配置非法仍留证 — 通过

真实子进程 `python -m potatocode`，工作区无配置文件：

```
exit=1
stderr: '配置错误: 配置文件不存在: .potatocode/config.yaml\n'
会话日志含："配置错误"
```

即：终端可读提示保留（原有行为未变），同时该会话目录下留下了只含这条错误日志的会话（spec 中已声明的取舍）。

### S3 压缩失败可追栈 — 通过

走真实的 `compact.manage_context`（手动触发路径），provider 在摘要请求上抛
`RuntimeError("摘要请求被拒：prompt_too_long")`。日志中同时命中
`上下文压缩失败`、`Traceback`、`prompt_too_long` 三个标记——堆栈完整落盘。
这是本章改动前**完全丢失**的信息。

### S4 MCP 连不上可追因 — 通过

配置一个命令不存在的 stdio server，真实连接尝试后日志中出现：

```
WARNING potatocode.mcp.manager:105 — connect server unreachable failed: [WinError 2] 系统找不到指定的文件。
```

### S5 详细度可临时调整 — 通过

同一场景两次运行：默认级别下调试级记录**不出现**；设 `POTATOCODE_LOG_LEVEL=DEBUG` 后**出现**。未改动任何代码或配置文件。

### S6 两会话互不串扰 — 通过

两个独立工作区各自启动：

```
1790656965-fea0dfc4 / 1790656965-242b7429
```

互检两个日志文件，均不含对方的会话 id。

## 三、真实日志样本

`POTATOCODE_LOG_LEVEL=DEBUG` 下的一次真实启动，文件全文：

```
2026-09-29 12:42:50 INFO    potatocode.cli:37 — 启动：session=1790656970-f675812a workspace=C:\Users\...\potatocode-ch10-sample-04yi_jcj
2026-09-29 12:42:50 WARNING potatocode.mcp.manager:105 — connect server unreachable failed: [WinError 2] 系统找不到指定的文件。
2026-09-29 12:42:50 INFO    potatocode.demo:70 — 本轮会话已就绪
2026-09-29 12:42:50 DEBUG   potatocode.demo:71 — 这条只在提升详细度后才应出现
```

四要素齐备（时间 / 级别 / 来源模块:行号 / 消息），中文无乱码。同一场景下终端 stderr 实测内容仅剩
`依赖库告警：不应出现在会话日志里`——即依赖库经标准库兜底处理器写往 stderr 的**原有行为未变**，
而本项目的自有日志（含原先写 stderr 的 MCP 告警）**一律不再上屏**，全部进入会话日志。

## 四、第三方隔离的实测

在同一次真实运行中让依赖库打日志（`httpx` 告警级 + `anthropic._client` 调试级）后解析会话日志：

- 依赖库的两条内容**均未进入文件**，即便详细度已提升到调试级；
- 文件中全部记录的来源字段为 `['potatocode.cli', 'potatocode.demo']`，**全部以 `potatocode.` 开头**。

隔离由"出口挂在包级日志器 `potatocode` 上且关闭其传播"结构性保证，不依赖任何名字黑名单。

## 五、一处实现与 plan 的偏差

plan.md「logging_setup（新增模块）」的常量表列出了 `LOG_FILENAME`，实际实现放在
`compact/state.py`。理由：`setup_logging` 接收的是**完整路径**，它不需要知道文件名；
而"会话目录里放哪些文件"由 `state.py` 的会话布局定义（`spill_dir` 已在其中），
常量放在那里更贴近职责。若放在 `logging_setup` 会成为一个无人使用的死常量，
或反过来迫使会话布局去依赖日志模块。功能与验收标准不受影响。

## 六、尚未覆盖的部分

以下两项**未做真实 TUI 交互验证**，仅有间接证据，如实记录：

- **S1 的"真实 TUI"**：端到端脚本用替身替换了 `PotatoCodeApp.run_async`，因为脚本需在无 TTY 环境运行。
  因此被验证的是**启动全链路 + 日志出口的装配与收尾**，而非 TUI 主循环本身。
  日志落盘发生在 TUI 启动之前与退出之后，与 TUI 渲染无耦合，判断风险很低但没有实测。
- **S1 的"发起一轮真实对话"**：spec 的 AC1 原话含"发起一轮对话后"。端到端脚本未发对话，
  因为对话需要联网与真实密钥（AGENTS.md 禁止读取密钥）。作为替代，`compact` 链路的
  信息级记录已由 S3 真实触发，MCP 链路的信息级记录已由 S4 真实触发。
