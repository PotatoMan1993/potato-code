# PotatoCode 架构：七层模块与交互时序

> 用 mermaid 绘制。渲染方式任选其一：GitHub 预览 / VS Code 装 `Markdown Preview Mermaid` 插件 / Typora / Obsidian。
>
> 两张图：
> 1. **七层模块架构图** —— 层与层之间的依赖/数据流向
> 2. **交互时序图** —— 从「用户输入」到「agent 处理完成」的过程中，各层之间每一次信息交互的时机与内容

---

## 一、七层模块划分

| 层 | 模块 | 关键文件 | 关键接口 |
|---|---|---|---|
| 1 | **app 层** | `cli.py`、`tui/app.py` | `PotatoCodeApp.on_chat_submitted` / `_start_turn` / `action_cancel_or_quit` |
| 2 | **tui 层** | `tui/app.py`、`tui/stream.py`、`tui/view.py`、`tui/select.py` | 用户输入框 `ChatTextArea`、streaming 动态框 `StreamMixin._consume_agent_events`、对话历史框 `RichLog #log`、状态栏 `statusbar` |
| 3 | **agent 规划层** | `agent/__init__.py` | `Agent.run`（ReAct Loop）/ `_execute_batched`（保序分批） |
| 4 | **conversation 层** | `conversation.py` | `add_user` / `add_assistant_with_tool_calls` / `add_tool_results` / `messages` |
| 5 | **agent 单轮** | `agent/__init__.py` | `_stream_once`（一次请求 + 双路收集） |
| 6 | **llm 层** | `llm/__init__.py`、`llm/anthropic_provider.py`、`llm/openai_provider.py` | `Provider.stream(msgs, tools, system_suffix) → AsyncIterator[StreamEvent]` |
| 7 | **工具层** | `tool/__init__.py`、`tool/*.py` | `Registry.execute(name, input, timeout) → Result` |

分层约定：**上层依赖下层、单向调用**。agent 规划层只依赖 `llm / tool / conversation / prompt`，不 import 任何厂商 SDK（协议无关）；llm 层对外吐统一的 `StreamEvent`；工具层永远以值类型 `Result` 返回、绝不向调用方抛异常。

### 七层架构图

```mermaid
flowchart TB
    USER(["👤 用户"])

    subgraph L1["1 · app 层"]
        APP["PotatoCodeApp<br/>状态机 · 命令处理 · 启停一轮"]
    end

    subgraph L2["2 · tui 层"]
        IN["用户输入框<br/>ChatTextArea"]
        ST["streaming 动态框<br/>StreamMixin 消费端"]
        LOG["对话历史框<br/>RichLog #log"]
        BAR["状态栏<br/>statusbar"]
    end

    subgraph L3["3 · agent 规划层"]
        AG["Agent.run<br/>ReAct Loop + 保序分批"]
    end

    subgraph L4["4 · conversation 层"]
        CV["Conversation<br/>消息列表"]
    end

    subgraph L5["5 · agent 单轮"]
        ON["_stream_once<br/>单次请求 + 双路收集"]
    end

    subgraph L6["6 · llm 层"]
        LLM["Provider<br/>anthropic / openai 适配器"]
    end

    subgraph L7["7 · 工具层"]
        TK["Registry<br/>6 个核心工具"]
    end

    USER -->|"Enter 提交"| IN
    IN -->|"ChatSubmitted(text)"| APP
    APP -->|"add_user / 回灌历史"| CV
    APP -->|"启动消费 task"| ST
    APP -->|"定型块：用户/最终答复"| LOG
    APP -->|"刷新累计用量"| BAR
    ST -->|"消费事件流"| AG
    AG -->|"读：conv.messages()"| CV
    AG -->|"写：assistant/tool 回合"| CV
    AG -->|"单轮请求"| ON
    ON -->|"provider.stream"| LLM
    AG -->|"registry.execute"| TK
    TK -->|"Result"| AG
```

---

## 二、交互时序图（用户输入 → agent 处理完成）

参与者（左侧为 UI，右侧为远端）：

