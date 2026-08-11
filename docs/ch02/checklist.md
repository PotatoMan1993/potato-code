# 多协议 LLM 终端对话客户端 Checklist

> 每一项通过运行代码或观察行为来验证，聚焦系统行为；括号内为验证方式。
> 验收完成于 2026-08-12：存证见 `docs/ch02/evidence/`（S1-S8 桩验证 + R1-R4 真实链路截图，
> 复现脚本 `e2e_capture.py` / `real_e2e.py`）。**全部 26 项通过。**

## 实现完整性
- [x] 配置加载：合法 `.potatocode/config.yaml` 能解析出 providers 列表（验证：单测 + 启动进入对话）。(AC1/F1) — 12 项单测通过；e2e S1；真实 2 条配置加载成功
- [x] 配置校验：缺密钥/非法 protocol/文件缺失时给出可读错误并非零退出，无未捕获堆栈（验证：删字段/改 protocol/删文件分别运行 `python -m potatocode`）。(AC1/N4) — 三种坏配置均打印「配置错误: …」且 exit=1
- [x] 单 provider 直进：仅一条配置时启动直接进入对话（验证：单条配置运行）。(AC2/F2) — e2e S1 截图
- [x] 多 provider 选择：多条配置时出现方向键 `OptionList`，选定后进入对话（验证：两条配置运行、上下选择 + Enter）。(AC2/F2) — e2e S2 + R1（真实 2 条配置出现选择列表，选定后状态栏显示 name/model）
- [x] 内置 system prompt 与历史随请求发送（验证：问"你的角色/规则"，回答体现内置 prompt；多轮见 AC6）。(AC4/F4) — 真实链路 R1：模型自报「我是 PotatoCode，一个运行在终端里的 AI 助手」并逐条列出内置规则；历史携带见 R1 多轮
- [x] thinking：anthropic 配 `thinking: true` 时启用，且界面不出现任何思考文本（验证：开启后观察仅最终回复）。(AC5/F5) — 真实链路验证：原始事件流观测到 4 个 thinking_delta（18 字符）+ signature_delta，适配器全部丢弃，界面仅最终回复（R2 截图）
- [x] 流式逐字：回复以纯文本逐字出现（验证：长回复肉眼可见逐步输出）。(AC5/F8) — e2e S3（增量分段到达、动态区实时更新）+ R1-R4 真实流式
- [x] markdown 定型：回复结束后整段以 markdown 渲染（代码块/列表/强调正确）（验证：让模型输出含代码块与列表的内容）。(AC8/F8) — e2e S4 + R3（fib 代码块高亮）+ R4（quick_sort 代码块）
- [x] 多行输入：Alt+Enter 换行、Enter 提交、提交后输入框清空（验证：输入两行后提交）。(AC9/F9) — e2e 场景 A（"hi\nyo" 提交成功）
- [x] 响应计时：自提交即显示 `Imagining… (Ns)` 且秒数递增，结束后显示总耗时（验证：发一条慢回复观察）。(AC12/F12) — e2e S3（等待期计时行）+ R1-R4 截图均含 ⏱ 总耗时行
- [x] 错误反馈：错误 key/不存在模型时，错误在对话区可区分样式（红色）显示且不退出（验证：改坏 key 运行后再正常发一条）。(AC11/F11) — e2e S6（错误后继续对话）+ S7（真实端点 401）
- [x] 退出：`/exit` 与 Ctrl+C 均能安全退出，终端恢复正常（验证：两种方式各试一次，观察无残留/错乱）。(AC10/F10/N7) — e2e 场景 D/E/F（含流式中 Ctrl+C 取消 task）+ real_e2e 以 /exit 收尾；Textual 自动还原终端模式
- [x] 界面布局：启动 banner + 名称版本 + cwd + 就绪提示行 + 输入框（含 `❯` 与占位符）+ 状态栏（左 name 右 model）（验证：启动截图比对）。(AC7/F7) — e2e S1 截图逐项核对通过；R1-R4 状态栏真实显示 provider/模型

