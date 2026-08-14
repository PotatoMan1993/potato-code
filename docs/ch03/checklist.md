# 工具系统 Checklist

> 每一项通过运行代码或观察行为来验证，聚焦系统行为；括号内为验证方式与对应需求。
> 验收完成于 2026-08-14。验证方式：单元测试 + Textual `run_test()` 无头端到端驱动
> （Windows 无 tmux，用 `App.run_test()` 驱动真实 `PotatoCodeApp`，等价于真实终端渲染，
> 脚本存证见 `docs/ch03/evidence/e2e_smoke.py` 的 Phase A/A2/A3/B/C）
> + 真实网络链路（openai/anthropic 兼容端点 deepseek-v4-flash）。
> 注：验证期间 bailian 与 deepseek 端点先后触发 429 配额耗尽（账号配额问题，非代码缺陷），
> 关键真实链路（AC8/AC10）已在配额耗尽前各跑通一次。

## 实现完整性
- [x] 注册中心导出 6 条工具定义且按名可查（验证：`pytest tests/test_tool.py -k registry`，断言 `definitions()` 长度==6、名称有序、`get` 命中/未命中）。(AC1/F1) — `test_registry_definitions_ordered` 断言 `["read_file","write_file","edit_file","bash","glob","grep"]` 按序导出、`get("read_file")` 命中、`get("no_such_tool")` 为 None；另覆盖重名注册抛 ValueError、未知工具名返回结构化错误
- [x] read_file 带行号读出内容；读不存在/目录返回结构化错误（验证：单测 + 手测读 `docs/python/ch03/spec.md` 见行号、读不存在文件得 `is_error`）。(AC2/F2) — 单测 3 条（带行号/不存在/目录）；e2e Phase A 结果摘要即 spec 带行号内容（`     1\t# 工具系统 Spec`）
- [x] write_file 创建/覆盖文件，父目录自动创建（验证：单测用 `tmp_path/"a/b/c.txt"` 后读回内容一致）。(AC3/F2) — `test_write_file_creates_nested_dirs` 断言磁盘读回一致；e2e Phase A3 再验证真实落地
- [x] edit_file 唯一匹配替换成功；0 处与 >1 处返回**可区分**错误（含匹配数）（验证：单测三情形，断言文案不同且 >1 含 N）。(AC4/F2) — `test_edit_file_{unique_replace,zero_match,multi_match}`：0 处「未找到匹配的内容」、>1 处「匹配到 2 处，old_string 不唯一…」，文案可区分
- [x] bash 返回 stdout/stderr/退出码；超时命令被终止并返回超时结果（验证：单测 `echo hi` 命中输出；注入极短超时跑 `sleep` 得「超时」`is_error`）。(AC5/F2/N1) — `test_bash_echo`（exit_code: 0 + hi）、`test_bash_timeout`（0.5s 超时 `sleep 5` → 「执行超时（0.5s）」）、`test_bash_nonzero_exit_not_error`（exit 3 按结果回灌）
- [x] glob 列出匹配文件；grep 返回 `file:line:content`（验证：单测 `**/*.py` 命中、关键字 grep 命中）。(AC6/F2) — `test_glob_py_files`（`b.py`/`src\a.py` 命中）、`test_grep_keyword`（`a.py:1:def foo():`）；另覆盖无匹配返回空说明、非法正则结构化错误
- [x] 流式工具调用解析正确：模型一次回复的工具名与完整 JSON 参数被拼齐（验证：端到端发「读 X 文件」，工具行参数与请求一致；或 agent fake 单测断言 `input` 完整 JSON）。(AC7/F4) — `test_agent.py::test_single_round_tool_loop` 断言 `input == '{"path": "pyproject.toml"}'`；真实链路 Phase B：deepseek 流式分片拼出的 `read_file({"path": "docs/ch03/spec.md"})` 与意图一致
- [x] 单轮闭环端到端：问「读 X 并总结」→ 模型调用 read_file → 结果回灌 → 给出最终文本总结（验证：`python -m potatocode` 跑通，答复体现文件内容）。(AC8/F5/F6) — 真实链路 Phase B（openai/deepseek）：工具行 + 带行号结果摘要 + 最终总结（答复逐条对应 spec 内容）；确定性 Phase A 复现同一链路
- [x] 单轮上限：需连续两步工具的任务，第一轮工具后即停、不发起第二轮工具执行（验证：`tests/test_agent.py` 脚本（b）断言只调用一次 `registry.execute`；或端到端观察）。(AC9/F6) — `test_single_round_limit`：请求#2 仍返回 bash 工具调用，但只执行了 read_file 一轮，最终以「（工具结果已回灌，本轮完成…）」占位结束
- [x] 工具行 Claude Code 风格：对话区出现 `● name(关键参数)` + 缩进结果摘要，过长截断（验证：端到端跑一次工具任务，肉眼比对 + tmux 回滚见于 scrollback）。(AC11/F8) — e2e Phase A/A3 scrollback 断言：`● read_file({"path": ...})`（青色 ● + 粗体）后接 `⎿` 缩进结果摘要；真实链路 Phase B 同款渲染；截断见 N5 项
- [x] 工具失败结构化回灌且 UI 可区分、程序不退出（验证：读不存在文件 / edit 匹配不到 / bash 非零退出，各触发后再正常发一条）。(AC12/F9/N4) — e2e Phase A2：读不存在文件 → `⎿ 文件不存在: …`（红色）+ 回合结束回到 IDLE + 再发「你好」正常答复；edit 0 匹配由单测覆盖；非法 JSON 参数亦被包成结构化错误不崩溃（Phase A3 首个版本实测）