```mermaid
sequenceDiagram
    participant U as 用户
    participant IN as 2·tui 输入框<br/>ChatTextArea
    participant APP as 1·app 层<br/>PotatoCodeApp
    participant LOG as 2·tui 对话历史框<br/>RichLog
    participant BAR as 2·tui 状态栏
    participant ST as 2·tui streaming 动态框<br/>StreamMixin 消费端
    participant AG as 3·agent 规划层<br/>Agent.run
    participant CV as 4·conversation 层<br/>Conversation
    participant ON as 5·agent 单轮<br/>_stream_once
    participant LLM as 6·llm 层<br/>Provider
    participant TK as 7·工具层<br/>Registry

    Note over U,APP: ① 用户提交（app.on_chat_submitted，tui/app.py:242）
    U->>IN: 输入文本，按 Enter
    IN->>APP: ChatSubmitted(text)
    APP->>CV: conv.add_user(text)　用户消息入历史
    APP->>LOG: user_block(text)　用户块定型
    APP->>APP: _start_turn()：state=STREAMING、建 turn_cancel、起 0.1s 定时器
    APP->>ST: asyncio.create_task(_consume_agent_events())
    ST->>ST: 刷新动态框：spinner "Imagining…"
    ST->>AG: Agent(provider, registry).run(conv, mode, cancel)

    Note over AG,TK: ② Agent Loop 每一轮迭代（for it in 1..25）
    loop 迭代循环
        AG-->>ST: Event(iter=it)　迭代进度
        ST->>ST: 刷新动态框（第 N 轮）

        Note over AG,LLM: ③ agent 单轮：发起请求 + 双路收集（_stream_once，agent/__init__.py:237）
        AG->>ON: _stream_once(conv, defs, suffix, cancel)
        ON->>CV: conv.messages()　取完整历史（含上轮 assistant/tool 回合）
        ON->>LLM: provider.stream(msgs, tools, system_suffix)
        loop 流式接收 StreamEvent（LLM 一次请求）
            LLM-->>ON: StreamEvent(text 增量)　正文增量
            ON-->>ST: Event(text 增量)　实时转发
            ST->>ST: cur_reply 累积 + 刷新动态框
        end
        LLM-->>ON: StreamEvent(usage / tool_calls / done / err)
        ON-->>AG: _StreamOutcome(text, calls, usage, ok)

        AG-->>ST: Event(usage)　本轮 token 用量
        ST->>BAR: usage_in/out 累加，刷新状态栏

        alt 本轮请求了工具（calls 非空）
            AG->>CV: add_assistant_with_tool_calls(text, calls)　assistant 回合入历史
            loop 保序分批（_execute_batched：只读并发 / 副作用串行）
                AG-->>ST: Event(tool, phase=START)
                ST->>LOG: 首个工具前：preamble 文本定型
                ST->>ST: 动态框追加 ●name(args) Running…
                AG->>TK: registry.execute(name, input, timeout)
                TK-->>AG: Result(content, is_error)　工具层恒返结构化结果
                AG-->>ST: Event(tool, phase=END, result, is_error)
                ST->>LOG: tool_line + 结果摘要定型
            end
            AG->>CV: add_tool_results(results)　④ 工具结果回灌进历史
        else 本轮为纯文本（自然完成）
            AG->>CV: add_assistant(final)　最终答复入历史
            AG-->>ST: Event(done=True)
            ST->>LOG: assistant_block 最终答复 markdown 定型
            ST->>APP: 停定时器、state=IDLE、聚焦输入框
        end
    end

    Note over U,AG: ⑤ 提前终止路径（F7/F2：取消 / 流出错 / 迭代上限 / 未知工具）
    opt 取消 / 出错
        U->>APP: Esc / Ctrl+C
        APP->>AG: turn_cancel.set()
        AG->>CV: 收尾历史（补「已取消」工具结果 + assistant 兜底文本，保 F6）
        AG-->>ST: generator 终结（无 done 事件）
        ST->>LOG: notice/error 提示块
        ST->>APP: state=IDLE
    end
```

---

## 三、关键交互要点

- **两条消息通道并行**：每轮请求的流式响应走「双路」——文本增量实时经 `Event(text)` 推给 streaming 动态框（界面即时显示）；完整文本 + 拼齐 JSON 的工具调用累积在 `_stream_once` 内，供循环判断下一步（`_stream_once`，`agent/__init__.py:237`）。
- **界面三块分工**：`#streaming` 动态框只放「流式中回复 + 执行中工具指示 + 迭代进度」，可实时重绘；`RichLog #log` 对话历史框放所有已定型内容（用户块 / preamble / 工具行 / 结果摘要 / 最终答复 / 提示），可滚动回看；两者由 agent 事件流驱动，互不感知。
- **conversation 是 Agent 与 LLM 之间的「持久内存」**：每轮 `_stream_once` 从 `conv.messages()` 读完整历史；agent 每轮结束后写回 `assistant（含工具调用）` 与 `tool` 回合（`add_tool_results`），下一轮请求自动携带——这就是多轮自动连环的关键。
- **工具结果恒为结构化值**：`Registry.execute` 把未知工具 / 超时 / 异常一律包成 `Result(is_error=True)` 返回，agent 原样回灌给模型，模型据此调整或收手，从不抛异常打断循环（`tool/__init__.py:122`）。
- **事件流解耦**：agent 规划层对外只吐统一 `Event`（文本 / 工具 / 用量 / 迭代 / 提示 / done / err），tui 只消费事件、不感知循环内部细节；取消通过共享的 `asyncio.Event`（`turn_cancel`）从 app 层注入，agent 在下次迭代/await 点响应。
- **启动路径（时序图未展开）**：`cli.main` 加载配置 → `new_default_registry()` 注册 6 工具 → `PotatoCodeApp.run()`；单 provider 直进对话，多 provider 先进选择界面（`tui/select.py`）。
- **命令分支**：`/plan` 切换 `Mode.PLAN`（只注入只读工具定义 + 计划态 system 后缀）；`/do` 把 `EXECUTE_DIRECTIVE` 写入历史后走与普通消息完全相同的流程；`/exit` 退出。
