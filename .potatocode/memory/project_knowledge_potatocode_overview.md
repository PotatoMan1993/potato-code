---
type: project_knowledge
title: PotatoCode 项目概述
created: '2026-10-20T11:14:00.099042+08:00'
updated: '2026-10-20T11:48:49.409053+08:00'
---
PotatoCode 是用 Python 3.12 和 Textual 开发的终端 AI 编程助手，支持 Anthropic 等模型。项目指令由 src/potatocode/instructions/loader.py 加载，按优先级合并三层 POTATOCODE.md（项目根 > <project_root>/.potatocode/ > ~/.potatocode/）并展开 @include。长期记忆存放在 ~/.potatocode/memory/ 下，一条笔记一个带 YAML frontmatter 的 Markdown 文件，文件名形如 <type>_<slug>.md，MEMORY.md 维护每行一条的索引；笔记分 project 与 user 两级。
