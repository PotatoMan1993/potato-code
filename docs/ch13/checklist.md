# Skill 系统 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。操作目录在仓库根 `.`。

## 1. 实现完整性

### 1.1 解析与加载

- [x] `potatocode/skills/parser.py:23` `SkillDef` 含字段 `name / description / prompt_body / mode / model / context / source_path / is_directory`（`grep -n "class SkillDef" potatocode/skills/parser.py` 命中）
- [x] `potatocode/skills/parser.py:36` `parse_frontmatter(raw) -> (dict, str)` 处理 `---\n...\n---` 格式
- [x] `potatocode/skills/parser.py:57` `_validate_meta` 校验 `name` 正则 + `mode in {inline, fork}` + `context in {full, recent, none}`
- [x] `potatocode/skills/parser.py:99` `substitute_arguments(prompt_body, args)` 实现 `$ARGUMENTS` 替换
- [x] `potatocode/skills/loader.py:15` `SkillLoader(work_dir)` 实现两级搜索（`grep -n "PROJECT_SKILLS_DIR\|USER_SKILLS_DIR" potatocode/skills/loader.py` 命中 ≥2 处）
- [x] `potatocode/skills/loader.py:96` `get(name)` 每次重读源文件实现热重载，失败回退 `_cache` 并 `log.warning`
- [x] `potatocode/skills/loader.py:117` `get_source_label(name)` 按 `_project_dir / _user_dir` 前缀返回 `project | user`

### 1.2 Executor

- [x] `potatocode/skills/executor.py:43` 含 `class SkillExecutor` 与 `execute_inline / execute_fork` 两个方法
- [x] inline 调用链：`substitute_arguments` → `agent.activate_skill(name, body)`（`grep -n "activate_skill" potatocode/skills/executor.py` 命中）
- [x] fork 调用链：新 `ConversationManager` → `_build_fork_context(context)` 按 `full / recent / none` 三档装填 → 临时 Agent run → 收集 `StreamText` 到 `LoopComplete`

### 1.3 Agent 集成

- [x] `potatocode/agent.py:317` Agent 含 `self.active_skills: dict[str, str] = {}`
- [x] `potatocode/agent.py:357` `activate_skill(name, prompt_body)` 实现
- [x] `potatocode/agent.py:360` `clear_active_skills()` 实现
- [x] `potatocode/agent.py:363` `set_skill_catalog(catalog)` 实现
- [x] `potatocode/agent.py:400` 主循环每轮调用 `build_environment_context(work_dir, active_skills, skill_catalog, agent_catalog)`
- [x] `potatocode/prompts.py:277` `build_environment_context` 把 `active_skills` 拼成 `## Active Skills` 段；`skill_catalog` 拼到 environment block

### 1.4 LoadSkill 工具

- [x] `potatocode/tools/load_skill.py:21` 含 `class LoadSkill(Tool)`，`name = "LoadSkill"`、`category = "read"`
- [x] `potatocode/tools/load_skill.py:39` `set_loader / set_agent` 注入方法
- [x] `potatocode/tools/load_skill.py:46` `execute` 调 `loader.get → agent.activate_skill` → 返回 `"Skill '' activated. SOP pinned to environment context."`
- [x] `potatocode/skills/executor.py:14` `SYSTEM_TOOL_NAMES = frozenset({"LoadSkill"})` 常量

### 1.5 命令集成

- [x] 每个 skill 自动注册为 `/` 命令，描述末尾含 `[skill]`（`grep -n "\\[skill\\]" potatocode/commands/handlers/skill_register.py` 命中）
- [x] `potatocode/commands/handlers/skill_register.py:18` `register_skill_commands(registry, loader, executor)` 实现；模块级 `_REGISTERED_SKILL_NAMES` 跟踪重复注册
- [x] inline skill 命令 handler 调 `executor.execute_inline` 后再 `ctx.ui.send_user_message(trigger)`
- [x] fork skill 命令 handler 走 `asyncio.create_task(_run_fork)`，结果作为 `add_system_message` 插入
- [x] `potatocode/commands/handlers/skill.py:11` `/skill list | info  | reload` 子命令分发
- [x] `potatocode/commands/handlers/clear.py:19` `handle_clear` 调用 `ctx.agent.clear_active_skills()`

### 1.6 远程安装

- [x] `potatocode/skills/install.py` 含 `parse_skill_url` 支持三种 URL 格式
- [x] `install_skill(src, install_root)` 走 GitHub Contents API 递归拉取，atomic rename
- [x] 限额常量 MAX_FILE_SIZE / MAX_TOTAL_SIZE / MAX_FILE_COUNT / MAX_RECURSION_DEPTH
- [x] 下载完没有 SKILL.md 或 skill.yaml 时拒绝安装并清理 staging
- [x] `InstallSkillTool` name = InstallSkill，category = write
- [x] 执行成功后调 loader.reload() + on_installed 回调

## 2. 接入完整性（杜绝死代码）

- [x] `grep -rn "SkillLoader" potatocode/app.py` 命中 ≥2 处（import + 实例化）
- [x] `grep -rn "activate_skill" potatocode/` 命中 Agent 方法定义 + Executor + LoadSkillTool 三处调用
- [x] `grep -rn "clear_active_skills" potatocode/` 命中 `/clear` handler 调用 + Agent 方法定义
- [x] `grep -rn "LoadSkill\|\"LoadSkill\"" potatocode/` 命中 tool 定义 + app 注册 + 至少 1 个测试
- [x] `grep -rn "SkillExecutor\|register_skill_commands" potatocode/` 命中 app.py 注册 + handler 模块
- [x] `grep -rn "execute_inline\|execute_fork" potatocode/skills/` 命中 Executor 定义 + handler 调用
- [x] `grep -rn "loader.get\|SkillLoader.get" potatocode/tools/load_skill.py` 命中 1 处
- [x] `potatocode/app.py:556` 存在 `self.skill_loader` / `self.skill_executor` / `self._load_skill_tool` 字段
- [x] `potatocode/app.py:885` `CommandContext.config` 字典塞入 `"skill_loader"` 与 `"skill_executor"` key

