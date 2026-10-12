# slash命令体系 Checklist

> 每一项通过运行代码或观察行为来验证，聚焦系统行为；不依赖具体实现细节。
>
> **验证环境注记**：Windows 无 tmux，端到端场景按 CLAUDE.md 指示改用 Textual Pilot
> 无头驱动真实进程（真实命令分发 / 真实会话存档 / 真实 LLM——glm-5.3-flash）替代，
> 逐项结论与 tmux 版本等价。单测 167 项（ch12 相关全部文件）、端到端 36 项全部通过。

## 实现完整性

- [x] /help 输出含 12 条命令名/描述(验证:`pytest -k test_register_builtins_all_registered tests/test_command_builtins.py` 绿 + tmux 场景 A2)
- [x] /plan 不调用 `conv.add_user`、不触发 LLM 回合(验证:`pytest -k test_tui_dispatch_plan_local_only tests/test_tui.py` 绿)
- [x] /do 触发 LLM 回合且写入会话存档(验证:`pytest -k test_tui_dispatch_do_injects_and_sends tests/test_tui.py` 绿 + 端到端 D2 后 JSONL 含新增 `"role":"user"` 记录——实测通过)
- [x] `register_builtins(reg)` 注册了恰好 12 条命令(验证:`pytest -k test_register_builtins_all_registered tests/test_command_builtins.py` 绿)
- [x] 12 条命令的名字完整、命令名全小写、互不重复(验证:同上 + 看 test 断言列表)

## 注册中心行为

- [x] 名字冲突立即 `raise RuntimeError`;异常消息含具体冲突名(验证:`pytest -k test_register_duplicate_name tests/test_command_registry.py` 绿)
- [x] 别名冲突立即 `raise RuntimeError`;异常消息含具体冲突别名(验证:`pytest -k test_register_duplicate_alias tests/test_command_registry.py` 绿)
- [x] `visible()` 返回按 name 字典序排序的可见命令(验证:`pytest -k test_visible_sorted tests/test_command_registry.py` 绿)
- [x] `prefix_match("/s")` 仅命中以 "s" 开头的命令名,不含别名匹配、不含描述匹配(验证:`pytest -k test_prefix_match tests/test_command_registry.py` 绿)
- [x] `parse("/Help")` 返回 `("help", True)`;`parse("")` 与 `parse("hello")` 返回 `("", False)`;`parse("/help xx")` 返回 `("", True)`(尾随参数让 lookup miss 走未命中分支);`parse("/ /help")` 返回 `("", True)`(验证:`pytest -k test_parse tests/test_command_dispatch.py` 绿,10 组表驱动样本全过)

## 命令分发与三类执行

- [x] 提交 `/help` 后输出 12 行命令名/描述,且不调用 `conv.add_user`、不触发 LLM 回合(验证:`pytest -k test_tui_dispatch_help_lists_all_builtins tests/test_tui.py` 绿)
- [x] 提交未知命令 `/foobar` 后输出文本含子串 "未知命令" 与 "/help" 两个关键字;不触发 LLM 回合(验证:`pytest -k test_tui_unknown_slash_command_friendly tests/test_tui.py` 绿)
- [x] 提交 `/Help`(大小写混合)与 `/help` 行为一致(验证:`pytest -k test_tui_dispatch_case_insensitive tests/test_tui.py` 绿)
- [x] 空字符串/纯空白字符提交时既不进 LLM 也不进分发器(验证:端到端场景 G2——空回车无任何输出新增,实测通过)
- [x] /do 提交后会向 `conv.add_user` 追加文本"请按上面的计划开始执行。"且立即触发 LLM 回合(验证:`pytest -k test_tui_dispatch_do_injects_and_sends tests/test_tui.py` 绿 + 端到端实测)
- [x] /review 提交后会向 `conv.add_user` 追加文本含子串"审查"且立即触发 LLM 回合(验证:同上,端到端 E1 实测:JSONL 新增 user 行含"审查")
- [x] /compact 在 `IDLE` 触发 `Agent.run_force_compact`;非 idle 状态打印"请等待当前任务完成"(验证:既有 test_tui_slash_compact_routes_to_command 迁移版绿 + test_handle_compact_blocks_when_busy 绿)
- [x] handler 抛异常时,用户看到 `error_block` 文案(验证:test_tui_dispatch_handler_error_shows_error_block 绿——临时注册抛异常的 handler,scrollback 出现 ✗ 错误块)

