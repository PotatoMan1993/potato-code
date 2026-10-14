# Skill 系统 Spec

## 1. 背景

PotatoCode 用户会反复输入一组类似的 prompt（commit message 规范、代码审查清单、跑测试的项目类型识别）。当前所有 prompt 要么写死在源码 Slash Command（`/review`）里，要么用户每次手敲，两个痛点：(1) 不能复用与分发，(2) 长流程任务缺少上下文隔离，中间状态会污染主对话。Skill 把可复用 SOP 装进可编辑的 Markdown 文件，配渐进式披露与执行模式，同时解决这两个问题。

## 2. 目标

把 `SKILL.md` 升级为「带 frontmatter + 资源」的能力包。启动时只把 `name + description` 注入对话给 Agent 看；Agent 通过 `LoadSkill` 工具按需把完整 SOP 钉到环境上下文。`inline` 模式 SOP 在主对话内执行，`fork` 模式独立子 Agent 隔离执行后把结果回流。`/` 显式触发与意图识别自动触发共用同一套执行器。

## 3. 功能需求

### 解析与加载
- F1: `SkillDef`（`potatocode/skills/parser.py:24`）字段：`name / description / prompt_body / mode / model / context / source_path / is_directory`；`mode` 取 `inline | fork`（默认 `inline`），`context` 取 `full | recent | none`（默认 `full`，仅 fork 模式生效）
- F2: 单文件 `*.md`（YAML frontmatter + body）与目录型（`/SKILL.md` + `references/*.py`）两种磁盘布局；`SkillLoader._scan_directory` 区分两类
- F3: 两级搜索路径加载（`potatocode/skills/loader.py:23`），优先级 `项目 .potatocode/skills/` > `~/.potatocode/skills/`；首次出现的 name 占位，后续同名跳过；解析失败单条 `warning` 日志并跳过
- F4: 启动期 `SkillLoader.load_all` 解析所有 frontmatter+body 进内存；`SkillLoader.get(name)` 每次重读源文件实现热重载，失败回退缓存（`potatocode/skills/loader.py:96`）

### 执行
- F5: `substitute_arguments(prompt_body, args)`（`potatocode/skills/parser.py:99`）把 `$ARGUMENTS` 替换为参数；没有占位符则原样返回
- F6: inline 执行：`SkillExecutor.execute_inline`（`potatocode/skills/executor.py:54`）渲染 body 后调用 `Agent.activate_skill(name, body)` 钉到 env context，主循环每轮迭代重建 environment 时 SOP 都注入
- F7: fork 执行：`SkillExecutor.execute_fork`（`potatocode/skills/executor.py:58`）创建独立 `ConversationManager`，按 `context` 字段决定历史携带（`full` = 主对话拼接摘要 / `recent` = 最近 5 条 / `none` = 完全隔离），临时 Agent 跑到 `LoopComplete` 后把累计文本回流

### LoadSkill 工具与 Skill Catalog 注入
- F10: `LoadSkill`（`potatocode/tools/load_skill.py:21`）read-only 工具，输入 `{name: str}`；调用 `SkillLoader.get` 取 skill → `Agent.activate_skill` 钉 SOP → 返回简短确认（不返回完整 SOP，避免 tool_result 占用空间）
- F11: 启动期 `app.py:673` 构建「Available Skills」段（只 `- : ` 列表 + LoadSkill 调用指引），通过 `Agent.set_skill_catalog` 注入 environment context（`potatocode/prompts.py:293`）

### 命令集成
- F12: 每个 skill 由 `register_skill_commands`（`potatocode/commands/handlers/skill_register.py:18`）注册为 `/` 短命令，描述末尾标注 `[skill]`；mode 字段决定运行时分支：inline 调 `execute_inline` 后再发送一次 user message 触发 loop，fork 则后台 `asyncio.create_task(_run_fork)` 把结果作为 system message 插入
- F13: `/skill list | info  | reload` 管理子命令（`potatocode/commands/handlers/skill.py:11`）：list 列出已加载 skill 与来源；info 显示完整 frontmatter 与文件路径；reload 重新扫描两级目录并重新注册命令
- F14: skill 命令与已有 slash 命令同名时，skill 版本优先覆盖旧 handler

### 热更新与清理
- F17: `SkillLoader.get(name)` 每次调用都 `parse_skill_file(source_path)` 重读，文件修改即时生效；解析失败回退 `_cache` 中的旧版本并记 warning（`potatocode/skills/loader.py:103`）
- F18: `/clear` 命令在清对话历史时调 `Agent.clear_active_skills()`（`potatocode/commands/handlers/clear.py:19`）把激活 skill 列表清空

