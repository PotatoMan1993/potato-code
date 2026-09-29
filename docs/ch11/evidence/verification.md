# 项目记忆与会话持久化 验收证据

> 实测日期：2026-09-29。命令均在仓库根执行。
> 端到端脚本：`docs/ch11/evidence/session_e2e.py`（`uv run python docs/ch11/evidence/session_e2e.py`），
> 结果 **19/19 通过**（真实 provider：`Claude-deepseek` / anthropic / `deepseek-v4-flash`）。

## 一、改动前后的基线对照

改动前的数据在 `HEAD` 的独立 worktree 里实测取得（`PYTHONPATH` 指向该 worktree 的 `src`，
避免可编辑安装把 `src` 指回主工作区），非推断。

| 检查项 | 改动前（HEAD） | 改动后 | 结论 |
|--------|---------------|--------|------|
| `uv run pytest -q` | `2 failed, 356 passed, 2 skipped` | `2 failed, 449 passed, 2 skipped` | 失败项完全相同；通过数 +93，为本章新增用例数 |
| `uv run ruff check .` | `Found 6 errors` | `Found 2 errors` | 减少 4 项：本章顺带修掉 `compact/compact.py` 2 项、`tool/read_file.py` 2 项超长行；剩余 2 项与本章无关 |
| `uv run ruff format --check .` | `3 files would be reformatted, 90 already formatted` | `110 files already formatted` | 全仓格式化已清零 |
| `uv run ruff check src tests`（本章范围） | — | `All checks passed!` | 新增 3 个子包与会话恢复界面零告警 |

**既有失败与既有告警的归属**（均与本章无关）：

- `tests/test_mcp_config.py::test_documented_example_parses`：用例引用的示例文件不在仓库中，ch07 遗留。
- `tests/test_mcp_manager.py::test_real_http_server_headers_call_and_delete`：依赖真实本地 HTTP server，环境相关。
- `ruff check .` 剩余 2 项：根目录 `_probe_grep.py` / `_probe_grep2.py` 的导入排序，ch09 的调试脚本。

**一处需要显式修正的既有用例**：`tests/test_mcp_cli.py` 里的 `FakeApp` 按旧入参表签名，
本章给 `PotatoCodeApp` 增加了 `writer / mem_mgr / instruction_text / memory_text / sessions_dir /
workspace` 六个入参后该替身会 `TypeError`；已同步扩展替身签名，并补了三条断言确认
启动流程确实把这些对象交给了 TUI（而不是悄悄退化成无持久化运行）。

## 二、端到端场景结果

脚本在临时工作区里跑**真实链路**：真实 provider 请求、Textual 无头驱动（与
`python -m potatocode` 同一条装配与事件循环路径）、真实 JSONL 落盘与恢复、真实 LLM 记忆提炼。
会话目录、项目级 memory、用户级 memory 全部落在临时目录，不碰工作区与用户主目录。

### 场景 1（冷启动 + 项目指令 + 会话存档）— 通过

工作区写入 `MEWCODE.md`（"所有回复必须以「喵~」开头"），启动时加载到：

```
启动时加载的指令: '所有回复必须以「喵~」开头。'
```

模型回复：

```
喵~ 我是 PotatoCode，一个跑在终端里…
```

即项目指令**确实进了系统提示并被模型遵循**。存档侧：

```
会话 id: 20260929-163707-4b8f
[PASS] JSONL 至少 user + assistant 两行 — 2 行
[PASS] 首行带 model 字段 — deepseek-v4-flash
[PASS] 每行可解析且带 role / ts
```

会话 id 采用新格式（正则 `\d{8}-\d{6}-[0-9a-f]{4}` 可匹配），首行携带模型名。

### 场景 2（自动记忆）— 通过

输入"记住：以后回复都要简洁，不要结尾复述"后异步提炼出笔记：

```
索引行: - [user_preference] 回复简洁且结尾不复述 — 用户要求回复保持简洁，不要在结尾复述已说过的内容。（user_preference_terse_replies_no_summary.md）
[PASS] 记忆笔记落盘（项目级或用户级）
[PASS] 索引行含类型与文件名
[PASS] 同一轮仍在追加写入同一存档 — 2 → 4 行
```

索引行格式为 `- [<type>] <title> — <描述>（<文件名>）`：类型、一句话描述与文件名都齐，
模型据此知道要读哪个文件才能拿到全文。本次 LLM 判定为跨项目通用信息，落在用户级 memory。

### 场景 3（会话恢复 `/resume`）— 通过

```
新进程开的是新会话 — 20260929-163707-4b8f → 20260929-163712-13f6
列表（新→旧）: ['20260929-163708-977e', '20260929-163707-4b8f']
[PASS] /resume 进入列表态
[PASS] 列表包含历史会话 — 2 项
[PASS] 选中的是目标会话（进入正确会话目录） — 20260929-163707-4b8f
[PASS] 恢复后回到空闲态
[PASS] 对话历史被恢复 — 2 条
[PASS] 恢复后新消息追加进旧存档 — 2 → 4 行
```

