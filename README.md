# PotatoCode

Claude Code 风格的终端 AI 对话客户端，Python 实现。

当前章节（ch02）：多协议 LLM 终端对话客户端——支持 Anthropic 与 OpenAI 两种协议，
流式输出、多轮对话、Textual TUI。

## 安装

```bash
uv sync
```

## 配置

复制配置模板并填入真实密钥：

```bash
cp .potatocode/config.yaml.example .potatocode/config.yaml
```

## 运行

```bash
uv run potatocode
# 或
python -m potatocode
```

## 文档

见 `docs/` 各章节的 spec / plan / task / checklist。
