# PotatoCode

## 项目概述

PotatoCode 是使用 Python 3.12 和 Textual 开发的终端 AI 编程助手，支持
Anthropic 与 OpenAI 协议。

## 沟通语言

- 使用中文回答。
- 代码注释和项目文档使用中文。
- 标识符遵循项目现有的英文命名风格。

## 开发约定

- 使用 `uv` 管理依赖和执行命令。
- 开始章节或功能开发前，阅读相关的 `docs/chXX/spec.md`、`plan.md`、
  `task.md` 和 `checklist.md`。
- 新增功能、模块、架构或明显改变外部行为时，使用仓库中的
  `potato-spec` skill。小型缺陷修复、文案和格式调整可以直接修改并验证。
- 保留用户已有改动，不修改与当前任务无关的代码。
- 不读取、输出或提交 API Key、访问令牌及本地密钥配置。
- 每个自定义类和自定义函数内部都要添加注释：描述自定义类或自定义函数的功能，最多100字。

## 验证

完成代码修改后，根据改动范围运行：

1. `uv run ruff format --check .`
2. `uv run ruff check .`
3. `uv run pytest`
4. 必要时运行 `uv run mypy src/potatocode`

涉及终端交互时执行端到端测试：

- 环境提供 tmux 时使用 tmux。
- Windows 环境使用 Textual `App.run_test()` 或对应章节 `evidence/` 下的测试脚本。
- 对照对应章节的 `checklist.md`，记录实际执行结果后再报告结论。