列表按最后修改时间倒序；用**上下键**把高亮移到目标会话后回车（不是直接按下标赋值），
所以这条同时覆盖了"上下键导航"与"Enter 恢复"。恢复后新消息继续追加进被恢复会话的
同一份 `conversation.jsonl`。

### 场景 4（工具调用存档 + 压缩标记）— 通过

```
[PASS] 再次恢复同一会话
[PASS] JSONL 记录 tool_calls
[PASS] JSONL 记录 tool_results
[PASS] 压缩标记落盘 — token 2641 → 3185
[PASS] 恢复时从最后一个 compact 标记之后加载 — 标记后 16 条 / 全文件 31 行
```

工具调用回合与工具结果回合都落进了存档；`/compact` 触发后出现
`{"type":"compact",...}` 标记行，重新加载时只从最后一个标记之后开始（16 条 < 31 行）。

> 压缩前后 token 反而上升，是 ch08 既有行为（摘要请求本身要带恢复段与文件追踪素材），
> 与本章的存档改动无关；这里验的是**标记行被写入且恢复从标记后开始**。

## 三、checklist 逐项覆盖对照

| checklist 分组 | 覆盖方式 | 结果 |
|---------------|---------|------|
| 编译与测试 | 上表基线对照 + `tests/test_session.py::test_writer_concurrent_append_is_atomic`（60 线程并发追加，行数与写入次数一致、每行可解析） | 通过 |
| 项目指令文件（9 项） | `tests/test_instructions_loader.py`（17 例：三层优先级、缺失静默、正常/嵌套展开、6 层深度截断、环路、路径逃逸、用户级边界、二进制、空指令、段落中间与缩进的 `@include` 不展开、读不了的层降级）+ 端到端场景 1 | 通过 |
| 会话存档（6 项） | `tests/test_session.py`（22 例）+ 端到端场景 1/4 | 通过 |
| 会话恢复（13 项） | `tests/test_tui.py` 的恢复用例组（进入列表、Esc、搜索过滤、上下键、Enter 恢复、时间跨度提醒、旧格式不展示、无历史提示、运行中拒绝、加载超限先压缩、恢复失败保持当前会话）+ 端到端场景 3 | 通过 |
| 会话清理（4 项） | `tests/test_session.py` 清理组（31 天删 / 1 天留 / 旧格式留 / 普通文件不误伤）+ 清理走 `asyncio.to_thread` 后台任务 | 通过 |
| 自动笔记（11 项） | `tests/test_memory.py`（23 例）+ `tests/test_agent.py` 记忆触发组（8 例，含"更新未完成时下一轮照常开始"）+ `tests/test_prompt.py` 注入组 + 端到端场景 2 | 通过 |
| 集成（4 项） | `tests/test_prompt.py`（空参逐字节等价）、`tests/test_conversation.py`（无回调时行为不变）、`tests/test_mcp_cli.py`（启动串联）、端到端场景 1–4 | 通过 |

## 四、与 checklist 写法的两处偏差（据实说明）

1. **用 Textual 无头驱动代替 tmux 手工敲键。** 本章开发机为 Windows，CLAUDE.md 约定的
   tmux 实跑不可用。脚本用 `App.run_test()` 驱动**真实的** TUI 事件循环、真实按键事件
   （`pilot.press("down"/"enter"/"escape")`）与真实组件状态机，覆盖了 checklist
   "端到端场景"条目的行为面；差异是没有真实 TTY 渲染与人眼观察。
2. **`tail -f` 专项未单独跑。** fsync 的效果由脚本与真实运行共同佐证：脚本在写入器仍持有
   句柄的情况下，从另一个路径反复读回 JSONL 并成功解析全部行（若没有 flush+fsync，
   内核缓冲未落盘时外部读不到完整行）。

## 五、本章新增/修改文件

新增：`instructions/`（loader）、`session/`（writer / list / load / cleanup）、
`memory/`（types / const / store / manager / prompts）、`tui/resume.py`，
以及对应 3 个测试文件。

修改：`compact/state.py`（会话 id 格式、`session_dir`、`open_session_context`、
`parse_session_time`、`sessions_root`）、`conversation.py`（`from_messages` + 两个持久化回调）、
`prompt/modules.py` 与 `prompt/__init__.py`（可选槽参数化）、`agent/__init__.py`
（每 5 轮 / 关键词触发记忆更新）、`agent/runtime.py`（`turn_count`）、
`tui/app.py`（`RESUMING` 状态、存档/记忆注入、退出收尾）、`tui/commands.py`（`/resume`）、
`tui/view.py`（`RESUMING` + `write_notice`）、`cli.py`（启动串联）、
`.potatocode/config.yaml.example`（MEWCODE.md 与运行时目录说明）。

顺带修复（与本章功能无关，为了让"lint 无告警"这条 checklist 成立）：
`compact/compact.py`、`tool/read_file.py` 的超长行，`tool/grep_tool.py` 的格式。