### 远程安装
- F19: `InstallSkillTool` 让用户把 URL 发给 potatocode、由 Agent 自动安装到 `~/.potatocode/skills/<name>/`
  - 支持三种 URL：`skills.sh` / `github.com tree` / `raw.githubusercontent.com`
  - 走 GitHub Contents API 递归拉取目录树（无需本地 git），单文件 ≤1 MiB、总大小 ≤8 MiB、文件数 ≤64、深度 ≤4
  - 暂存到兄弟 tempdir，验证含 SKILL.md 后 atomic rename 到位
  - 安装后自动 reload catalog + 重新注册斜杠命令，无需重启即可使用

### 来源标识
- F20: `SkillLoader.get_source_label`（`potatocode/skills/loader.py:117`）按路径前缀返回 `project | user`

## 4. 非功能需求

- N1: 单个 skill 文件解析失败不能阻断其他 skill 加载，错误走 `logging.warning`
- N2: `LoadSkill` 工具调用不弹权限提示（read-only 类别）
- N3: fork 模式必须隔离 `ConversationManager`，主对话状态不被子 Agent 修改
- N5: 项目级与用户级同名 skill 冲突时，项目级优先

## 5. 设计概要

### 核心数据结构
- `SkillDef`（`potatocode/skills/parser.py:23`）：dataclass，含 `mode / model / context` 三个执行字段 + `source_path / is_directory` 元信息
- `SkillLoader`（`potatocode/skills/loader.py:15`）：name → `SkillDef`；持有 `_skills` 与 `_cache` 两份字典，热更新失败回退缓存
- `SkillExecutor`（`potatocode/skills/executor.py:43`）：`execute_inline(skill, args) -> None` 与 `execute_fork(skill, args) -> str`
- `LoadSkill`（`potatocode/tools/load_skill.py:21`）：实现 `Tool` 抽象类；持有 `SkillLoader` 与 `Agent` 引用
- Agent 新增字段与方法：`active_skills: dict[str, str]`、`_skill_catalog: str`、`activate_skill(name, body)`、`clear_active_skills()`、`set_skill_catalog(catalog)`（`potatocode/agent.py:317-364`）

### 主流程
1. 启动：`PotatoCodeApp.__init__` → 实例化 `LoadSkill` 并 register → 构造 `Agent` → `SkillLoader(work_dir).load_all()` 扫描项目和用户目录 → `load_skill_tool.set_loader/set_agent` → 构造 `SkillExecutor` → 把 catalog 字符串写入 `agent.set_skill_catalog` → `register_skill_commands` 把每个 skill 注册成 `/`
2. system prompt 注入：`build_environment_context`（`potatocode/prompts.py:277`）每轮迭代重建 environment block，把 `agent._skill_catalog` 与 `agent.active_skills` 字典分别拼为 catalog 段和「## Active Skills」段
3. 主 Agent 循环每轮 `_build_system_message` 调 `build_environment_context(work_dir, active_skills, skill_catalog, agent_catalog)`（`potatocode/agent.py:400`），实现 SOP 钉到 env 的能力
4. 显式调用（以 inline skill 为例，如用户自建的 `/commit`）：`register_skill_commands` 注册的 handler → `executor.execute_inline(skill, args)` → `agent.activate_skill(name, rendered_body)` → 再 `ctx.ui.send_user_message(trigger)` 触发 Agent loop
5. 意图识别：Agent 调 `LoadSkill({name: ""})` → `loader.get` → `agent.activate_skill` → 返回 `"Skill '' activated. SOP pinned to environment context."`
6. fork 调用（以 fork skill 为例，如用户自建的 `/review`）：handler 走 `asyncio.create_task(_run_fork)` → `executor.execute_fork` 新 conversation + 临时 Agent + 收集 `StreamText` 到 `LoopComplete` → 把 finalText 作为 system message 插入主对话
7. `/clear`：handler → reset conversation → `agent.clear_active_skills()` → 后续轮 environment 不再注入旧 SOP

### 调用链
- 启动：`potatocode.app.PotatoCodeApp.__init__` → `SkillLoader.load_all` → `register_skill_commands`（`potatocode/app.py:687`）
- inline 显式（例如 `/commit`）：用户输入 skill 命令 → command handler → `executor.execute_inline` → `agent.activate_skill` → `ctx.ui.send_user_message` → Agent loop（每轮 env 注入 SOP）
- fork 显式（例如 `/review`）：用户输入 skill 命令 → handler → `asyncio.create_task(execute_fork)` → `system message`
- 意图触发：Agent 在某轮调用 `LoadSkill` → `loader.get` → `agent.activate_skill` → 下一轮 SOP 钉在 env 里
- 清理：用户 `/clear` → `handle_clear` → conversation reset + `agent.clear_active_skills`

### 与其他模块的交互
- 上行依赖：`potatocode/app.py`（注入 system prompt、注册命令、注入 `SkillLoader/SkillExecutor` 到 `CommandContext.config`）、`Agent`（`active_skills` 字段 + env 注入）、`ConversationManager`（fork 用独立实例）
- 下行：`SkillExecutor` 通过 `from potatocode.agent import Agent` 局部 import 避免循环依赖

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。