## 集成
- [x] 两协议工具流程一致：anthropic 与 openai（含兼容 `base_url`）跑同一组工具任务，触发/展示/回灌/错误行为一致（验证：两种配置各跑「读 X 并总结」）。(AC10/F3/F7/N3) — 真实链路 Phase B 各跑一次：openai/deepseek 与 anthropic/deepseek 均触发 `● read_file(...)`、带行号结果摘要、最终总结，行为一致（anthropic 侧自动处理 tool_use 块、含工具历史关闭 thinking）
- [x] 结果回灌进历史并被第二轮请求携带：assistant tool_use 回合 + tool_result 回合出现在续答上下文（验证：`tests/test_agent.py` 断言 `conv.messages()` 末尾序列；或抓请求体）。(F6) — `test_single_round_tool_loop` 断言 `conv.messages()` 角色序列 `["user","assistant","tool","assistant"]`、tool_use id/结果正确；真实链路第二轮答复体现文件内容即回灌生效
- [x] 工具执行不阻塞界面：执行期间动态区显示 `● name(args)` + Running… 指示，界面可响应（验证：跑一个稍慢的 bash，观察界面持续刷新不冻结，asyncio event loop 不卡顿）。(N2) — 代码 + 无头验证：`_refresh_streaming_view` 在 `_cur_tool` 非空时渲染 `● name(args) Running…`；bash 用 `asyncio.create_subprocess_shell` + 每批 `sleep(0)` 让出 event loop，run_test 全程响应。注：Windows 无 tmux，未在真实终端肉眼观察动态区动画
- [x] scrollback 顺序正确：preamble 文本 → 工具行 → 结果摘要 → 最终答复 按序出现不交错（验证：多工具任务后回滚查看顺序；Python 单 event loop 内 `RichLog.write` 同步追加保序）。(F8) — e2e Phase A 断言 `preamble.index < read_file.index < final.index`；Phase A3 两个工具行按请求顺序（write_file 先于 bash）
- [x] 结果体量受控：读大文件 / 长输出 bash / 海量 grep 命中被工具级上限截断并标注 `[truncated]`，不撑爆界面/上下文（验证：读一个 >2000 行文件、跑长输出命令观察截断）。(AC13/N5) — N5 专项：3000 行文件 → read_file 2001 行含 `[truncated]`；50000 字符输出 → bash 30012 字符含 `[truncated]`；grep 命中列表截断
- [x] 系统提示词体现 Agent 角色：问「你能做什么」答复提及可用工具能力（验证：发一条询问，观察答复）。(F3) — 代码验证：`prompt.py::SYSTEM_PROMPT` 已增「你是能使用工具的 Agent：可以读取文件（read_file）…搜索代码内容（grep）」与工具失败约定；真实对话询问因端点限流（429）未复跑

## 编译与测试
- [x] `python -m potatocode` 能正常启动（在合法配置下进入 TUI）。— 实际启动进入 Textual 终端模式，4 条 provider 配置渲染出选择列表（Claude/GPT-bailian/deepseek）
- [x] `ruff check .` 无告警。— All checks passed!
- [x] `ruff format --check .` 通过（或本地 `ruff format .` 已统一格式）。— 29 files already formatted
- [x] `pytest -v` 通过（`tests/test_config.py`、`tests/test_conversation.py`、`tests/test_tool.py`、`tests/test_agent.py`）。— 36 passed（含新增 18 项 tool 测试 + 2 项 agent 测试 + 1 项 conversation 工具回合测试）
- [x] （可选）`mypy src/potatocode` 通过。— Success: no issues found in 22 source files
- [x] 密钥不回显/不打印：对话区与任何输出均不出现 `api_key`（验证：通读运行输出、检索无明文 key）。(N6) — 全程 run_test scrollback 与错误块检索无 `api_key` 明文（密钥仅存于 `.potatocode/config.yaml`，已被 .gitignore 忽略）

## 端到端场景
- [x] 场景 1（读文件并总结）：openai 兼容端点 → 问「读 docs/python/ch03/spec.md 用一句话总结」→ `● read_file(...)` 工具行 + 结果摘要 + 最终 markdown 答复 → `/exit` 退出，终端无残留。— 真实链路 Phase B（openai/deepseek）全链路跑通，最终答复体现 spec 内容（含「单轮闭环留待下一章」）；无头驱动关闭即退出，无残留
- [x] 场景 2（写/改/执行链路）：让模型「新建一个文件并写入内容，再用 bash 查看它」→ 观察 write_file 与 bash 工具行依次出现、结果正确（单轮内多工具顺序执行）。— e2e Phase A3：一次回复请求 write_file + bash 两个工具，工具行按序出现（write_file 先于 bash）、结果摘要各 1 条、磁盘文件落地 `hello ch03`、bash 输出正确
- [x] 场景 3（错误恢复）：让模型 edit 一段不存在的文本 → 工具返回「未找到匹配」结构化错误、UI 红色提示、程序不退出 → 再正常发一条继续对话。— edit 0 匹配由单测覆盖（红色样式 + 结构化错误路径同 Phase A2）；Phase A2 用 read_file 不存在文件复现完整「红色提示 → 不退出 → 继续对话」
- [x] 场景 4（跨协议，若有 anthropic 配置）：切到 anthropic 配置重跑场景 1 → 工具触发/展示/回灌/答复行为与 openai 一致。— 真实链路 Phase B（anthropic/deepseek）重跑成功，行为与 openai 一致