## 3. 编译与测试

- [x] `cd . && ruff check potatocode/skills potatocode/tools/load_skill.py` 无 error
- [x] `cd . && pytest tests/test_skills.py -q` 全部通过
- [x] `cd . && pytest tests/test_agent.py -q` 全部通过
- [x] `cd . && python -c "from potatocode.skills.loader import SkillLoader; l = SkillLoader('/tmp'); l.load_all(); print('SkillLoader OK')"` 不报错
- [x] `cd . && python -c "from potatocode.tools.load_skill import LoadSkill; t = LoadSkill(); print(t.name, t.category)"` 输出 `LoadSkill read`

## 4. 端到端验证（手动操作 TUI）

> 启动命令：`cd . && python -m potatocode`

- [x] 创建测试 skill 目录 `.potatocode/skills/test-skill/SKILL.md`，写一个简单 inline SOP（`name: test-skill / description: A test skill / mode: inline`，body 写 `Echo hello`）
- [x] 启动后输 `/help`，看到 `/test-skill [skill]` / `/skill` 都列出
- [x] 输 `/test-skill`，看到 SOP 加载到 env context
- [x] 编辑 `.potatocode/skills/test-skill/SKILL.md` 改一行后**不重启** TUI，再 `/test-skill`，看到新行进入 prompt（热重载验证）
- [x] 自然语言触发 `LoadSkill({name: "test-skill"})`，environment 段里出现该 skill 的 SOP
- [x] 输 `/clear`，立即输任意消息，environment 段里**不再出现** `## Active Skills`
- [x] 创建 `.potatocode/skills/bad.md` 故意写错 frontmatter，启动日志出现 `Skipping ... skill 'bad': ...` warning，其他 skill 仍正常加载
- [x] LoadSkill 工具调用时**不**弹权限提示（`category=read`）

## 5. 文档

- [x] `docs/skills/spec.md` 更新到课程全量版（不是验收版）
- [x] `docs/skills/tasks.md` 11 个任务全部勾上
- [x] `docs/skills/checklist.md` 全部条目勾上
- [x] commit 信息：`feat(skills): full skill system per course design (python) [spec/tasks/checklist closed]`
---

## 验收附注（ch13 实施对照）

**路径映射**：本仓库为 `src/` 布局包结构，checklist 中的扁平路径按下表对应实现
（行号为实际位置，与文档锚点行号不一一对应）：

| 文档锚点 | 实际位置 |
|---|---|
| `potatocode/skills/parser.py` | `src/potatocode/skills/parser.py` |
| `potatocode/skills/loader.py` | `src/potatocode/skills/loader.py` |
| `potatocode/skills/executor.py` | `src/potatocode/skills/executor.py` |
| `potatocode/skills/install.py` | `src/potatocode/skills/install.py` |
| `potatocode/tools/load_skill.py` | `src/potatocode/tool/load_skill.py` |
| `potatocode/tools/install_skill.py` | `src/potatocode/tool/install_skill.py` |
| `potatocode/agent.py`（Agent） | `src/potatocode/agent/__init__.py` |
| `potatocode/prompts.py`（build_environment_context） | `src/potatocode/prompt/skills_block.py` |
| `potatocode/app.py`（装配） | `src/potatocode/cli.py` + `src/potatocode/tui/app.py` |
| `potatocode/commands/handlers/skill_register.py` | `src/potatocode/command/skill_register.py` |
| `potatocode/commands/handlers/skill.py` | `src/potatocode/command/builtin_skill.py` |
| `potatocode/commands/handlers/clear.py` | `src/potatocode/command/builtin_ui.py`（handle_clear） |

**§1.4**：`category="read"` 在本仓库对应 `Tool.read_only() -> True`（权限引擎按
read_only 判定并发与放行），故 `LoadSkill` 打印为 `read_only=True` 而非字面
`category`；`is_concurrency_safe` 无对应协议字段，由 read_only 推导。

**§1.5**：本仓库命令 handler 只接收 `UI`（无 `CommandContext.config` 字典），
loader/executor 以闭包捕获注入 handler；fork 结果按 plan.md 执行器设计以
assistant 消息回流（`ui.append_assistant_message`），非字面 `add_system_message`
（Conversation 无 system 角色回合）。

**§4**：Windows 环境无 tmux/交互终端，端到端验收以 Textual `run_test()` 无头驱动
真实 `PotatoCodeApp` 完成（`tests/test_skills_e2e.py`，fake provider 脚本化、零网络）：
/help 列出命令、/skill list|info、/test-skill SOP 进 env、不重启热重载、LoadSkill
意图触发后下一轮 env 出现 SOP、/clear 后 env 不再出现 Active Skills；坏
frontmatter 的 `Skipping ... skill` warning 由 tests/test_skills.py 的 pkg_logs
夹具验证。真实 TUI 的手动复核留待用户进行。

**§5**：commit message 按 CLAUDE.md 约定使用中文（覆盖本文件预置的英文建议）。