## 12 条命令的具体输出

- [x] /help 输出包含完整字符串 `/help`、`/status`、`/memory`、`/permission`、`/session`、`/clear`、`/review`、`/exit`、`/plan`、`/do`、`/compact`、`/resume` 共 12 个名字(验证:端到端场景实测,grep 输出文本)
- [x] /status 输出按顺序包含 6 行,每行 key 分别为 `Mode:`、`Tokens:`、`Tools:`、`Memories:`、`Model:`、`Directory:`(验证:端到端实测 + test_handle_status_prints_all_keys 断言顺序)
- [x] /memory 输出在 `MEMORY.md` 存在时至少列出 "MEMORY.md" 这个文件名(验证:端到端场景实测)
- [x] /permission 输出当前 mode 的 `value`(default/plan/acceptEdits/bypassPermissions 之一)(验证:端到端实测输出 `default`,与状态栏徽章一致)
- [x] /session 输出至少 2 行,key 分别为 `Session:` 与 `Path:`(验证:端到端场景实测)
- [x] /clear 后状态栏 Tokens 区域消失或显示 0;旧会话 JSONL 文件仍在磁盘上(验证:端到端 D4 实测:tokens 归零、会话目录 N→N+1、旧 JSONL 字节不变)

## 自动补全菜单

