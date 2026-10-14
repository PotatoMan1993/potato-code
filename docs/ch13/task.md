# Skill 系统 Tasks

> 顺序执行。每完成一个任务跑 `ruff check potatocode/skills potatocode/tools/load_skill.py` 与 `pytest tests/test_skills.py -q` 确保通过；接入主流程的任务（T8、T9、T10）做完后立刻补一次端到端验证再进下一项。

## T1: 定义 SkillDef 数据结构与 frontmatter 解析

- 影响文件: `potatocode/skills/parser.py`（新建）
- 依赖任务: 无
- 完成标准: dataclass `SkillDef` 含 `name / description / prompt_body / mode / model / context / source_path / is_directory`；`parse_frontmatter(raw) -> (meta, body)` 处理 `---\n...\n---\n` 格式；`_validate_meta` 校验 `name` 正则 `^[a-z][a-z0-9\-]*$`、`mode in {inline, fork}`、`context in {full, recent, none}`；`SkillParseError` 自定义异常类
- 备注: yaml 库走 `import yaml`（pyyaml）；`substitute_arguments(prompt_body, args)` 简单 `.replace("$ARGUMENTS", args)` 即可

## T2: 实现 SkillLoader 两级搜索与热重载

- 影响文件: `potatocode/skills/loader.py`（新建）
- 依赖任务: T1
- 完成标准:
  - 常量 `PROJECT_SKILLS_DIR = ".potatocode/skills"` / `USER_SKILLS_DIR = "~/.potatocode/skills"`
  - `SkillLoader(work_dir)` 构造时计算 `_project_dir` / `_user_dir`
  - `load_all()` 按 project → user 顺序扫描，首次出现的 name 保留，后续跳过；维护 `_skills` 与 `_cache` 两份字典
  - `_scan_directory(path, source)` 同时处理 `*.md` 与 `/SKILL.md` 两种布局，目录型 skill `is_directory = True`
  - `get(name)` 命中后 `parse_skill_file(source_path)` 强制重读；失败回退 `_cache` 中旧版本并 `log.warning`
  - `get_catalog()` 返回 `[(name, description), ...]`；`get_source_label(name)` 按路径前缀返回 `project | user`
- 备注: 解析失败用 `log.warning("Skipping %s skill '%s': %s", ...)` 不抛出

## T4: SkillExecutor.execute_inline

- 影响文件: `potatocode/skills/executor.py`（继续）
- 依赖任务: T2
- 完成标准: `class SkillExecutor(agent, client, protocol)` 三个属性持有；`execute_inline(skill, args) -> None`：
  - `substitute_arguments(skill.prompt_body, args)`
  - `agent.activate_skill(skill.name, rendered)`
  - 不需要立即调用 LLM，rendered body 钉到 env 后由 command handler 再 `ctx.ui.send_user_message(trigger)` 触发 loop

## T5: SkillExecutor.execute_fork

- 影响文件: `potatocode/skills/executor.py`（继续）
- 依赖任务: T4
- 完成标准: `async execute_fork(skill, args) -> str`：
  - 渲染 prompt
  - 新 `ConversationManager()`
  - 根据 `skill.context` 装填历史：`none` 空 / `recent` 取 `agent._conversation.history` 最近 5 条 user/assistant 消息 / `full` 拼成一段 `"## Previous conversation summary\n\n"` summary 作为单条 user message
  - `fork_conv.add_user_message(rendered)`
  - 局部 `from potatocode.agent import Agent as AgentClass, StreamText, LoopComplete, ErrorEvent`（避免循环 import）构造临时 Agent，沿用 `client / protocol / work_dir / max_iterations / context_window`
  - `async for event in fork_agent.run(fork_conv)`：`StreamText` 追加文本，`ErrorEvent` 追加错误标记，`LoopComplete` break
  - 返回 `"".join(result_parts)`

## T6: Agent 集成 active_skills 与 skill_catalog

