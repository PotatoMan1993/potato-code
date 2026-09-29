# 运行日志落盘 Checklist

> 每一项通过运行代码或观察行为来验证，聚焦系统行为。
>
> **改动前基线（2026-09-29 实测，`git stash` 后的 HEAD 工作区）**：`pytest` = `2 failed, 341 passed, 2 skipped`；
> `ruff check .` = 6 项错误；`ruff format --check .` = 3 个文件待格式化；`mypy` = 2 项错误。
> 这四项全部位于本章未触碰的文件（清单见 `evidence/verification.md` 第一节），
> 故 AC12 的判定标准是"**除这些既有项外无新增**"，而非"零告警"。
>
> **结果：29/29 通过**（详见 `docs/ch10/evidence/verification.md`，端到端脚本 14/14 通过）。
> 逐项证据见各条末尾；S1 中"真实 TUI 交互"与"发起一轮对话"两项仅有间接证据，
> 未做实测，已在 `evidence/verification.md` 第六节如实说明。

## 实现完整性

- [x] **会话日志文件生成**：每会话的日志落在 `.potatocode/sessions/<session_id>/session.log`，与同会话的 `tool-results/` 同目录。（AC1/F1）— 实测：`.../1790656963-1c7fd5f6/session.log`，与 `spill_dir` 同父目录；`test_new_session_context` 同源字段由 `session.log` / `父目录一致: True` 验证。
- [x] **启动早期事件被覆盖**：在配置加载之前发生的事件（配置错误、MCP 连接失败、权限引擎降级）出现在该会话日志中。（AC2/F2）— 实测：S2 子进程退出码 1 且日志含"配置错误"；S4 日志含 `connect server unreachable failed`。
- [x] **默认详细度含信息级**：默认启动时，信息级记录写入日志文件。（AC3/F3）— 实测：S1 日志含 `INFO potatocode.demo — 本轮会话已就绪`。
- [x] **默认不记调试级**：默认启动时，调试级记录不出现在日志文件。（AC4/F3）— 实测：S5 默认级别下 `这条只在提升详细度后才应出现` 不出现。
- [x] **详细度可临时调整**：设置环境变量后可提升到调试级，且同一路径下调试级记录出现；无需改动代码或配置文件。（AC4/F4）— 实测：S5 设 `POTATOCODE_LOG_LEVEL=DEBUG` 后同一记录出现；`test_env_var_raises_level_without_code_change` 另作覆盖。
- [x] **第三方日志被隔离**：让依赖库（LLM SDK / HTTP 客户端 / MCP SDK）产生日志，这些记录不出现在会话日志中——包括详细度已提升到调试级的情形。（AC5/F5）— 实测：调试级下注入 `httpx` 告警与 `anthropic._client` 调试，两者均未入文件；文件内来源字段全部为 `potatocode.cli` / `potatocode.demo`。
- [x] **异常堆栈落盘**：以异常级记录的调用，其完整堆栈写入日志文件。（AC6/F6）— 实测：S3 走真实 `manage_context`，日志含 `上下文压缩失败` + `Traceback` + `prompt_too_long`。
- [x] **MCP 输出已统一**：MCP 模块的告警与连接信息走日志出口，进入会话日志。（AC7/F7）— 实测：S4 的连不上告警在日志中；`manager.py` / `config.py` / `tool.py` 已无任何 `print` 与 `sys.stderr`。
- [x] **日志格式四要素**：每行含时间、级别、来源模块、消息；中文内容无乱码。（AC8/F8）— 实测：样本行 `2026-09-29 12:42:50 WARNING potatocode.mcp.manager:105 — connect server ...`；`test_line_has_four_elements` 用正则整行匹配；`test_chinese_round_trips_as_utf8` 校验字节为合法 UTF-8。
- [x] **退出不丢尾部**：进程正常退出后，日志文件末尾是完整的成行记录，无截断。（AC9/F9）— 实测：S1 断言文件以换行结尾且非空；`close_logging` 在 `finally` 中执行，S2 的提前 `return 1` 路径同样收尾。

## 边界与降级

