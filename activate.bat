[33mcommit 580434b071b0823ab3aa87e5d2bd2e5f20e38dfd[m[33m ([m[1;36mHEAD[m[33m -> [m[1;32mmain[m[33m, [m[1;31morigin/main[m[33m)[m
Author: Potato Man <yqzhang_opt@126.com>
Date:   Sun Aug 30 10:51:10 2026 +0800

    ch06的四份开发文档

[33mcommit e78e1529212b745936925b9bfbe277724d2a91f9[m
Author: Potato Man <yqzhang_opt@126.com>
Date:   Fri Aug 28 11:05:59 2026 +0800

    提交项目级权限配置文档

[33mcommit 3c34fea32536e15392109ebc685ea2904e4b1477[m
Author: Potato Man <yqzhang_opt@126.com>
Date:   Thu Aug 27 09:27:55 2026 +0800

    ch06: 权限系统——五层串联 + OS沙箱

[33mcommit bfd63c9a8f25a74c07c91baad969b79d17207c75[m
Author: Potato Man <yqzhang_opt@126.com>
Date:   Wed Aug 26 00:41:58 2026 +0800

    ch05: 系统提示工程化——模块化装配 + 缓存通道 + 规划按轮次注入
    
    - prompt 包：Module 模块化装配（七固定模块 10..70 + 三可选空槽 80..100），environment 环境采集（cwd/平台/日期/git/版本/模型，git 2s 超时降级），reminder 补充消息（system-reminder 标签 + 规划完整/精简）
    - llm：新增 System/Request dataclass，Usage 加 cache_write/cache_read，Provider.stream 改 req 入参；删 _effective_system 与 prompt import
    - anthropic：稳定块打 cache_control 断点 + 环境块不缓存，解析 cache_creation/read_input_tokens，reminder 并入末条 user content 块
    - openai：单条 system（stable+env 拼接），解析 prompt_tokens_details.cached_tokens，reminder 追加尾部 user
    - agent：run 采集环境/装配稳定系统提示，规划模式按轮次注入 reminder（iter==1 或 (iter-1)%4==0 完整、其余精简），缓存用量透传
    - 工具：edit_file/bash 描述双重强化（编辑前先 read_file、优先用专用工具而非 bash 拼凑）
    - 测试：test_prompt 14 + test_anthropic_system 7 + test_agent 适配，75 passed；ruff/mypy 干净
    - 文档：docs/ch05/spec-plan-task-checklist
    
    Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

[33mcommit 607574409e4dcad174efe4676e7eacab8f1360e1[m
Author: Potato Man <yqzhang_opt@126.com>
Date:   Mon Aug 24 00:48:04 2026 +0800

    ch04：agent loop + 迭代进度 + token用量 + plan/do模式切换

[33mcommit dd93290b958177586e470ca2717cc318b8327c62[m
Author: Potato Man <yqzhang_opt@126.com>
Date:   Fri Aug 14 12:03:48 2026 +0800

    ch03: 工具系统——6 核心工具 + agent 单轮闭环 + 双协议工具调用
    
    - tool 包：Tool/Result/Registry 抽象 + read_file/write_file/edit_file/bash/glob/grep 六工具，失败统一结构化返回
    - agent 包：Agent.run 单轮闭环（请求#1 带工具 → 执行 → 结果回灌 → 请求#2 续答），AC9 单轮上限
    - llm：ToolCall/ToolResult/ToolDefinition/ROLE_TOOL；anthropic 解析 tool_use、openai 按 index 拼 tool_calls
    - tui：Claude Code 风格工具行 ● name(args) + ⎿ 结果摘要 + 执行指示，接入 Agent.run
    - 测试：test_tool 18 项 + test_agent 2 项 + conversation 工具回合，36 passed；ruff/mypy 干净
    - 验收：docs/ch03/checklist.md 27 项全过，真实链路 openai/anthropic 各跑通一次，存证 e2e_smoke.py
    
    Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

[33mcommit 191101fc2babd80f887d5ba6c36d1b7ca3ef6004[m
Author: Potato Man <yqzhang_opt@126.com>
Date:   Thu Aug 13 23:51:32 2026 +0800

    走读ch02代码，并做适当样式调整

[33mcommit f270e555372c25c389b02695ffe8808af44d7032[m
Author: Potato Man <yqzhang_opt@126.com>
Date:   Wed Aug 12 01:55:55 2026 +0800

    验收完成：真实链路全部通过，checklist 26/26
    
    真实端点验证（qwen3.8-max via 自定义 base_url）：
    - system prompt 生效（模型自报 PotatoCode 身份与规则）
    - 多轮上下文（第二轮准确引用「小明/蓝色」）
    - thinking：原始流含 4 个 thinking_delta，适配器丢弃，界面零思考文本
    - anthropic / openai 双协议流式 + markdown 定型一致
    - /exit 退出、密钥零泄漏（截图前后缀比对）
    
    Co-Authored-By: Claude <noreply@anthropic.com>

[33mcommit 46ecc189451fe3ff9ed166c133585d97626a3651[m
Author: Potato Man <yqzhang_opt@126.com>
Date:   Wed Aug 12 01:34:46 2026 +0800

    验收：checklist 逐项标注验证状态与存证索引
    
    Co-Authored-By: Claude <noreply@anthropic.com>

[33mcommit a6e46c457e51cad1fe41a260781c47f34173dd24[m
Author: Potato Man <yqzhang_opt@126.com>
Date:   Wed Aug 12 01:32:03 2026 +0800

    T14: 端到端验证 + mypy 类型修复
    
    - 修复 action_quit 异步签名（textual 基类为 async）与调用点 await
    - openai 适配器消息类型窄化（ChatCompletionMessageParam）
    - 选项列表高度：去掉 auto 父容器下的百分比 max-height（循环坍缩）
    - docs 排除出 ruff；补 types-pyyaml
    - e2e_capture.py 归档至 docs/ch02/evidence/，含 8 张 SVG 存证
    
    Co-Authored-By: Claude <noreply@anthropic.com>

[33mcommit e30c7e4d50daf8e6a0d8232541dda871b2c6393d[m
Author: Potato Man <yqzhang_opt@126.com>
Date:   Wed Aug 12 01:07:25 2026 +0800

    T13: 入口装配——配置加载与可读错误退出
    
    Co-Authored-By: Claude <noreply@anthropic.com>

[33mcommit 6072d524204dd5607c6b3cae5126f4845b73e6d0[m
Author: Potato Man <yqzhang_opt@126.com>
Date:   Wed Aug 12 01:05:48 2026 +0800

    T9-T12: Textual TUI——状态机、流式计时、provider 选择与视图渲染
    
    - app.py: SELECTING/IDLE/STREAMING 状态机、ChatTextArea（Enter 提交/Alt+Enter 换行/Ctrl+C 退出）
    - stream.py: _consume_stream/_tick/_finish_*（mixin），动态区 Imagining… (Ns) 计时
    - select.py: 多 provider OptionList 选择
    - view.py: banner/用户块/markdown 定型/错误块/状态栏
    
    headless 冒烟验证六场景通过：多行编辑、流式定型、多轮上下文、
    错误恢复、方向键选择、/exit 与 Ctrl+C（含流式中取消）
    
    Co-Authored-By: Claude <noreply@anthropic.com>

[33mcommit b7f6eaa8a351707a5467a34ac5a447c9798d611b[m
Author: Potato Man <yqzhang_opt@126.com>
Date:   Wed Aug 12 00:51:10 2026 +0800

    T7+T8: anthropic / openai 双协议适配器
    
    统一 StreamEvent 输出；thinking 增量丢弃；错误转 err 事件不抛出
    
    Co-Authored-By: Claude <noreply@anthropic.com>

[33mcommit 77878278608106cb7e81151516558b6ea7f38466[m
Author: Potato Man <yqzhang_opt@126.com>
Date:   Wed Aug 12 00:45:12 2026 +0800

    T6: conversation 模块——单会话多轮历史，含单测
    
    Co-Authored-By: Claude <noreply@anthropic.com>

[33mcommit 29605e732be2549fefa0691a44f46e6f068a2c1c[m
Author: Potato Man <yqzhang_opt@126.com>
Date:   Wed Aug 12 00:43:48 2026 +0800

    T5: llm 包骨架——Provider Protocol 与统一消息/事件类型
    
    Co-Authored-By: Claude <noreply@anthropic.com>

[33mcommit 42af401f0a997182a9b893e736ce39181010906f[m
Author: Potato Man <yqzhang_opt@126.com>
Date:   Wed Aug 12 00:39:48 2026 +0800

    T4: prompt 模块——内置 system prompt 与 ASCII banner
    
    Co-Authored-By: Claude <noreply@anthropic.com>

[33mcommit e485e89c2a2c4d1a2fcf5934ae3a74372dc8d64a[m
Author: Potato Man <yqzhang_opt@126.com>
Date:   Wed Aug 12 00:36:49 2026 +0800

    T3: 配置模板 config.yaml.example 与密钥忽略规则
    
    Co-Authored-By: Claude <noreply@anthropic.com>

[33mcommit 6f1840441117f4cdedf897e37b374514486775e4[m
Author: Potato Man <yqzhang_opt@126.com>
Date:   Wed Aug 12 00:33:23 2026 +0800

    T2: config 模块——加载与校验 .potatocode/config.yaml
    
    ProviderConfig/Config/ConfigError/load，含 12 项单测
    
    Co-Authored-By: Claude <noreply@anthropic.com>

[33mcommit aed02b0df5db79108a3b47b6584f7da521f90545[m
Author: Potato Man <yqzhang_opt@126.com>
Date:   Wed Aug 12 00:24:35 2026 +0800

    T1: 初始化 Python 项目骨架与依赖
    
    pyproject.toml（PEP 621 + hatchling）、包入口与 CLI 占位
    
    Co-Authored-By: Claude <noreply@anthropic.com>