- 影响文件: `potatocode/agent.py`（修改）、`potatocode/prompts.py`（修改）
- 依赖任务: 无（与 T1-T5 并行可做）
- 完成标准:
  - `Agent.__init__` 增加 `self.active_skills: dict[str, str] = {}` 与 `self._skill_catalog: str = ""`
  - 方法 `activate_skill(name, prompt_body)` / `clear_active_skills()` / `set_skill_catalog(catalog)`
  - 每轮 `_build_system_message`（或同等位置）调用 `build_environment_context(work_dir, active_skills, skill_catalog, agent_catalog)`
  - `potatocode/prompts.py` 的 `build_environment_context` 拼接：先写 `skill_catalog` 段落，再写 `## Active Skills` 标题 + `### Skill: \n` 子段

## T7: LoadSkill 工具

- 影响文件: `potatocode/tools/load_skill.py`（新建）
- 依赖任务: T2, T6
- 完成标准:
  - `LoadSkill` 继承 `Tool`，`name = "LoadSkill"`、`description` 描述「按需激活 skill」、`params_model = LoadSkillParams(name: str)`、`category = "read"`、`is_concurrency_safe = False`
  - 持有 `_loader` 与 `_agent` 私有属性；`set_loader(loader)` / `set_agent(agent)` 注入器
  - `execute(params)`：
    - 未初始化返回 `is_error=True` 的「LoadSkill not properly initialized」
    - `self._loader.get(params.name)` 为 None 时列出 catalog 返回错误
    - 调 `self._agent.activate_skill(skill.name, skill.prompt_body)`
    - 返回 `"Skill '' activated. SOP pinned to environment context."`

## T8: 接入 app.py —— 加载 + Catalog 注入 + 命令注册

- 影响文件: `potatocode/app.py`（修改）
- 依赖任务: T2, T4, T5, T6, T7
- 完成标准:
  - import `SkillLoader / SkillExecutor / register_skill_commands / LoadSkill`
  - `PotatoCodeApp.__init__` 字段 `self.skill_loader / self.skill_executor / self._load_skill_tool`
  - 先 `LoadSkill()` 实例化注册到 `self.registry`，再构造 `Agent`（保证 registry 已含 LoadSkill）
  - `SkillLoader(work_dir).load_all()` 加载 catalog
  - `load_skill_tool.set_loader(self.skill_loader)` / `set_agent(self.agent)` 注入
  - `SkillExecutor(agent=..., client=..., protocol=...)` 构造
  - 把 catalog 拼成 `"You can use the following Skills:\n\n- : \n...\nIf the user's request matches a Skill, call LoadSkill to activate it."` 调 `self.agent.set_skill_catalog(...)`
  - `register_skill_commands(self.command_registry, self.skill_loader, self.skill_executor)`
  - `CommandContext.config` 字典塞入 `"skill_loader" / "skill_executor"` 供 handler 取用

## T9: 接入 commands —— `/skill` 管理 + skill 命令 + `/clear` 钩

- 影响文件: `potatocode/commands/handlers/skill.py`（新建）、`potatocode/commands/handlers/skill_register.py`（新建）、`potatocode/commands/handlers/clear.py`（修改）、`potatocode/commands/handlers/__init__.py`（注册 SKILL_COMMAND）
- 依赖任务: T8
- 完成标准:
  - `SKILL_COMMAND` 提供 `/skill list | info  | reload` 三档：
    - list：遍历 catalog，每行 `f"  {name:<20} {desc}  [{source}]"`
    - info：拉 `loader.get(name)` 输出完整 frontmatter + path + directory 标记
    - reload：`loader.reload()` 后调用 `register_skill_commands` 重建命令
  - `register_skill_commands(registry, loader, executor)`：模块级集合 `_REGISTERED_SKILL_NAMES` 跟踪本次会话已注册的 skill 命令，再次调用先清掉旧的；inline skill 命令 handler `execute_inline` 后调 `ctx.ui.send_user_message(trigger)`；fork skill 命令 handler 走 `asyncio.create_task(_run_fork)`，结果作为 system message
  - `clear.py` 的 `handle_clear` 增加 `if ctx.agent: ctx.agent.clear_active_skills()`

## T10: 接入主流程 —— 端到端走通