- [x] **目录不可写不阻断启动**：日志目录不可写时，应用仍能启动并进入交互，日志装配失败被降级处理。（AC10/N4）— `test_unwritable_path_degrades_without_raising`：以目录充当日志路径，`setup_logging` 返回 `None` 且不上抛；`close_logging(None)` 安全。
- [x] **多实例互不串扰**：并发启动两个实例，各自日志中不含对方会话的记录。（AC11/N5）— 实测：S6 两个工作区会话 id 分别为 `1790656965-fea0dfc4` / `1790656965-242b7429`，互检均不含对方 id。
- [x] **重复装配不叠加**：同一进程内连续装配两次日志出口，记录不重复、无多余文件句柄。（N2 相关）— `test_repeated_setup_does_not_duplicate`：连装两次后记一条，文件内该条只出现 1 次。
- [x] **不新增泄漏面**：日志文件中不含 API 密钥。（N3）— 实测：让依赖库打日志后文件内来源全部为 `potatocode.*`，会带出请求体的第三方日志被结构性排除；13 处既有调用点的参数为路径/计数/异常对象，均不含密钥。另：端到端脚本全程使用占位密钥。

## 集成

- [x] **会话标识一致性**：同一次启动中，日志文件的所属会话与运行时的会话是同一个。（F1/F2）— 实测：`cli._amain` 把启动时建好的会话同时交给 `setup_logging` 与 `new_runtime(session=...)`；`new_runtime` 复用同一对象的单测见 T4 验证（`复用同一会话: True`）。
- [x] **既有的 13 处日志调用未被改动**：原有调用的位置、级别与消息文本保持不变，只是现在有了出口。（不做的事）— 实测：`git diff` 中不含任何对既有 `logger.*` 行的删除或修改（全部改动行均为新增）；HEAD 上按级别统计为信息级 6、告警级 6、异常级 1。
- [x] **终端提示未被移除**：配置错误与权限降级在终端上仍然可见，同时额外进了日志。（不做的事）— 实测：S2 的 stderr 仍为 `配置错误: 配置文件不存在: .potatocode/config.yaml`，日志中另有同名记录。
- [x] **不上屏**：日志不向终端输出，TUI 界面不因日志写入而花屏。（不做的事/N1）— 实测：抓取一次真实启动的 stderr，内容仅为依赖库经兜底处理器输出的既有告警，**不含任何本项目日志行**；MCP 告警从 stderr 消失、改在文件里出现。

## 编译与测试

- [x] 格式检查通过（本章新增/修改的 10 个文件）— `uv run ruff format --check <这些文件>` = `39 files already formatted`。
- [x] 静态检查通过（本章新增/修改的 10 个文件）— `uv run ruff check <这些文件>` = `All checks passed!`。
- [x] 全量测试通过（`uv run pytest`）— `2 failed, 356 passed, 2 skipped`。
- [x] 类型静态检查通过（`uv run mypy src/potatocode`）— 2 项错误，与 HEAD 基线逐条相同，未新增。
- [x] **测试基线不回归**：除改动前既有的 2 个失败项外无新增失败。（AC12）— 实测：改动前 `2 failed, 341 passed`，改动后 `2 failed, 356 passed`，失败项为同一对，通过数 +15。
- [x] **MCP 测试的捕获通道已迁移**：原先依赖标准错误流断言的用例改为经日志收集器断言，断言语义等价。（F7）— 10 处 `capsys` → `pkg_logs` 全部迁移（`test_mcp_config.py` 5、`test_mcp_manager.py` 3、`test_mcp_tool.py` 2），三文件全绿；断言子串仅去掉 `[mcp] warn: ` 前缀、正文不变。

## 端到端场景

- [x] **S1 正常会话落盘**：启动一次真实会话 → 会话目录下出现 `session.log`，其中含启动记录与信息级记录。（AC1）— 实测：退出码 0，日志首行 `启动：session=1790656963-1c7fd5f6 workspace=...`。**注意**：真实 TUI 交互与"发起一轮对话"未实测，见 `evidence/verification.md` 第六节。
- [x] **S2 配置非法仍留证**：用非法配置启动 → 进程以非零码退出，终端给出可读提示，且对应会话日志中存在记录该配置错误的条目。（AC2）— 实测：真实子进程 `exit=1`，stderr 与日志双通道均有。
- [x] **S3 压缩失败可追栈**：构造一次上下文压缩失败 → 日志文件中能找到含完整堆栈的异常级记录。（AC6）— 实测：命中 `上下文压缩失败` / `Traceback` / `prompt_too_long`。
- [x] **S4 MCP 失败可追因**：配置一个连不上的 MCP server 启动 → 日志文件中出现该 server 的连接失败告警，且不再只写标准错误流。（AC7）— 实测：`connect server unreachable failed: [WinError 2] 系统找不到指定的文件。`
- [x] **S5 调试级排查**：以默认级别与调试级各跑同一场景 → 后者日志内容更详细，且前者不含调试级记录。（AC4）— 实测：默认级别 3 行、调试级 4 行，差异恰为那条调试级记录。