## 集成
- [x] TUI 通过统一 `Provider` Protocol 驱动两种协议，切换协议不改变上层交互（验证：分别用 anthropic / openai 配置跑同一组对话，行为一致）。(AC3/N3) — 真实链路 R1-R3（anthropic 协议）与 R4（openai 协议）交互行为一致：流式、定型、计时、状态栏均相同
- [x] 多轮上下文携带：先告知信息、后追问，模型能正确引用前文；退出再启动后历史为空（验证：两轮对话 + 重启验证）。(AC6/F6) — 真实链路 R1：第二轮准确回答「你叫小明，最喜欢蓝色」；重启后选单/历史从零开始；e2e S5 验证请求携带全部历史
- [x] 流式不阻塞：等待/流式期间界面仍响应、不冻结（验证：长回复期间界面持续刷新；asyncio event loop 不阻塞）。(AC13/N1) — 全程 asyncio 驱动；S3 流式期间计时持续刷新
- [x] scrollback 渲染（Claude Code 风格）：完成的消息（用户输入/助手回复/错误）追加到 `RichLog`，可用 Textual 滚动回看，动态区仅含输入框 + 正在流式的回复 + 状态栏（验证：多轮后滚动查看历史）。— 完成块经 RichLog 持久化并自动滚动到底（scroll_y=max 验证）；R1 截图含四轮完整历史。注：Textual 使用备用屏幕，退出后内容不保留在终端原生历史中（技术选型固有行为）
- [x] base_url 覆盖：为某 provider 配自定义 `base_url`（兼容端点）可正常收发（验证：配一个兼容端点跑通一轮）。(F3) — 真实链路两个 provider 均经自定义 base_url 接入兼容端点（qwen3.8-max），收发正常
- [x] 窗口自适应：缩放终端宽度后输入框/对话区/markdown 不错版（验证：运行中调整终端宽度）。(N6) — e2e S8（100x32 → 50x20 后继续正常对话）

## 编译与测试
- [x] `python -m potatocode` 能正常启动（在合法配置下进入 TUI）。— 实际启动进入 Textual 终端模式（ANSI 转义序列可见）
- [x] `ruff check .` 无告警。
- [x] `ruff format --check .` 通过（或本地 `ruff format .` 已统一格式）。
- [x] `pytest` 通过（`tests/test_config.py`、`tests/test_conversation.py`）。— 15 passed
- [x] （可选）`mypy src/potatocode` 通过（启用 strict 子集亦可）。— Success: no issues found in 14 source files
- [x] 密钥不回显/不打印：对话区与任何输出均不出现 `api_key`（验证：通读运行输出、检索无明文 key）。(N5) — 全部 12 张截图 SVG 检索无密钥明文（含真实密钥前后 20 字符片段比对）

## 端到端场景
- [x] 场景 1（anthropic 多轮）：单条 anthropic 配置启动 → 连续两轮、第二轮引用第一轮 → 流式 + 计时 + markdown 定型 → `/exit` 退出。— 真实链路 R1-R3（四轮对话，含上下文引用、计时、代码块定型）
- [x] 场景 2（openai 流式）：openai 协议配置 → 发一条含代码块的请求 → 流式逐字后 markdown 渲染正确。— 真实链路 R4（quick_sort 代码块 + 3 点列表，7.0s 流式完成）
- [x] 场景 3（多 provider 选择）：两条配置 → 启动出现列表 → 选第二条 → 状态栏显示其 name/model → 正常对话。— e2e S2 + real_e2e 两次启动（先选第一项、再选第二项，状态栏随之更新）
- [x] 场景 4（错误恢复）：错误 key 触发失败 → 对话区红色错误、程序不退出 → 修正后继续正常对话。— e2e S6/S7（含真实端点 401；同进程换回正常 provider 继续对话验证通过）