- 影响文件: 无（仅运行验证）
- 依赖任务: T1-T9
- 完成标准:
  - `pytest tests/test_skills.py -q` 全部通过
  - 创建测试 skill 目录 `.potatocode/skills/test-skill/SKILL.md`，写一个简单的 inline SOP（如 `name: test-skill / description: A test skill / mode: inline`，body 写 `Echo hello`）
  - 在仓库根目录手动启动 `python -m potatocode`：
    1. `/help` 列出 `/test-skill`、`/skill` 命令
    2. `/test-skill` 能加载对应 SOP
    3. 编辑 `.potatocode/skills/test-skill/SKILL.md` 改一行后**不重启**再 `/test-skill`，新行进入 prompt（热重载验证）
    4. 自然语言触发 `LoadSkill({name: "test-skill"})`，env-reminder 出现 SOP
    5. `/clear` 后 env-reminder 不再出现旧 SOP

## T11: 单元测试

- 影响文件: `tests/test_skills.py`（新建）
- 依赖任务: T1-T9
- 完成标准: 覆盖
  - parser：valid / missing opening / unclosed / invalid yaml / non-dict / missing name / missing description / invalid name format / invalid mode / nonexistent file / fork mode with context
  - substitute_arguments：with / without args / no placeholder / multiple
  - loader：项目加载 / 用户加载 / 项目覆盖用户 / catalog / get / get_unknown / 热重载成功 / 热重载失败回退 / 目录型识别 / source_label / 失败文件跳过 / reload
  - LoadSkill：load existing / load unknown / 未初始化 / `category="read"`
  - Agent 集成：`build_environment_context` 含 / 不含 Active Skills 段 / `activate_skill` 后字典含 name / `clear_active_skills` 清空
- 备注: 用 `unittest.mock.MagicMock / AsyncMock` 替代真实 Agent；`pytest.mark.asyncio` 配 `pytest-asyncio`

## T12: InstallSkill 远程安装

- 影响文件: `potatocode/skills/install.py`（新建）、`potatocode/tools/install_skill.py`（新建）、`potatocode/app.py`（修改）
- 依赖任务: T9（命令注册）
- 完成标准:
  - `parse_skill_url(url)` 支持 skills.sh / github.com tree / raw.githubusercontent.com 三种 URL，拒绝其他 host
  - `install_skill(src, install_root)` 走 GitHub Contents API（httpx）递归下载到 staging temp dir，验证含 SKILL.md 后 atomic rename
  - 限额：单文件 ≤1 MiB、总大小 ≤8 MiB、文件数 ≤64、深度 ≤4
  - `InstallSkillTool` 实现 Tool 协议，name = InstallSkill，category = write
  - 执行后调 loader.reload() + on_installed 回调重新注册斜杠命令

## 进度

- [x] T1（skills/parser.py：SkillDef / parse_frontmatter / _validate_meta / substitute_arguments）
- [x] T2（skills/loader.py：两级搜索 + 热重载 + get_catalog / get_source_label）
- [x] T4（skills/executor.py：execute_inline 钉 SOP 到 env）
- [x] T5（skills/executor.py：execute_fork 独立 Conversation + 临时 Agent + 文本回流）
- [x] T6（agent：active_skills / _skill_catalog / activate_skill / clear_active_skills / set_skill_catalog；prompt/skills_block.py：build_environment_context 每轮迭代重建 env）
- [x] T7（tool/load_skill.py：read-only、set_loader/set_agent 注入器、unknown 时列出 catalog）
- [x] T8（cli.py + tui/app.py 接线：LoadSkill 注册 → Agent → loader.load_all → 注入 → SkillExecutor → catalog → register_skill_commands）
- [x] T9（command/builtin_skill.py `/skill list|info|reload`、command/skill_register.py skill 命令注册 + `[skill]` 后缀、builtin_ui.handle_clear 钩 clear_active_skills）
- [x] T10（端到端：Windows 无 tmux，改用 Textual run_test 无头驱动 tests/test_skills_e2e.py，覆盖 /help、/test-skill、热重载、LoadSkill 意图触发、/clear；真实 TUI 手动验收留给用户）
- [x] T11（tests/test_skills.py：parser / substitute / loader / executor / LoadSkill / Agent 集成 / 命令注册 / install，共 60+ 用例）
- [x] T12（skills/install.py 三种 URL + Contents API 递归 + 限额 + 暂存原子落位；tool/install_skill.py write 类别 + reload + on_installed 回调）