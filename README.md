# PotatoCode

Claude Code 风格的终端 AI 对话客户端，Python 实现。

当前章节（ch08）：支持 Anthropic 与 OpenAI 两种协议、工具调用、权限控制、MCP、
Textual TUI 和上下文管理。

## 安装

```bash
uv sync
```

## 配置

复制配置模板并填入真实密钥：

```bash
cp .potatocode/config.yaml.example .potatocode/config.yaml
```

每个 provider 必须配置实际端点的 `context_window`（大于 33,000 的整数）。
缺少或无效时界面仍可启动，请求会显示配置错误并保留用户输入。
普通输出上限 `max_output_tokens` 默认 4,096；摘要输出上限
`compact_max_output_tokens` 默认 20,000，且不能超过 20,000。
OpenAI 默认使用 `max_completion_tokens`；旧兼容端点若要求 `max_tokens`，设置
`openai_output_limit_parameter: max_tokens`。

## 上下文管理

每次普通 API 请求（包括工具续调）先处理大工具结果，再检查历史预算。
单个结果超过 50,000 字符会完整存入
`.potatocode/context/<会话ID>/tool-results/`，历史中保留前 1,500 字符和文件路径。
同一条工具消息合计超过 200,000 字符时，从最大结果开始逐个存盘。
这些文件已被 Git 忽略，会话期间应保留以供模型按需读取。

上下文达到 `context_window - 33,000` token 时自动摘要早期消息。
预算采用上一次成功普通请求的 API 输入 usage 校准，新增内容按字符估算；
usage 台账独立记录每次普通和摘要请求，状态栏显示累计计费用量。
缺失 usage 会标注“部分用量未知”，不会把多轮输入累加当作当前上下文。

空闲时输入 `/compact` 可手动压缩，不继续执行任务，不改变权限模式。
自动、手动和实际超限后的紧急压缩都严格保留至少 5 条、合计超过 10,000 token
的近期消息，并保持工具调用与结果配对。没有合法早期消息时提示无法压缩；
若剩余原文仍超窗口，则停止本轮并报告原因。
摘要最多尝试 3 次；紧急恢复成功后，原请求最多重发 1 次。
压缩过程中按 Esc 或 Ctrl+C 可取消，近期原文不会被替换。

摘要包含任务进展、可验证的用户引文、最近最多 5 个文件及已加载 Skill。
精确代码应重新读取源文件。当前使用近似估算，不包含精确 tokenizer 或跨进程会话恢复。

## 运行

```bash
uv run potatocode
# 或
python -m potatocode
```

## 文档

见 `docs/` 各章节的 spec / plan / task / checklist。
本章验收：`uv run python docs/ch08/evidence/context_e2e.py`（本地受控 provider，无真实 API 请求）。