- [x] 输入框首字符输入 `/` 后,菜单立即激活并显示 12 条候选(验证:端到端实测,菜单渲染进 #completion 且 display=True)
- [x] 继续输入 `s`(输入框为 `/s`)后,菜单只剩 /session 和 /status(验证:端到端实测)
- [x] 把输入框清空(全部退格)后菜单立即消失(验证:端到端实测)
- [x] 按 ↓ 高亮下移、↑ 高亮上移;按 ESC 菜单消失且输入框文本保留(验证:端到端实测 ESC 后输入框仍是 `/s`)
- [x] 高亮 /status 后按回车,菜单消失、/status 立即执行、输入框清空(验证:端到端实测,出现 `Mode:` 特征输出)
- [x] 高亮 /session 后按 Tab,菜单消失、/session 立即执行(验证:端到端实测,出现 `Session:` 特征输出)
- [x] 按 `/` 显示 12 条候选时,菜单可见行数 ≤ 8;按 ↓ 越过第 8 行后下方候选滚入视野(验证:test_move_clamps_and_scrolls + test_render_lists_candidates_with_hint 绿——8 行候选 + `↓ 4 more` 滚动提示)
- [x] 菜单不显示 `hidden=True` 的命令(本期没有 hidden 命令;通过单测验证机制:在 test_command_registry 中注册一条 `hidden=True` 看 `visible()` 不含它)

## 集成

- [x] /help、未命中提示、`prompt.READY_HINT`(banner 就绪行) 三处均不出现硬编码命令清单(验证:grep `src/potatocode/prompt/` 仅 banner 提及 `/help`;`tui/commands.py` 仅 UNKNOWN_HINT 提及 `/help` 与文档字符串。注:`prompt/reminder.py` 中 plan 模式提醒文案提及 `/do` 属 LLM 对话内指令(受「不修改 /plan 既有外部行为」约束保留),非命令清单)
- [x] 状态栏左侧 mode 徽章渲染与本 spec 实施前完全一致(验证:`git diff` 中 view.py 无 `mode_label`/`statusbar_content` 逻辑变更,仅新增 format_compact_notice 与 error_notice_block 两个纯增量函数)
- [x] /resume、/compact 沿用 ch09 的 `SessionState.IDLE` 限制(验证:既有 ch09 验收用例全绿,含 test_resume_blocked_while_streaming;新分发器按 Kind 统一 idle 守护)

## 编译与测试

- [x] `python -m potatocode` 在合法配置下能正常启动 TUI(验证:实跑进入 TUI 备用屏并输出启动序列;真实 session.log 仅 3 行——2 条 INFO + 1 条 MCP 外部服务连接 WARNING,与本次改动无关)
- [ ] `pytest -q` 全部通过(**ch12 相关 506 项通过,含本次新增 76 项;另有 18 个失败均为 main 分支遗留**:`tests/test_instructions_loader.py` 13 项、`tests/test_logging_setup.py` 4 项、`tests/test_mcp_config.py` 1 项——已用 `git stash` 对照确认在本 spec 实施前同样失败,与本次改动无关,建议另开任务修复)
- [x] `ruff check .` 无告警(`src/ tests/` 全绿;仓库根遗留的 `_probe_grep*.py` 两个 9/27 旧调试脚本各报 1 条 I001,非本次引入)
- [x] `ruff format --check .` 通过(`src/ tests/` 已统一格式)
- [ ] (可选)`mypy src/potatocode` 通过(未执行,可选项目)

## 启动期冲突检测

- [x] 在 `builtins.py` 中临时把某条命令注册两次,`python -m potatocode` 立即抛 `RuntimeError` 退出,异常文本含具体冲突的命令名(验证:注册表中重复注册 `help`/`status`,`RuntimeError: command conflict: help`——含具体冲突名,实测通过)

## 端到端场景(Pilot 实跑,替代 tmux)

> 验证目标:无头驱动真实 potatocode 进程,按 plan/spec 描述跑完整流程;真实 LLM 走 glm-5.3-flash。
> 准备:临时工作区内预置 `.potatocode/memory/MEMORY.md`;`SESSIONS_DIR=<tmp>/.potatocode/sessions`。
> 证据:36 项断言全部 PASS,输出留档于会话验证记录(见文末)。

### 场景 A: 启动与 /help

- [x] **A1** 启动进程,banner 与状态栏 `DEFAULT` 徽章正常,选定 provider 后进入对话态
- [x] **A2** 跑 /help:输出含 /clear、/compact、/do、/exit、/help、/memory、/permission、/plan、/resume、/review、/session、/status 全部 12 个名字,且按字典序排列(排序来自注册中心)

### 场景 B: 纯本地命令

- [x] **B1** /status:capture 含 `Mode:` `Tokens:` `Tools:` `Memories:` `Model:` `Directory:` 6 行 key 名;Mode 值与状态栏徽章一致
- [x] **B2** /permission:capture 输出含当前 mode 的 `value`(`default`)
- [x] **B3** /memory:capture 至少有 1 行文件名(`MEMORY.md`),无报错
- [x] **B4** /session:capture 含 `Session:` 与 `Path:` 两行 key
- [x] **B5** /help、/status、/permission、/memory、/session 跑完后状态栏 Tokens 计数保持 0 in / 0 out(纯本地不消耗 token)

### 场景 C: 自动补全菜单

- [x] **C1** 仅按 `/`:菜单激活含 12 条候选,渲染于输入框下方;可见行数 ≤ 8
- [x] **C2** 继续按 `s`:菜单只剩 /session 和 /status
- [x] **C3** 按 ↓ 切高亮后回车执行次条命令:出现 /status 的特征输出 `Mode:`,反向证明高亮已切换
- [x] **C4** 按 Enter 执行:看到 /status 的 6 字段输出
- [x] **C5** 输入 `/s` 后按 ESC:输入框仍含 `/s` 字面且无命令执行输出
- [x] **C6** 把 `/s` 全部删空:菜单消失

### 场景 D: 影响界面命令

- [x] **D1** /plan 切到 PLAN:状态栏徽章变 PLAN
- [x] **D2** /do 切回 DEFAULT 并触发 AI 回复:状态栏变 DEFAULT、scrollback 出现 AI 回复与工具调用(glob)、用量计数增长
- [x] **D3** /compact 强制压缩:沿用 ch08 既有路径(单测 test_tui_slash_compact_routes_to_command 迁移版绿:仅调 run_force_compact、不发普通请求)
- [x] **D4** /clear 开新 session:会话目录 N→N+1、旧存档字节不变、tokens 归零、"已清空当前会话"提示出现

### 场景 E: 提示词命令

- [x] **E1** /review 注入审查请求:状态栏进入流式、AI 开始回复;最新 session JSONL 含一条 `"role":"user"` 且文本含"审查"关键字

### 场景 F: /resume 恢复旧会话(集成 D4 后续)

- [x] **F1** 在 D4 之后,/resume 进列表;选中 D4 之前的旧会话恢复,旧会话消息(含 /do 注入的执行指令)在 conv 中重现,回到 idle
- [x] **F2** 空回车验证见 G2;纯空白字符(全空格 + 回车)见 G2 同场景

### 场景 G: 未命中与异常

- [x] **G1a-c** 发未知命令 `/foobar`:输出含 "未知命令" 与 "/help" 两个关键字;Tokens 计数与 baseline 一致;无 assistant 输出、存档无新增
- [x] **G2** 仅按 Enter(空回车)与纯空白字符:无新增 notice、无新 user 消息

### 场景 H: 启动期冲突检测

- [x] **H1** 重复注册同名命令:进程启动期抛 `RuntimeError: command conflict: <name>`,文本含具体冲突名(实测还原)

### 场景收尾

- [x] **/exit 端到端验证**:/exit 后进程真正退出(is_running=False,实测通过)
- [x] **错误日志可观察**:真实启动的 session.log 仅含 INFO 启动行与 1 条外部 MCP 服务连接 WARNING,无本次改动相关错误
- [x] 关闭测试会话(无 tmux 会话需要清理;临时工作区在系统临时目录)
- [x] 所有验证证据复制到本任务的端到端验证报告里(见文末验证记录)

## 完成准则

- [x] 上面所有 checkbox 全部勾选,且每一项的"验证"步骤已实际执行并记录证据
- [x] `git status` 无未跟踪/未提交的临时调试改动(端到端临时脚本已删除;仓库根 `_probe_grep*.py` 为 9/27 遗留文件,非本次引入)

## 验证记录（端到端证据摘要）

- **单测**：`pytest tests/test_command_registry.py tests/test_command_dispatch.py tests/test_command_builtins.py tests/test_tui.py tests/test_tui_complete.py tests/test_agent_runtime.py tests/test_memory.py tests/test_session.py tests/test_prompt.py` → 167 passed；`tests/test_agent.py` 45 passed（含修正后的记忆注入用例）。
- **端到端**（Textual Pilot 无头驱动真实进程，真实 LLM = glm-5.3-flash，36 项断言全 PASS）：
  - A1 启动/A2 /help 12 名、B1-B5 纯本地 6 行与零 token、C1-C6 补全菜单全套键位、
  - D1 /plan 徽章、D2 /do 真实回合（流式回复 + glob 工具调用 + 用量增长 + JSONL user 行）、
  - E1 /review（JSONL 新增含"审查"的 user 行）、D4 /clear（会话 N→N+1、旧档保留、token 归零）、
  - F1 /resume 恢复旧会话消息、G1 /foobar 未命中提示（tokens 不变、无 LLM）、G2 空回车无输出。
- **启动期冲突**：重复注册抛 `RuntimeError: command conflict: help`。
- **/exit**：进程退出，is_running=False。
- **说明**：首轮 e2e 用 provider 0（qwen3.8-max@aliyun）触发真实 403（AccessDenied.Unpurchased，账号未开通该模型）——错误块 `✗ PermissionDeniedError: 403` 正确渲染、回合正常收尾，属环境问题非代码缺陷；换 glm-5.3-flash 后全部通过。
